# LeaveNoTrace

> Coding agents should **close what they open** (dev servers, browser tabs, iOS simulators, temp files) and
> **reuse what is already open** instead of opening duplicates - and must never touch anything they did not open.

LeaveNoTrace is a Claude Code plugin (plus a dependency-free CLI and thin adapters for other agents). It keeps a
per-session registry of what *this agent session* started, denies duplicate starts with a "reuse this" message,
sweeps idle things, and closes everything the session owns when the task or session ends - with identity checks
(PID **and** process start time) before every kill.

*Working name.* The product name lives in one constant block (see [Renaming](#renaming)).

* Primary target and the only thing exercised by the test-suite: **Claude Code hooks on Linux**.
* v1 status: tested against simulated hook payloads and real child processes; **not yet run inside a live Claude
  Code session** (see [Verified vs unverified](#verified-vs-unverified)).

## Why

Agents leave a trail: `next dev` servers that grow to GBs, headless browsers, simulators, tmp files, tabs.
Upstream evidence (all four issues are now closed upstream, which does not mean the underlying behaviour
stopped existing for every setup - several were closed with workarounds/partial fixes):

| Issue | What it shows |
|---|---|
| [anthropics/claude-code#43944](https://github.com/anthropics/claude-code/issues/43944) | Background processes started by the Bash tool (dev servers, builds) are not cleaned up on session exit; they reparent to PID 1 and keep growing (e.g. orphaned `next dev` at 8+ GB). **This is the core case LeaveNoTrace targets.** |
| [#33947](https://github.com/anthropics/claude-code/issues/33947) | MCP-server and subagent processes not cleaned up on session end (macOS, PPID=1 orphan accumulation). |
| [#79567](https://github.com/anthropics/claude-code/issues/79567) | MCP server processes outlive the session, each burning ~90% of a core. |
| [#15436](https://github.com/anthropics/claude-code/issues/15436) | "Claude in Chrome" tab groups pile up after sessions end. |

Be precise about scope: #33947 and #79567 are about *MCP servers/subagents the harness itself spawns*.
LeaveNoTrace does **not** try to reap those (that is what the tools below do). It targets the resources the
*agent* opens through tools - and, for Playwright/DevTools MCP, the browser processes launched under the agent.

### How it differs from existing tools

| Tool | Approach | Difference |
|---|---|---|
| [claude-code-shepherd](https://github.com/mgorkemuz/claude-code-shepherd) | Tracks Bash-spawned processes per session, stash/unstash, orphan cleanup on next launch | Closest relative. No browser tabs / simulators / temp files, no reuse enforcement (it warns about port conflicts), no idle-TTL. |
| [cc-reaper](https://github.com/theQuert/cc-reaper) | Kills orphaned subagents/MCP servers/stale browsers (PGID & PPID=1 heuristics), daemon fallback | Reactive pattern/orphan cleanup; no ownership registry, no reuse. |
| [process-guardian](https://github.com/ibarapascal/process-guardian) | Allow-list of known orphan patterns + session tracking of PPID=1 diffs, triple verification (pid + start + command) | Orphan-oriented; no browser tabs, simulators, reuse denial or keep-alive model. |
| [qazaqninja/claude-cleanup](https://github.com/qazaqninja/claude-cleanup) | **A different project with a similar name.** Shell hooks + `/cleanup` skill killing PPID=1 leftovers | Heuristic orphan sweep after the fact. |
| [mcp-reap](https://github.com/Caarlosgg/mcp-reap) | Node CLI that detects/cleans orphaned MCP servers for Claude/Cursor/Codex/Aider/Gemini, dry-run by default | MCP-server orphans only; ours is about what the agent opened itself. |
| superclean ([albertdobmeyer/superclean](https://github.com/albertdobmeyer/superclean)) | Fail-closed orphan diagnosis/cleanup with tiers and confirmation | Cleans leftovers from the machine's point of view; ours is the agent's own ownership ledger + prevention (reuse). |

Common thread: those tools clean *orphans after the damage is done* by pattern/PPID. LeaveNoTrace
(1) records ownership at open time, (2) prevents duplicates up front, (3) keeps things alive while the task needs
them and closes them when it ends. They compose: run one of them for harness-spawned MCP orphans, this for
agent-opened resources. (Row descriptions are summarized from each repo's public description/README at time of writing; I did not install or test these tools, and the claude-code-shepherd row is the least verified - the repo has no description field.)

## Install

**Plugin (recommended)** - from a local checkout:

```bash
claude --plugin-dir /path/to/leavenotrace          # try it for one session
# or add to a marketplace you control and: /plugin install leavenotrace@<marketplace>
```

The plugin's `bin/` directory is on the Bash tool's PATH, so the agent can run `leavenotrace ...` directly.
Hooks come from `hooks/hooks.json`, the skill is `leavenotrace:cleanup`, the slash command is
`/leavenotrace:leavenotrace [list|status|close ...]`.

**Manual (no plugin)** - writes the same hooks into `~/.claude/settings.json` (backup kept, idempotent):

```bash
python3 /path/to/leavenotrace/bin/leavenotrace install            # add --copy to copy the script to ~/.leavenotrace/bin
python3 /path/to/leavenotrace/bin/leavenotrace uninstall          # removes only its own entries
```

Do not enable both (hooks would run twice). `leavenotrace doctor` checks the environment.
Requires Python >= 3.8 (stdlib only); Linux or macOS (`ps`/`lsof` on macOS, `/proc` on Linux).

**Other agents** (thin adapters, **untested**): `adapters/AGENTS.md.snippet` (Codex CLI, Gemini CLI, Cursor,
anything reading AGENTS.md), `adapters/codex/hooks.json`, `adapters/gemini/settings.hooks.json`,
`adapters/cursor/README.md`. They use `leavenotrace register/close` and, where the agent has hooks, the same hook handler.

## How it works

State lives in `~/.leavenotrace/` (override with `LEAVENOTRACE_HOME`):

```
sessions/<session_id>.json   registry (atomic temp-file+rename writes, flock for read-modify-write)
tmp/<session_id>/            per-session temp dir (`leavenotrace tmpdir`), deleted at session end
config.json                  optional settings (see below)
leavenotrace.log             hook diagnostics (hooks fail open and log instead of breaking the agent)
```

Each resource records: type, id, pid, **process start time token**, recorded child pids (with start tokens),
command, cwd, port(s), url/tab ref, simulator UDID + `owned`, `opened_at`, `last_used_at`, `keep_alive`, state.

| Hook | What it does |
|---|---|
| **SessionStart** | Creates the session record (owner = the agent process found by walking ancestors, or `LEAVENOTRACE_OWNER_PID`), crash recovery for *other* sessions whose owner process is dead (hands them to a detached cleaner), creates the temp dir, exports `LEAVENOTRACE_SESSION_ID`/`LEAVENOTRACE_TMPDIR` via `CLAUDE_ENV_FILE`, injects `additionalContext` with the rules and the currently owned resources. |
| **PreToolUse** (`Bash`, `Write`, browser/simulator MCP tools) | *Reuse enforcement.* Denies (`permissionDecision: "deny"` + reason with id/port/tab/UDID) when you start a server on a registered live port or with the same normalized command+cwd, open a tab for a URL already open in a tab this session opened, or boot a simulator while any simulator is booted (owned or not). Override: `# leavenotrace:force` in the command (or `#leavenotrace:force` in a URL fragment). Also snapshots pre-state (booted simulators, processes already matching the command, whether a file already existed) and refreshes `last_used_at` for anything the call references (ports, URLs, UDIDs). |
| **PostToolUse** (same matcher) | Registers what the agent opened: Bash dev servers/background processes (process tree + listening ports discovered from `/proc` or `lsof`), Playwright / Chrome DevTools / Claude-in-Chrome tabs parsed from the tool response, browser processes launched under the agent, simulators that were *not* booted before, `Write`-created files under temp roots. Emits a one-line `additionalContext` with the new ids. |
| **Stop** (every turn) | Cheap idle sweep only: resources unreferenced for `idle_ttl_minutes` (default 30; not `keep_alive`) are handed to a detached cleaner; idle tabs are marked `pending_close` and the agent is asked **once** (via `additionalContext`, never when `stop_hook_active`) to close them. It never closes "everything". Temp files and the temp dir are excluded from the TTL. |
| **SessionEnd** | Hands everything the session owns to a **detached cleaner** (`setsid`), waits up to 0.6 s for it, then returns - well inside Claude Code's 1.5 s budget; the cleaner keeps going after the hook returns. |

CLI (`leavenotrace`): `list`, `status`, `close [id...|--all-mine|--session X] [--except-kept] [--dry-run]`, `keep`, `release`,
`forget`, `sweep`, `doctor`, `register`, `tmpdir`, `install`, `uninstall`. The session is auto-detected from
`LEAVENOTRACE_SESSION_ID`, then from the agent process in the CLI's ancestry, then from cwd; otherwise it refuses.

Config (`~/.leavenotrace/config.json` or env `LEAVENOTRACE_<KEY>`): `idle_ttl_minutes` (30, `0` = off),
`grace_seconds` (3), `stop_nudge` (true), `session_end_wait_ms` (600), `port_wait_seconds` (2), `orphan_hours` (24),
`prune_days` (7).

### Done = `close --all-mine`
The skill (`skills/cleanup/SKILL.md`) tells the agent to check `leavenotrace list` before opening anything, reuse,
`keep` things upcoming steps need, and run `leavenotrace close --all-mine` when finished.

## Safety model

* **Ownership, not heuristics.** Only resources in this session's registry are ever closed. Nothing is found by
  scanning for names (`pkill node`), ports (`kill $(lsof -ti :3000)`) or `simctl shutdown all`; the source is audited by a
  test for `killpg`, `shutdown all`, `lsof -t` and for a single `os.kill` call site.
* **Identity re-verified at kill time.** Registered PID *and* process start token (Linux: boot id + `/proc/<pid>/stat`
  starttime; macOS: `ps lstart`) must match. Any mismatch (PID reuse), missing start time, or unreadable state => **refuse the
  whole entry** (state `refused`), nothing is killed. Identity is re-checked immediately before each signal.
* **Protected set.** Never kills the current process, its ancestors, the session's owner/agent process, anything that looks like
  `claude`/`codex`/`gemini`, PIDs 0/1 - even if (mis)registered. Cleaners spawned by hooks get the hook's ancestors passed in
  (`--protect`) because detaching changes their ancestry.
* **Tree, not group.** SIGTERM to the registered process, its live descendants and recorded children (each verified individually),
  leaves first; wait `grace_seconds`; re-verify, then SIGKILL survivors. No process-group kills (they could include the agent).
* **Claiming is conservative.** A Bash-started process is only registered if it did not exist before the command (pre-snapshot),
  matches the command's tokens, is not registered elsewhere, and is a descendant of the agent process *or* has its cwd in the
  command's directory. Ambiguous simulator boots are not claimed. A browser process is only registered if it is a descendant of
  the agent process (MCP servers are its children) and its command line is a known automation browser (`ms-playwright`, ...).
  The implicit first Playwright tab is only claimed when such an owned browser was detected (so attach/extension modes - where the
  tab is the user's - are never claimed).
* **Simulators.** Only `owned=true` ones are shut down (`xcrun simctl shutdown <udid>`, UDID format-checked). Reuse applies to any
  booted simulator; non-owned ones are never shut down.
* **Temp files.** Deleted only if the path is a strict descendant of a temp root (`/tmp`, `/var/tmp`, `$TMPDIR`, the state dir's
  `tmp/`) or under a `.agent-tmp/` directory, it was *created* by the agent (pre-existing files are never registered), is not
  inside the agent's own scratchpad, and is not a directory; symlinks are unlinked, not followed. The session temp dir is removed
  only if it is exactly the expected path and not a symlink.
* **Hooks fail open, kills fail closed.** An internal error in a hook logs and exits 0 (never blocks the agent); anything
  uncertain on the kill path refuses.

## Browser tabs: limitations (read this)

A hook is a shell command; it **cannot call MCP tools**. So LeaveNoTrace cannot close a Playwright / Chrome DevTools /
Claude-in-Chrome tab itself. What it does instead:

1. Registers tabs (ref = Playwright index / DevTools `pageId` / Chrome `tabId`, URL) from tool responses; formats differ across
   server versions (0- vs 1-based indexes, tab lines with or without URL), so parsing is best-effort and degrades to
   "registered with URL, unknown ref" - then the instructions say *"list tabs and close the one with URL X"*.
2. `close --all-mine` (and the Stop-hook nudge for idle tabs) marks tabs `pending_close` and prints the exact tool call, e.g.
   `mcp__playwright__browser_tabs {"action":"close","index":2}` or `mcp__chrome-devtools__close_page {"pageId":2}`. The agent must make
   that call; the PostToolUse hook then marks the entry closed.
3. If the browser process is a registered child of the agent (Playwright/DevTools launch their own Chromium), it is killed **by PID**
   (safely, as above) and its tabs are marked closed - this is the only fully automatic browser cleanup.
4. At SessionEnd Claude Code **discards hook output**, so a SessionEnd hook cannot ask the agent to close anything (the session is
   over anyway); leftover tabs are marked `orphaned`. Only registered browser processes die.
5. Claude in Chrome: tabs live in a Chrome tab group; closing a grouped tab can leave an empty group chip behind
   ([#15436](https://github.com/anthropics/claude-code/issues/15436)). Ungroup before closing; that part is upstream and only documented here.

## Demo

```bash
scripts/demo.sh 5     # 5 servers: old way leaves 5 behind, with LeaveNoTrace 0 - plus a denied duplicate start
```

## Tests

```bash
python3 -m unittest discover -s tests -v        # stdlib only, spawns real processes (http.server, sleep, ...)
```

Covers: registration of real background servers (ports, start tokens), duplicate denial + force override, idle TTL (env/config),
Stop hook is cheap/never closes fresh things, SessionEnd budget + detached cleaner, SessionStart crash recovery (dead vs live owner),
kill safety (same-port stranger, PID reuse/start-time mismatch, ancestors, agent process, tampered registries, symlinks),
browser tab parsing (3 formats + garbage), reuse denial for tabs, simulators with a mocked `xcrun`, temp files, install/uninstall,
rename, concurrency of registry writes, fail-open hooks.

## Verified vs unverified

**Verified (by the test-suite, Linux):** everything in the Tests list above - 99 tests (one of them re-runs the whole suite in a renamed copy), 0 failures.
**Verified against documentation only:** hook stdin/stdout JSON formats, `permissionDecision: "deny"` shape, `additionalContext`
fields, SessionEnd budget/discarded output, Stop `additionalContext` continuing the turn, plugin layout (`.claude-plugin/plugin.json`,
`hooks/hooks.json`, `bin/` on PATH), `${CLAUDE_PLUGIN_ROOT}` (code.claude.com/docs/en/hooks and /plugins-reference, fetched 2026-10-26).

**Unverified - treat as such:**
* A real Claude Code session (hooks firing in practice, real `run_in_background` timing, real `tool_response` shapes, how
  quickly Bash-started servers get reparented). `claude plugin validate` was not run (no `claude` binary in the build environment).
* macOS: code paths for `ps lstart`/`lsof` exist but were never executed.
* Real Playwright MCP / Chrome DevTools MCP / Claude-in-Chrome responses: parsers are written from public source/docs and unit-tested
  with synthetic strings; the Claude-in-Chrome handling is a generic guess (tab id regex, ungroup note).
* Real iOS simulators (`xcrun` is mocked); `xcodebuild`-implicit boots and `open -a Simulator` are not detected.
* Codex / Gemini hook configs and `--agent` name mapping (docs-derived), Cursor guidance.
* Servers that daemonize/double-fork away from their registered tree after the last refresh escape cleanup.
* Windows: unsupported.
* Processes started outside the Bash tool (other MCP tools, `docker compose up`, IDE terminals) are not seen unless registered manually.

## Renaming

Everything name-related is the `IDENTITY` block at the top of `bin/leavenotrace`:

```python
PLUGIN_NAME = "leavenotrace"; DISPLAY_NAME = "LeaveNoTrace"; CLI_NAME = "leavenotrace"
STATE_DIRNAME = ".leavenotrace"; ENV_PREFIX = "LEAVENOTRACE"; FORCE_MARKER = "leavenotrace:force"
```

Code uses only those constants. The remaining occurrences are static files (plugin manifest, hooks.json, skill, command, docs, tests),
so to rename the whole project in one go:

```bash
python3 scripts/rename.py --name tidyup --display TidyUp     # also renames bin/leavenotrace and commands/leavenotrace.md
python3 -m unittest discover -s tests                         # the renamed copy passes the same tests (a test proves it)
```

(Plugin names starting with `claude-`/`anthropic-` are reserved by Claude Code.)

## Layout

```
.claude-plugin/plugin.json  hooks/hooks.json  skills/cleanup/SKILL.md  commands/leavenotrace.md
bin/leavenotrace            (the CLI + hook handlers; single file)
adapters/                   AGENTS.md snippet, Codex + Gemini hook examples (untested), Cursor notes
scripts/                    demo.sh  rename.py  package.py      tests/   LICENSE (MIT)
```
