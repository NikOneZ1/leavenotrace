import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest

from helpers import Base, CLI, ROOT, load_module, wait_dead, pid_alive, free_port, wait_port


class Analyzer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = load_module()

    def cands(self, cmd, cwd="/base", bg=False):
        return [s for s in self.m.analyze_bash(cmd, cwd, bg) if s["candidate"]]

    def test_background_and_dev_server_detection(self):
        c = self.cands("npm run dev &")
        self.assertEqual([s["norm"] for s in c], ["npm run dev"])
        c = self.cands("cd web && pnpm dev --port 4000 &")
        self.assertEqual((c[0]["cwd"], c[0]["port"]), ("/base/web", 4000))
        c = self.cands("PORT=3001 nohup node server.js > /tmp/log 2>&1 &")
        self.assertEqual((c[0]["norm"], c[0]["port"]), ("node server.js", 3001))
        c = self.cands("python3 -m http.server 8000")
        self.assertEqual((c[0]["norm"], c[0]["port"]), ("python3 -m http.server 8000", 8000))
        self.assertTrue(self.cands("next dev -p 3100 &"))
        self.assertEqual(self.cands("next dev -p 3100")[0]["port"], 3100)
        self.assertTrue(self.cands("yarn start"))
        self.assertTrue(self.cands("whatever --serve", bg=True), "run_in_background makes any command a candidate")

    def test_conservative_negatives(self):
        for cmd in ("echo 'a & b' && ls", "sleep 5 && echo done", "git commit -m 'wip &'", "make test 2>&1 | tee out",
                    "npm test", "tail -f log &", "ls > out.txt", "grep foo a.txt && cat a.txt", "cd /tmp && ls"):
            self.assertEqual(self.cands(cmd), [], cmd)

    def test_chain_background_flag(self):
        segs = self.m.split_segments("a && b | c & d ; e")
        self.assertEqual([(t, bg) for t, term, bg in segs], [("a", True), ("b", True), ("c", True), ("d", False), ("e", False)])

    def test_redirections_not_treated_as_background(self):
        segs = self.m.split_segments("cmd 2>&1 >out &>all.txt")
        self.assertEqual(len(segs), 1)
        self.assertFalse(segs[0][2])

    def test_simctl_boot_parse(self):
        s = self.m.analyze_bash("xcrun simctl boot 'iPhone 15'", "/")[0]
        self.assertTrue(s["sim_boot"])
        self.assertEqual(s["sim_target"], "iPhone 15")
        self.assertTrue(self.m.analyze_bash("xcrun simctl bootstatus ABC -b", "/")[0]["sim_boot"])
        self.assertFalse(self.m.analyze_bash("xcrun simctl list devices", "/")[0]["sim_boot"])
        self.assertFalse(self.m.analyze_bash("xcrun simctl install booted app.app", "/")[0]["sim_boot"])

    def test_ports_and_urls(self):
        self.assertEqual(self.m.ports_in_text("curl http://localhost:3000/x && PORT=4000 node a http://127.0.0.1:5173"), {3000, 4000, 5173})
        n = self.m.norm_url
        self.assertEqual(n("http://127.0.0.1:3000/a/"), n("HTTP://localhost:3000/a#x"))
        self.assertNotEqual(n("http://localhost:3000/a"), n("http://localhost:3000/b"))


class Install(Base):
    def test_install_uninstall_preserves_user_hooks_and_is_idempotent(self):
        sp = os.path.join(self.tmp, "settings.json")
        mine = {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "echo user-hook"}]}]},
                "permissions": {"allow": ["Bash(ls)"]}}
        json.dump(mine, open(sp, "w"))
        for _ in range(2):
            r = self.run_cli("install", "--settings", sp)
            self.assertEqual(r.returncode, 0, r.stderr)
        cfg = json.load(open(sp))
        self.assertEqual(cfg["permissions"], mine["permissions"])
        for ev in ("PreToolUse", "PostToolUse", "Stop", "SessionStart", "SessionEnd"):
            ours = [h for g in cfg["hooks"][ev] for h in g["hooks"] if "/leavenotrace\" hook " in h["command"]]
            self.assertEqual(len(ours), 1, ev)
        self.assertIn("echo user-hook", json.dumps(cfg["hooks"]["PreToolUse"]))
        self.assertTrue(os.path.exists(sp + ".leavenotrace-bak"))
        self.assertIn(CLI, json.dumps(cfg))
        r = self.run_cli("uninstall", "--settings", sp)
        cfg2 = json.load(open(sp))
        self.assertEqual(cfg2, mine)

    def test_install_copy_and_dry_run(self):
        sp = os.path.join(self.tmp, "s2.json")
        r = self.run_cli("install", "--settings", sp, "--dry-run")
        self.assertFalse(os.path.exists(sp))
        self.assertIn("hook session-start", r.stdout)
        r = self.run_cli("install", "--settings", sp, "--copy")
        copied = os.path.join(self.home, "bin", "leavenotrace")
        self.assertTrue(os.access(copied, os.X_OK))
        self.assertIn(copied, open(sp).read())

    def test_uninstall_on_missing_file(self):
        r = self.run_cli("uninstall", "--settings", os.path.join(self.tmp, "nope.json"))
        self.assertEqual(r.returncode, 0)


