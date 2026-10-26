#!/bin/bash
# Live test of the plugin inside a REAL Claude Code binary, fully isolated and without using anyone's Claude account:
# the model is replaced by scripts/live/mock_api.py (ANTHROPIC_BASE_URL + dummy key), HOME and the LeaveNoTrace state dir
# are throwaway, the plugin is loaded for this one session via --plugin-dir. Needs: claude, python3. Takes ~10 s.
# usage: scripts/live/run_live.sh [bg|nohup]
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
MODE="${1:-bg}"; W="$(mktemp -d /tmp/lnt-live.XXXXXX)"; PORT=18080; SRV=8765
if [ "$MODE" = nohup ]; then
  echo "[{\"command\":\"nohup python3 -m http.server $SRV --bind 127.0.0.1 >/dev/null 2>&1 &\"},{\"command\":\"nohup python3 -m http.server $SRV --bind 127.0.0.1 >/dev/null 2>&1 & \"}]" > "$W/script.json"
else
  echo "[{\"command\":\"python3 -m http.server $SRV --bind 127.0.0.1\",\"run_in_background\":true},{\"command\":\"python3 -m http.server $SRV --bind 127.0.0.1\",\"run_in_background\":true}]" > "$W/script.json"
fi
python3 "$ROOT/scripts/live/mock_api.py" $PORT "$W/script.json" & MOCK=$!
trap 'kill $MOCK 2>/dev/null; true' EXIT
sleep 1; mkdir -p "$W/home" "$W/proj"; cd "$W/proj"
HOME="$W/home" ANTHROPIC_BASE_URL=http://127.0.0.1:$PORT ANTHROPIC_API_KEY=sk-ant-api03-mock-not-real \
  CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 LEAVENOTRACE_HOME="$W/home/lnt" LEAVENOTRACE_DEBUG_PAYLOADS="$W/payloads.jsonl" \
  python3 - "$W" "$ROOT" <<'PY'
import subprocess, sys, os, signal
w, root = sys.argv[1:3]
p = subprocess.Popen(["claude", "-p", "go", "--model", "haiku", "--plugin-dir", root, "--setting-sources", "project,local",
                      "--strict-mcp-config", "--tools", "Bash", "--permission-mode", "bypassPermissions", "--no-session-persistence",
                      "--output-format", "stream-json", "--verbose", "--include-hook-events"],
                     stdout=open(w + "/out.jsonl", "w"), start_new_session=True)
try:
    p.wait(timeout=120)
except subprocess.TimeoutExpired:
    os.killpg(p.pid, signal.SIGKILL); print("TIMEOUT")
PY
sleep 4
echo "== hook outputs / tool results"; python3 - "$W/out.jsonl" <<'PY'
import json, sys
for l in open(sys.argv[1]):
    o = json.loads(l)
    if o.get("type") == "system" and o.get("subtype") == "hook_response" and o.get("output"):
        print(o["hook_event"], o["output"][:200].replace("\n", " "))
    elif o.get("type") == "user":
        print("tool_result:", json.dumps(o["message"]["content"])[:200])
PY
echo "== server processes still alive (expect none):"; pgrep -fl "http.server $SRV --bind" || echo none
echo "== registry:"; cat "$W/home/lnt/sessions/"*.json | python3 -c "import json,sys; d=json.load(sys.stdin); print('ended', d.get('ended')); [print(r['id'], r['state'], r.get('note')) for r in d['resources'].values()]"
echo "artifacts in $W (payloads.jsonl = raw hook payloads)"
