---
name: cleanup
description: Resource hygiene for anything you open while working - background processes and dev servers, browser tabs (Playwright / Chrome DevTools MCP), iOS simulators, temp files. Use before starting a dev server, opening a browser tab or booting a simulator, when a hook says something is already open, and when a task is finished and you must close what you opened.
---

# LeaveNoTrace: close what you open, reuse what is open

A per-session registry records what **this session** opened (dev servers and other background
processes, browser tabs, iOS simulators you booted, temp files you wrote). Only those things are ever
closed. Things the user started are never touched. The CLI is `leavenotrace` (on PATH when the plugin is
enabled; otherwise use `python3 "${CLAUDE_PLUGIN_ROOT}/bin/leavenotrace"` or the exact command printed in the
session-start context).

## Before you open anything

1. Run `leavenotrace list`. It shows ids, ports, tab ids/URLs and simulator UDIDs you already own.
2. **Reuse** what is there:
   - dev server already running -> use its port, do not start another (restart only if you must:
     `leavenotrace close <id>`, then start again);
   - tab already open on that URL -> select it and navigate/reload instead of opening a new tab;
   - simulator already booted (even one you did not boot) -> install/launch on that UDID.
3. If a hook **denies** a start with "reuse ..." take that as the answer. Only add `# leavenotrace:force`
   to a command when you really need a second instance (different config, different device type).
4. Need a scratch file? Write it under `leavenotrace tmpdir` (a per-session dir that is deleted at session
   end) or under `.agent-tmp/`; files created in /tmp are tracked and removed too.

## While you work

- Keep things alive while upcoming steps need them: `leavenotrace keep <id>` (skips the idle sweep).
  `leavenotrace release <id>` undoes it. Idle resources (default 30 min without being referenced) are
  closed automatically after a turn ends, so keep long-lived servers you still need.
- Do not kill servers with `pkill`, `killall node` or `kill $(lsof -ti :PORT)`. Use
  `leavenotrace close <id>`; it verifies PID + start time and only touches what you opened.

## When the task is finished

1. Run `leavenotrace close --all-mine` (or `leavenotrace close <id> [<id>...]` for specific ones).
2. It closes processes, simulators you booted and temp files itself. **Browser tabs cannot be closed from
   outside the browser tool**, so for each line that says `agent must close it:` call the printed
   browser-tool action yourself (e.g. `mcp__playwright__browser_tabs {"action":"close","index":2}`,
   `mcp__chrome-devtools__close_page {"pageId":2}`, or `browser_close` for the whole Playwright browser).
   The registry marks the tab closed automatically when that call succeeds.
   With Claude in Chrome: ungroup the tab from its tab group before closing it.
3. Run `leavenotrace list` once more; it should show nothing open.

Also available: `leavenotrace status`, `leavenotrace register --type process --pid N` (adopt a process you
started some other way), `leavenotrace forget <id>` (stop tracking without closing), `leavenotrace doctor`.
