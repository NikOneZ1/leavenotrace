# Cursor adapter (instructions only - no hooks, nothing here is tested)

Cursor is supported through instructions plus the CLI, not through hooks (no Cursor hook integration is
implemented in v1).

1. Put `leavenotrace/bin` on your PATH (or call it by absolute path).
2. Copy `../AGENTS.md.snippet` into the repo's `AGENTS.md` (Cursor reads it) or into a rule file,
   e.g. `.cursor/rules/leavenotrace.mdc` with front matter `alwaysApply: true`.
3. The agent then registers things it opens with `leavenotrace register ...` and closes them with
   `leavenotrace close --all-mine`. Because Cursor's agent is not detected as a session owner, pass
   `--session <any-stable-id>` consistently (e.g. `--session cursor-$(basename "$PWD")`) or export
   `LEAVENOTRACE_SESSION_ID`.
4. There is no automatic crash recovery for Cursor sessions unless you run `leavenotrace sweep` yourself:
   sessions registered without a detectable owner pid are only swept after 24 h (`orphan_hours`).
