---
description: List, close or inspect what this agent session opened (servers, tabs, simulators, temp files)
argument-hint: "[list | status | close <id>|--all-mine | keep <id> | release <id>]"
allowed-tools: Bash(leavenotrace:*)
---

Run the LeaveNoTrace CLI with the Bash tool: `leavenotrace $ARGUMENTS`
(when no arguments were given use `leavenotrace list`; if `leavenotrace` is not on PATH use
`python3 "${CLAUDE_PLUGIN_ROOT}/bin/leavenotrace"`).

Show the user the output. Then:

- For `close`: if the output contains lines saying `agent must close it:` for browser tabs, perform
  exactly those browser-tool calls (hooks cannot close MCP browser tabs), then run `leavenotrace list` to confirm
  nothing is left open.
- `list` and `status` are read-only. `keep <id>` protects a resource from the idle sweep; `release <id>` undoes it.
- Never kill processes by name or port to "help"; only registered ids are closed.
