#!/usr/bin/env python3
"""Ingest COROS/Garmin .FIT files (pure-python minimal decoder, no dependencies).

Decodes: file header, definition/data messages, compressed timestamps, CRC-tolerant.
Keeps record-level streams — timestamp, GPS, HR, distance, speed, cadence —
and stores them as workouts + series + route_points alongside Apple Health data.

Duplicate protection: if an existing workout of the same sport starts within
10 minutes, the FIT data is treated as the same session (logged, skipped)
unless --force.

Usage:
  python ingest_fit.py ~/Downloads/*.fit [--sport run] [--force] [--db ...]
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import os
import struct
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import eha_lib as E  # noqa: E402

FIT_EPOCH = datetime(1989, 12, 31, 0, 0, 0, tzinfo=timezone.utc)
SEMI = 180.0 / 2 ** 31

# base_type_id -> (struct_fmt, is_float, is_signed) ; 0x00..0x0D little-endian
BASE_TYPES = {
    0: ("B", False, False), 1: ("b", False, True), 2: ("B", False, False),
    3: ("h", False, True), 4: ("H", False, False), 5: ("i", False, True),
    6: ("I", False, False), 7: ("s", False, False), 8: ("f", True, False),
    9: ("d", True, False), 10: ("B", False, False), 11: ("H", False, False),
    12: ("I", False, False), 13: ("B", False, False),
}
BASE_SIZE = {"B": 1, "b": 1, "H": 2, "h": 2, "I": 4, "i": 4, "f": 4, "d": 8, "s": 1}

RECORD_MSG, LAP_MSG, SESSION_MSG, FILE_ID_MSG = 20, 19, 18, 0
RECORD_SCALE = {
    253: ("ts", 1.0, 0.0), 0: ("lat", SEMI, 0.0), 1: ("lon", SEMI, 0.0),
    3: ("hr", 1.0, 0.0), 4: ("cad", 1.0, 0.0), 5: ("dist", 0.01, 0.0),
    6: ("speed", 0.001, 0.0), 7: ("power", 1.0, 0.0),
}


def decode_value(fmt: str, raw: bytes, big_endian: bool):
    if fmt == "s":
        return raw.split(b"\x00", 1)[0].decode("utf-8", "ignore")
    prefix = ">" if big_endian else "<"
    try:
        return struct.unpack(prefix + fmt, raw)[0]
    except struct.error:
        return None


class FitFile:
    def __init__(self, path: str):
        self.defs: dict[int, dict] = {}
        self.prev_ts: int | None = None
        with open(path, "rb") as f:
            self.buf = f.read()
        self.pos = 0

    def parse(self):
        b = self.buf
        # header: size(1) protocol(1) profile(2) data_size(4) ".FIT"(4) crc(2)
        if len(b) < 12 or b[8:12] != b".FIT":
            raise ValueError("not a FIT file")
        hdr_size = b[0]
        self.pos = hdr_size
        records = []
        laps = []
        session = {}
        while self.pos < len(b) - 2:
            h = b[self.pos]
            self.pos += 1
            if h & 0x80:  # compressed timestamp
                local = (h >> 5) & 0x03
                off = h & 0x1F
                if self.prev_ts is not None:
                    t = (self.prev_ts & ~0x1F) + off
                    if t < self.prev_ts:
                        t += 32
                    self.prev_ts = t
                msg = self.defs.get(local)
                if not msg:
                    continue
                vals = self._read_data(msg, override_ts=self.prev_ts)
                self._dispatch(msg, vals, records, laps, session)
            elif h & 0x40:  # definition
                local = h & 0x0F
                self._read_def(local, dev_fields=bool(h & 0x20))
            else:  # data
                local = h & 0x0F
                msg = self.defs.get(local)
                if not msg:
                    continue
                vals = self._read_data(msg)
                self._dispatch(msg, vals, records, laps, session)
        return records, laps, session

    def _read_def(self, local: int, dev_fields: bool):
        b = self.buf
        _reserved = b[self.pos]
        arch = b[self.pos + 1]
        big = arch == 1
        gmn = struct.unpack(">H" if big else "<H", b[self.pos + 2:self.pos + 4])[0]
        self.pos += 4
        n = b[self.pos]
        self.pos += 1
        fields = []
        for _ in range(n):
            num, size, base = b[self.pos], b[self.pos + 1], b[self.pos + 2]
            self.pos += 3
            fields.append((num, size, base & 0x1F, big))
        if dev_fields:
            nd = b[self.pos]
            self.pos += 1
            for _ in range(nd):
                self.pos += 3  # skip developer fields
        self.defs[local] = {"gmn": gmn, "fields": fields}

    def _read_data(self, msg, override_ts=None):
        b = self.buf
        out = {}
        for num, size, base, big in msg["fields"]:
            raw = b[self.pos:self.pos + size]
            self.pos += size
            if len(raw) < size:
                return out
            fmt, _f, _s = BASE_TYPES.get(base, ("B", False, False))
            if base == 7:  # string
                out[num] = raw.split(b"\x00", 1)[0].decode("utf-8", "ignore")
                continue
            n = size // BASE_SIZE.get(fmt, 1)
            if fmt in ("f", "d") or n <= 1:
                v = decode_value(fmt, raw[:BASE_SIZE.get(fmt, size)], big)
                if v is not None:
                    out[num] = v
            else:  # array field: keep first element (enough for our fields)
                v = decode_value(fmt, raw[:BASE_SIZE[fmt]], big)
                if v is not None:
                    out[num] = v
        if override_ts is not None and 253 not in out:
            out[253] = override_ts
        return out

    def _dispatch(self, msg, vals, records, laps, session):
        if msg["gmn"] == RECORD_MSG:
            rec = {}
            for num, (key, scale, offset) in RECORD_SCALE.items():
                v = vals.get(num)
                if v is None or (isinstance(v, int) and num in (0, 1) and v == 0x7FFFFFFF):
                    continue
                if num == 3 and v == 0xFF:
                    continue
                if num == 4 and v == 0xFF:
                    continue
                rec[key] = v * scale + offset
            if "ts" in rec:
                self.prev_ts = vals.get(253, self.prev_ts)
                records.append(rec)
        elif msg["gmn"] == SESSION_MSG:
            session.update(vals)


def fit_session_summary(records, session, sport_hint=None):
    if not records:
        return None
    ts0 = records[0]["ts"]
    ts1 = records[-1]["ts"]
    start = FIT_EPOCH + timedelta(seconds=ts0)
    dur = ts1 - ts0
    dist = max((r.get("dist") or 0) for r in records)
    speeds = [r["speed"] for r in records if r.get("speed")]
    med_v = sorted(speeds)[len(speeds) // 2] if speeds else 0
    sport = sport_hint or ("run" if med_v < 5.0 else "ride")
    hrs = [r["hr"] for r in records if r.get("hr")]
    return {
        "start_utc": E.to_utc(start), "duration_s": float(dur), "distance_m": dist,
        "sport": sport, "avg_hr": sum(hrs) / len(hrs) if hrs else None,
        "max_hr": max(hrs) if hrs else None,
    }


def ingest_fit(conn, path: str, sport_hint=None, force=False):
    fit = FitFile(path)
    records, laps, session = fit.parse()
    summary = fit_session_summary(records, session, sport_hint)
    if not summary or not records:
        print(f"[fit] {os.path.basename(path)}: no usable records", file=sys.stderr)
        return None
    # workout matching against existing sessions
    dup = conn.execute(
        """SELECT workout_id, source FROM workouts
           WHERE sport_type = ? AND abs(epoch(start_time) - ?) <= 600""",
        [summary["sport"], summary["start_utc"].timestamp()]).fetchone()
    if dup and not force:
        E.log_ingest(conn, "duplicate_suppressed", {
            "from": "fit", "file": os.path.basename(path),
            "matched_workout": dup[0], "matched_source": dup[1]})
        print(f"[fit] {os.path.basename(path)}: matches existing workout {dup[0]} "
              f"({dup[1]}) — skipped (use --force to ingest anyway)")
        return dup[0]

    wid = E.make_workout_id("COROS", summary["start_utc"], summary["duration_s"], summary["distance_m"])
    conn.execute("DELETE FROM series WHERE workout_id = ?", [wid])
    conn.execute("DELETE FROM route_points WHERE workout_id = ?", [wid])
    rows_s, rows_r = [], []
    cum = 0.0
    for i, r in enumerate(records):
        ts = E.to_utc(FIT_EPOCH + timedelta(seconds=r["ts"]))
        lat, lon = r.get("lat"), r.get("lon")
        if lat is not None and lon is not None:
            rows_r.append((wid, i, ts, lat, lon, None))
        v = r.get("speed")
        if i and v is None and lat is not None:
            p, q = records[i - 1], r
            dt = r["ts"] - p["ts"]
            if dt > 0 and p.get("lat") is not None:
                v = E.haversine_m(p["lat"], p["lon"], lat, lon) / dt
        cum = max(cum, r.get("dist") or cum)
        rows_s.append((wid, ts, lat, lon, None, v,
                       (1000.0 / v) if v and v > 0.4 else None,
                       r.get("hr"), r.get("cad"), cum if cum else None, "COROS"))
    if rows_r:
        conn.executemany("INSERT INTO route_points VALUES (?,?,?,?,?,?)", rows_r)
    if rows_s:
        conn.executemany(
            "INSERT INTO series (workout_id, ts, lat, lon, alt, speed_mps, pace_s_per_km,"
            " hr, cadence, dist_m, source) VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows_s)
    pace = summary["duration_s"] / (summary["distance_m"] / 1000) if summary["distance_m"] else None
    conn.execute(
        "INSERT OR REPLACE INTO workouts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [wid, summary["sport"], "COROS", "COROS watch", summary["start_utc"],
         summary["start_utc"] + timedelta(seconds=summary["duration_s"]),
         summary["duration_s"], None, summary["distance_m"], pace, None, None, None, None,
         None, summary["avg_hr"], summary["max_hr"], None, None, None, bool(rows_r),
         json.dumps({"file": os.path.basename(path), "laps": len(laps)})])
    conn.commit()
    print(f"[fit] {os.path.basename(path)} → {wid} "
          f"({summary['distance_m'] / 1000:.2f} km, {len(records)} records)")
    return wid


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("files", nargs="+", help=".fit file(s) or glob")
    ap.add_argument("--sport", default=None, help="override sport detection (run/ride/swim)")
    ap.add_argument("--force", action="store_true", help="ingest even if it matches an existing workout")
    ap.add_argument("--db", default=E.DEFAULT_DB)
    args = ap.parse_args()

    paths = []
    for pat in args.files:
        matched = glob.glob(os.path.expanduser(pat))
        paths.extend(matched if matched else [pat])
    conn = E.connect(args.db)
    ok = fail = 0
    for p in paths:
        try:
            if ingest_fit(conn, p, args.sport, args.force):
                ok += 1
            else:
                fail += 1
        except (ValueError, struct.error, IndexError) as e:
            print(f"[fit] {p}: parse failed — {e}", file=sys.stderr)
            fail += 1
    conn.close()
    print(f"[fit] done: {ok} ingested, {fail} failed")
    return 0 if fail == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
