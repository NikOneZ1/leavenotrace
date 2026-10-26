#!/usr/bin/env bash
# Before/after demo: leftover dev servers the old way vs with LeaveNoTrace.
#   scripts/demo.sh [N]        (default N=5; needs python3; Linux or macOS)
# Everything runs in a throw-away state dir and only ever touches processes this script starts.
set -u
N="${1:-5}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
CLI="$HERE/bin/leavenotrace"
WORK="$(mktemp -d)"; export LEAVENOTRACE_HOME="$WORK/state"; mkdir -p "$WORK/proj"
export LEAVENOTRACE_OWNER_PID=$$        # this script plays the role of the agent process
export LEAVENOTRACE_GRACE_SECONDS=1
SID="demo-$$"
BASEPORT=$(python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()')
OLD_PIDS=()

count_listening() {  # how many of the demo ports accept connections
  python3 - "$BASEPORT" "$N" <<'PY'
import socket, sys
base, n = int(sys.argv[1]), int(sys.argv[2])
c = 0
for p in range(base, base + n):
    try:
        socket.create_connection(("127.0.0.1", p), 0.2).close(); c += 1
    except OSError:
        pass
print(c)
PY
}
wait_listening() {  # wait until $1 demo servers listen
  for _ in $(seq 1 80); do [ "$(count_listening)" -ge "$1" ] && return 0; sleep 0.1; done
}
hook() {  # hook <event-name> <cli-event> <json-builder args...>
  python3 - "$@" <<'PY' | python3 "$CLI" hook "$2"
import json, os, sys
ev, _cli, tool, extra = sys.argv[1], sys.argv[2], sys.argv[3], json.loads(sys.argv[4])
p = {"session_id": os.environ.get("DEMO_SID"), "cwd": os.environ.get("DEMO_CWD"), "hook_event_name": ev}
p.update(extra)
if tool: p["tool_name"] = tool
print(json.dumps(p))
PY
}
export DEMO_SID="$SID" DEMO_CWD="$WORK/proj"

echo "=== BEFORE: the old way (agent starts $N dev servers, then the session ends) ==="
cd "$WORK/proj"
for i in $(seq 0 $((N-1))); do
  nohup python3 -m http.server $((BASEPORT+i)) --bind 127.0.0.1 >/dev/null 2>&1 &
  OLD_PIDS+=($!); disown $!
done
wait_listening "$N"
echo "servers running after the 'agent' finished its work : $(count_listening) of $N   (nobody will close them)"
echo "...session ends. Servers still running                : $(count_listening)"
for p in "${OLD_PIDS[@]}"; do kill "$p" 2>/dev/null; done   # demo hygiene only
sleep 1
echo "(demo cleaned its own leftovers: now $(count_listening))"
echo

echo "=== AFTER: the same work with LeaveNoTrace hooks ==="
hook SessionStart session-start "" '{"source":"startup"}' >/dev/null
for i in $(seq 0 $((N-1))); do
  PORT=$((BASEPORT+i)); CMD="nohup python3 -m http.server $PORT --bind 127.0.0.1 >/dev/null 2>&1 &"
  hook PreToolUse pre-tool-use Bash "{\"tool_use_id\":\"t$i\",\"tool_input\":{\"command\":\"$CMD\"}}" >/dev/null
  (cd "$WORK/proj" && nohup python3 -m http.server $PORT --bind 127.0.0.1 >/dev/null 2>&1 &)
  wait_listening $((i+1))
  hook PostToolUse post-tool-use Bash "{\"tool_use_id\":\"t$i\",\"tool_input\":{\"command\":\"$CMD\"},\"tool_response\":{}}" >/dev/null
done
echo "servers running (all registered)        : $(count_listening) of $N"
echo "--- registry (leavenotrace list) ---"
python3 "$CLI" list --session "$SID"
echo "--- the agent tries to start a duplicate on port $BASEPORT ---"
hook PreToolUse pre-tool-use Bash "{\"tool_use_id\":\"dup\",\"tool_input\":{\"command\":\"python3 -m http.server $BASEPORT &\"}}" \
  | python3 -c 'import json,sys; o=json.load(sys.stdin)["hookSpecificOutput"]; print(o["permissionDecision"].upper()+": "+o["permissionDecisionReason"])'
echo "--- session ends (SessionEnd hook, 1.5 s budget) ---"
START=$(python3 -c 'import time;print(time.time())')
hook SessionEnd session-end "" '{"reason":"other"}'
END=$(python3 -c 'import time;print(time.time())')
python3 -c "print('hook returned after %.2f s' % ($END - $START))"
for _ in $(seq 1 60); do [ "$(count_listening)" -eq 0 ] && break; sleep 0.1; done
echo "servers still running after session end : $(count_listening)"
echo
echo "summary:  old way leaves $N processes behind;  with LeaveNoTrace: $(count_listening)"
rm -rf "$WORK"