class Packaging(unittest.TestCase):
    def test_plugin_manifest_hooks_skill_command(self):
        m = load_module()
        pj = json.load(open(os.path.join(ROOT, ".claude-plugin", "plugin.json")))
        self.assertEqual(pj["name"], m.PLUGIN_NAME)
        self.assertRegex(pj["name"], r"^[a-z][a-z0-9-]*$")
        hj = json.load(open(os.path.join(ROOT, "hooks", "hooks.json")))["hooks"]
        by_event = {ev: (name, matcher) for ev, name, matcher in m.HOOK_EVENTS}
        self.assertEqual(set(hj), set(by_event))
        for ev, groups in hj.items():
            g = groups[0]
            self.assertEqual(g.get("matcher"), by_event[ev][1], ev)
            cmd = g["hooks"][0]["command"]
            self.assertEqual(cmd, 'python3 "${CLAUDE_PLUGIN_ROOT}/bin/%s" hook %s' % (m.CLI_NAME, by_event[ev][0]))
        self.assertLessEqual(hj["SessionEnd"][0]["hooks"][0]["timeout"], 2)
        skill = open(os.path.join(ROOT, "skills", "cleanup", "SKILL.md")).read()
        self.assertTrue(skill.startswith("---\nname: cleanup\ndescription:"))
        for needle in ("leavenotrace list", "close --all-mine", "keep <id>", "release <id>", "reuse", "tmpdir"):
            self.assertIn(needle.lower(), skill.lower())
        cmdmd = open(os.path.join(ROOT, "commands", m.CLI_NAME + ".md")).read()
        self.assertTrue(cmdmd.startswith("---\ndescription:"))
        self.assertIn("$ARGUMENTS", cmdmd)
        self.assertTrue(os.stat(CLI).st_mode & stat.S_IXUSR)
        for f in ("README.md", "LICENSE", "adapters/AGENTS.md.snippet", "adapters/codex/hooks.json",
                  "adapters/gemini/settings.hooks.json", "scripts/demo.sh"):
            self.assertTrue(os.path.exists(os.path.join(ROOT, f)), f)
        json.load(open(os.path.join(ROOT, "adapters", "codex", "hooks.json")))
        json.load(open(os.path.join(ROOT, "adapters", "gemini", "settings.hooks.json")))
        self.assertIn("MIT License", open(os.path.join(ROOT, "LICENSE")).read())

    def test_identity_constants_only_source_of_names(self):
        src = open(CLI).read()
        head, _, body = src.partition("# IDENTITY")
        body = body.split("# ------", 2)[-1]
        for lit in ("LeaveNoTrace", "LEAVENOTRACE", ".leavenotrace"):
            self.assertNotIn(lit, body, "name literal %r hard-coded outside the IDENTITY block" % lit)


