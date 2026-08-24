from __future__ import annotations

import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "scripts")
sys.path.insert(0, SCRIPTS)

import manage_recovery as Recovery  # noqa: E402
import render_card as Render  # noqa: E402


class DocumentationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as f:
            cls.readme = f.read()
        with open(os.path.join(ROOT, "SKILL.md"), encoding="utf-8") as f:
            cls.skill = f.read()

    def test_skill_frontmatter_and_v02_triggers(self):
        self.assertTrue(self.skill.startswith("---\nname: endurance-health-analyst\n"))
        self.assertRegex(self.skill, r"(?m)^version: 0\.2\.0$")
        for phrase in (
            "为什么今天跑起来特别难",
            "Apple Watch 心率靠谱吗",
            "今晚提醒我做跑后放松",
            "用这张照片和 GPS 轨迹生成朋友圈运动卡",
        ):
            self.assertIn(phrase, self.skill)

    def test_readme_starts_from_lived_scenario(self):
        scenario = self.readme.index("明明和上次跑得差不多")
        technical = self.readme.index("它如何避免把一次波动")
        self.assertLess(scenario, technical)
        for heading in ("它会给你什么答案", "记录并提醒跑后放松",
                        "生成一张朋友圈方形分享卡", "隐私与医疗边界"):
            self.assertIn(heading, self.readme)

    def test_documented_cli_matches_real_parsers(self):
        render_args = Render.build_parser().parse_args([
            "--workout", "latest:run", "--photo", "/tmp/run.HEIC", "--privacy", "on"
        ])
        self.assertEqual(render_args.privacy, "on")
        self.assertEqual(render_args.photo, "/tmp/run.HEIC")
        recovery_args = Recovery.build_parser().parse_args([
            "create", "--workout", "latest:run", "--muscles", "calves,quads",
            "--routine", "post_run_basic", "--duration-min", "10",
            "--soreness-before", "5", "--remind-at", "2026-08-24T21:30:00+08:00",
            "--reminder-backend", "macos",
        ])
        self.assertEqual(recovery_args.soreness_before, 5)
        self.assertEqual(recovery_args.reminder_backend, "macos")
        quick_start = re.search(
            r"# 5\. 生成照片分享卡\n(?P<command>.*?--photo.*?--privacy on.*?)```",
            self.readme, re.DOTALL,
        )
        self.assertIsNotNone(quick_start, "quick-start share command must force privacy on")

    def test_demo_asset_is_safe_square_png(self):
        from PIL import Image

        asset = os.path.join(ROOT, "docs", "assets", "workout-card-demo.png")
        self.assertTrue(os.path.exists(asset))
        self.assertGreater(os.path.getsize(asset), 30_000)
        with Image.open(asset) as image:
            self.assertEqual(image.size, (2160, 2160))
            self.assertFalse(image.getexif())
            self.assertNotIn("gps", {str(key).lower() for key in image.info})

    def test_no_claim_to_be_a_doctor(self):
        self.assertIn("不是医生", self.readme)
        self.assertIn("不提供疾病诊断", self.readme)
        self.assertNotRegex(self.readme, re.compile(r"我是.{0,8}医生"))


if __name__ == "__main__":
    unittest.main()
