"""Hook payloads captured from a real Claude Code 2.1.280 run (anonymised fixture), replayed against real processes."""
import json
import os
import subprocess
import sys
import unittest

from helpers import Base, CLI, free_port, wait_dead, wait_port, pid_alive

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "claude_code_2_1_280_hooks.json")
EVENT = {"SessionStart": "session-start", "PreToolUse": "pre-tool-use", "PostToolUse": "post-tool-use", "Stop": "stop",
         "SessionEnd": "session-end"}


class RealClaudeCodePayloads(Base):
    def real(self, name, port=None):
        with open(FIX) as f:
            raw = json.dumps(json.load(f)[name])
        raw = raw.replace("{SID}", self.sid).replace("{CWD}", self.work).replace("{PORT}", str(port or 0))
        payload = json.loads(raw)
        r = self.run_cli("hook", EVENT[payload["hook_event_name"]], input=raw)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr, "")
        r.json = json.loads(r.stdout) if r.stdout.strip() else None
        return r

    def start_server(self, port):
        return self.spawn([sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"])

    def test_run_in_background_flow_with_real_payloads(self):
        port = free_port()
        self.real("session_start", port)
        self.assertIsNone(self.real("pre_bg", port).json)
        srv = self.start_server(port)
        self.assertTrue(wait_port(port))
        post = self.real("post_bg", port)
        ctx = post.json["hookSpecificOutput"]
        self.assertEqual(ctx["hookEventName"], "PostToolUse")
        self.assertIn("registered proc-1", ctx["additionalContext"])
        procs = self.resources("process", "open")
        self.assertEqual([(p["pid"], p["port"]) for p in procs], [(srv.pid, port)])
        dup = self.real("pre_bg_dup", port).json["hookSpecificOutput"]
        self.assertEqual(dup["hookEventName"], "PreToolUse")
        self.assertEqual(dup["permissionDecision"], "deny")
        self.assertIn("proc-1", dup["permissionDecisionReason"])
        self.assertIn("http://localhost:%d" % port, dup["permissionDecisionReason"])
        self.assertIsNone(self.real("stop", port).json)           # Stop with background_tasks never closes fresh things
        self.assertTrue(pid_alive(srv.pid))
        self.real("session_end", port)
        self.assertTrue(wait_dead(srv.pid))
        self.assertTrue(self.registry()["ended"])
        self.assertEqual(self.resources("process", "closed")[0]["pid"], srv.pid)

    def test_nohup_ampersand_flow_with_real_payloads(self):
        port = free_port()
        self.real("session_start", port)
        self.assertIsNone(self.real("pre_nohup", port).json)
        srv = self.start_server(port)
        self.assertTrue(wait_port(port))
        self.assertIn("registered proc-1", self.real("post_nohup", port).json["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(self.real("pre_nohup_dup", port).json["hookSpecificOutput"]["permissionDecision"], "deny")
        self.real("session_end", port)
        self.assertTrue(wait_dead(srv.pid))

    def test_unrelated_server_on_same_port_is_never_touched(self):
        """A server the user started on the port before the agent's command must not be claimed or killed."""
        port = free_port()
        user_srv = self.start_server(port)
        self.assertTrue(wait_port(port))
        self.real("session_start", port)
        pre = self.real("pre_bg", port).json["hookSpecificOutput"]   # unowned listener -> a note, never a denial
        self.assertNotIn("permissionDecision", pre)
        self.assertIn("NOT owned", pre["additionalContext"])
        self.real("post_bg", port)
        self.assertEqual(self.resources("process", "open"), [])
        self.real("session_end", port)
        self.assertTrue(pid_alive(user_srv.pid))


if __name__ == "__main__":
    unittest.main()