class RenameScript(unittest.TestCase):
    @unittest.skipIf(os.environ.get("LNT_RENAME_NESTED"), "nested run")
    def test_rename_produces_a_working_renamed_copy(self):
        tmp = tempfile.mkdtemp(prefix="lnt-rename-")
        try:
            dst = os.path.join(tmp, "tidyup")
            shutil.copytree(ROOT, dst, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.zip"))
            r = subprocess.run([sys.executable, os.path.join(dst, "scripts", "rename.py"), "--name", "tidyup", "--display", "TidyUp"],
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
            self.assertEqual(r.returncode, 0, r.stdout)
            self.assertTrue(os.path.exists(os.path.join(dst, "bin", "tidyup")))
            self.assertFalse(os.path.exists(os.path.join(dst, "bin", "leavenotrace")))
            self.assertEqual(json.load(open(os.path.join(dst, ".claude-plugin", "plugin.json")))["name"], "tidyup")
            blob = ""
            for dp, _, fns in os.walk(dst):
                for fn in fns:
                    if fn.endswith((".py", ".md", ".json", ".sh", ".snippet")) and fn != "rename.py":
                        blob += open(os.path.join(dp, fn)).read()
            self.assertNotRegex(blob, r"(?i)leavenotrace")
            self.assertIn("TIDYUP_HOME", blob)
            self.assertIn("# tidyup:force", blob)
            env = dict(os.environ, LNT_RENAME_NESTED="1")
            r = subprocess.run([sys.executable, "-m", "unittest", "test_simulator", "test_tempfiles", "test_misc"],
                               cwd=os.path.join(dst, "tests"), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               universal_newlines=True, timeout=300)
            self.assertEqual(r.returncode, 0, r.stdout[-3000:])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class Robustness(Base):
    def test_hooks_fail_open_on_garbage(self):
        for ev in ("pre-tool-use", "post-tool-use", "stop", "session-start", "session-end"):
            for inp in ("", "not json", "[1,2]", '{"tool_name": 7, "tool_input": "x"}', '{"session_id": null}'):
                r = self.run_cli("hook", ev, input=inp)
                self.assertEqual(r.returncode, 0, (ev, inp, r.stderr))

    def test_hooks_fail_open_when_state_dir_unwritable(self):
        r = self.run_cli("hook", "pre-tool-use", env={"LEAVENOTRACE_HOME": "/proc/definitely/not/writable"},
                         input=json.dumps({"session_id": "s", "tool_name": "Bash", "tool_input": {"command": "npm run dev &"}}))
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.strip(), "")

    def test_parallel_registrations_do_not_lose_updates(self):
        tmpbase = tempfile.gettempdir()
        procs = []
        for i in range(12):
            procs.append(subprocess.Popen([sys.executable, CLI, "register", "--type", "file", "--path",
                                           os.path.join(tmpbase, "lnt-par-%s-%d" % (os.path.basename(self.tmp), i)),
                                           "--session", self.sid], env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE))
        for p in procs:
            p.communicate(timeout=60)
            self.assertEqual(p.returncode, 0)
        ids = sorted(r["id"] for r in self.resources("temp_file"))
        self.assertEqual(len(ids), 12)
        self.assertEqual(len(set(ids)), 12)

    def test_owner_autodetected_from_process_ancestry_and_cli_resolves_session(self):
        fake = os.path.join(self.fakebin, "claude")
        with open(fake, "w") as f:
            f.write("#!/usr/bin/env python3\nimport json, os, subprocess, sys\n"
                    "cli = sys.argv[1]\nsid = sys.argv[2]\n"
                    "p = {'session_id': sid, 'cwd': os.getcwd(), 'source': 'startup'}\n"
                    "subprocess.run([sys.executable, cli, 'hook', 'session-start'], input=json.dumps(p), universal_newlines=True, stdout=subprocess.DEVNULL)\n"
                    "out = subprocess.run([sys.executable, cli, 'tmpdir'], stdout=subprocess.PIPE, universal_newlines=True).stdout\n"
                    "print(os.getpid(), out.strip())\n")
        os.chmod(fake, 0o755)
        env = dict(self.env)
        env.pop("LEAVENOTRACE_OWNER_PID")
        r = subprocess.run([sys.executable, fake, CLI, "auto-sess"], env=env, cwd=self.work, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, universal_newlines=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        pid, tmpdir = r.stdout.split()
        d = self.registry("auto-sess")
        self.assertEqual(d["owner_pid"], int(pid))
        self.assertTrue(tmpdir.endswith("auto-sess"), r.stdout)
        # the "claude" process has exited -> its session is now crash-recovery material
        self.assertTrue(self.mod.start_token(int(pid)) is None)

    def test_owner_from_claude_pid_env_when_process_name_is_unusual(self):
        """Claude Code exports CLAUDE_PID to hooks; trust it only when it is really an ancestor."""
        fake = os.path.join(self.fakebin, "weird-launcher-name")
        with open(fake, "w") as f:
            f.write("#!/usr/bin/env python3\nimport json, os, subprocess, sys\n"
                    "cli, sid = sys.argv[1], sys.argv[2]\n"
                    "env = dict(os.environ, CLAUDE_PID=os.environ.get('FAKE_CLAUDE_PID') or str(os.getpid()))\n"
                    "p = {'session_id': sid, 'cwd': os.getcwd(), 'source': 'startup'}\n"
                    "subprocess.run([sys.executable, cli, 'hook', 'session-start'], input=json.dumps(p), universal_newlines=True, "
                    "stdout=subprocess.DEVNULL, env=env)\nprint(os.getpid())\n")
        os.chmod(fake, 0o755)
        env = dict(self.env)
        env.pop("LEAVENOTRACE_OWNER_PID")
        r = subprocess.run([sys.executable, fake, CLI, "cpid-sess"], env=env, cwd=self.work, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, universal_newlines=True)
        self.assertEqual(self.registry("cpid-sess")["owner_pid"], int(r.stdout))
        # a CLAUDE_PID that is NOT an ancestor (e.g. pid of some unrelated process) is ignored
        other = self.spawn(["sleep", "60"])
        env["FAKE_CLAUDE_PID"] = str(other.pid)
        r = subprocess.run([sys.executable, fake, CLI, "cpid-sess2"], env=env, cwd=self.work, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, universal_newlines=True)
        self.assertNotEqual(self.registry("cpid-sess2")["owner_pid"], other.pid)

    def test_debug_payload_capture(self):
        log = os.path.join(self.tmp, "payloads.jsonl")
        self.hook("SessionStart", {"source": "startup"}, env={"LEAVENOTRACE_DEBUG_PAYLOADS": log})
        rec = json.loads(open(log).readline())
        self.assertEqual(rec["event"], "session-start")
        self.assertEqual(json.loads(rec["raw"])["session_id"], self.sid)
        self.assertTrue(rec["ppid_chain"])

    def test_gemini_adapter_translates_names_and_output(self):
        port, _ = self.start_agent_server()
        cmd = "python3 -m http.server %d &" % port
        payload = {"session_id": self.sid, "cwd": self.work, "tool_name": "run_shell_command", "tool_input": {"command": cmd}}
        r = self.run_cli("hook", "pre-tool-use", "--agent", "gemini", input=json.dumps(payload))
        out = json.loads(r.stdout)
        self.assertEqual(out["decision"], "deny")
        self.assertIn("proc-1", out["reason"])

    def test_cli_list_json_status_doctor_forget(self):
        port, _ = self.start_agent_server()
        out = json.loads(self.run_cli("list", "--json", "--session", self.sid).stdout)
        self.assertEqual(out[0]["resources"][0]["id"], "proc-1")
        st = self.run_cli("status", "--session", self.sid)
        self.assertIn("process=1", st.stdout)
        dr = self.run_cli("doctor")
        self.assertEqual(dr.returncode, 0, dr.stdout)
        self.assertIn("[ok]", dr.stdout)
        self.run_cli("forget", "proc-1", "--session", self.sid)
        self.assertEqual(self.resources("process", "open"), [])
        self.assertTrue(pid_alive(self.registry()["resources"]["proc-1"]["pid"]), "forget must not kill")

    def test_manual_register_for_other_agents(self):
        p = self.spawn(["sleep", "300"])
        r = self.run_cli("register", "--type", "process", "--pid", str(p.pid), "--command", "sleep 300", "--keep", "--session", self.sid)
        self.assertEqual(r.stdout.strip(), "proc-1")
        res = self.resources("process")[0]
        self.assertTrue(res["keep_alive"])
        # re-registering the same pid into another session is allowed but a dead pid is not
        dead = subprocess.Popen(["true"])
        dead.wait()
        r = self.run_cli("register", "--type", "process", "--pid", str(dead.pid), "--session", self.sid)
        self.assertNotEqual(r.returncode, 0)
        self.run_cli("close", "--all-mine", "--session", self.sid)
        self.assertTrue(wait_dead(p.pid))

    def test_pending_pruned_and_session_files_pruned(self):
        store = self.mod.Store(self.home)
        with store.txn("old-ended", create=dict(owner_pid=None, owner_start=None, cwd="/")) as d:
            d["ended"] = True
        path = os.path.join(self.home, "sessions", "old-ended.json")
        dd = json.load(open(path))
        dd["updated_at"] -= 30 * 86400
        json.dump(dd, open(path, "w"))
        # txn() bumps updated_at, so write the aged file directly (above) and then run session-start
        self.hook("SessionStart", {"source": "startup"})
        self.assertFalse(os.path.exists(path))


if __name__ == "__main__":
    unittest.main()
