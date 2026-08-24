on run argv
    if (count of argv) < 1 then error "Missing action" number 64
    set actionName to item 1 of argv

    if actionName is "create" then
        if (count of argv) is not 10 then error "create requires 9 arguments" number 64
        set listName to item 2 of argv
        set reminderName to item 3 of argv
        set reminderBody to item 4 of argv
        set dueDate to current date
        set day of dueDate to 1
        set year of dueDate to (item 5 of argv as integer)
        set month of dueDate to (item 6 of argv as integer)
        set day of dueDate to (item 7 of argv as integer)
        set hours of dueDate to (item 8 of argv as integer)
        set minutes of dueDate to (item 9 of argv as integer)
        set seconds of dueDate to (item 10 of argv as integer)

        tell application "Reminders"
            if not (exists list listName) then error "Reminder list not found: " & listName number 44
            set targetList to list listName
            set newReminder to make new reminder at end of reminders of targetList with properties {name:reminderName, body:reminderBody, due date:dueDate}
            return id of newReminder
        end tell
    end if

    if actionName is "complete" or actionName is "cancel" then
        if (count of argv) is not 2 then error actionName & " requires a reminder id" number 64
        set reminderID to item 2 of argv
        tell application "Reminders"
            repeat with reminderList in lists
                set matches to every reminder of reminderList whose id is reminderID
                if (count of matches) > 0 then
                    set targetReminder to item 1 of matches
                    if actionName is "complete" then
                        set completed of targetReminder to true
                        return id of targetReminder
                    end if
                    set deletedID to id of targetReminder
                    delete targetReminder
                    return deletedID
                end if
            end repeat
        end tell
        -- Terminal actions are idempotent: an absent reminder may mean a prior
        -- invocation succeeded before the caller persisted its local state.
        return "not-found"
    end if

    error "Unsupported action: " & actionName number 64
end run
