import json
import os
import subprocess
import sys
import time
import unittest

from helpers import Base, CLI, free_port, wait_port, wait_dead, pid_alive


class ProcessRegistration(Base):
    def test_post_registers_background_server_with_pid_start_port(self):
        port, post = self.start_agent_server()
        procs = self.resources("process", "open")
        self.assertEqual(len(procs), 1, post.stdout)
        r = procs[0]
        self.assertEqual(r["port"], port)
        self.assertIn(port, r["ports"])
        self.assertIsNotNone(r["pid_start"])
        self.assertEqual(r["cwd"], os.path.realpath(self.work))
        self.assertTrue(pid_alive(r["pid"]))
        self.assertIn("registered proc-1", post.json["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(post.json["hookSpecificOutput"]["hookEventName"], "PostToolUse")

    def test_run_in_background_direct_child(self):
        port = free_port()
        cmd = "python3 -m http.server %d --bind 127.0.0.1" % port
        self.bash(cmd, "pre", background=True)
        self.spawn(["python3", "-m", "http.server", str(port), "--bind", "127.0.0.1"])
        self.assertTrue(wait_port(port))
        self.bash(cmd, "post", background=True)
        self.assertEqual(len(self.resources("process", "open")), 1)

    def test_plain_foreground_commands_register_nothing(self):
        for cmd in ("ls -la", "git status", "python3 -c 'print(1)'", "echo hi > /tmp/x", "npm test"):
            self.bash(cmd, "pre")
            self.bash(cmd, "post")
        self.assertEqual(self.resources("process"), [])

    def test_user_started_server_not_registered(self):
        # a server the *user* started in the same cwd with the same command line before/around the tool call
        port = free_port()
        user = self.spawn(["python3", "-m", "http.server", str(port), "--bind", "127.0.0.1"])
        self.assertTrue(wait_port(port))
        time.sleep(0.2)
        cmd = "python3 -m http.server %d --bind 127.0.0.1 &" % port
        self.bash(cmd, "pre")
        # (the agent's command fails because the port is taken; nothing new starts)
        self.bash(cmd, "post")
        self.assertEqual(self.resources("process"), [])
        self.assertTrue(pid_alive(user.pid))

    def test_process_registered_by_other_session_is_not_claimed_twice(self):
        port, _ = self.start_agent_server()
        pid = self.resources("process")[0]["pid"]
        cmd = "python3 -m http.server %d --bind 127.0.0.1 &" % port
        self.bash(cmd, "post", sid="other-session", tid="t2")
        self.assertEqual(self.resources("process", sid="other-session") if os.path.exists(os.path.join(self.home, "sessions", "other-session.json")) else [], [])
        self.assertTrue(pid_alive(pid))


class ReuseEnforcement(Base):
    def test_duplicate_port_denied_with_reuse_info(self):
        port, _ = self.start_agent_server()
        pre = self.bash("python3 -m http.server %d &" % port, "pre", tid="t2")
        out = pre.json["hookSpecificOutput"]
        self.assertEqual(out["hookEventName"], "PreToolUse")
        self.assertEqual(out["permissionDecision"], "deny")
        reason = out["permissionDecisionReason"]
        self.assertIn(str(port), reason)
        self.assertIn("proc-1", reason)
        self.assertIn("Reuse", reason)
        self.assertIn("# leavenotrace:force", reason)

    def test_same_command_same_cwd_denied_even_without_port(self):
        p = self.spawn(["sleep", "300"])
        rid = self.register_proc(p, cmd_norm="npm run dev")
        pre = self.bash("npm run dev &", "pre")
        self.assertEqual(pre.json["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn(rid, pre.json["hookSpecificOutput"]["permissionDecisionReason"])

    def test_force_marker_overrides(self):
        port, _ = self.start_agent_server()
        pre = self.bash("python3 -m http.server %d & # leavenotrace:force" % port, "pre", tid="t3")
        self.assertIsNone(pre.json)

    def test_dead_registered_server_does_not_block_restart(self):
        port, _ = self.start_agent_server()
        pid = self.resources("process")[0]["pid"]
        os.kill(pid, 9)
        self.assertTrue(wait_dead(pid))
        pre = self.bash("python3 -m http.server %d &" % port, "pre", tid="t4")
        self.assertIsNone(pre.json)

    def test_other_session_server_blocks_same_port(self):
        port, _ = self.start_agent_server()
        pre = self.bash("python3 -m http.server %d &" % port, "pre", sid="another", tid="t5")
        self.assertEqual(pre.json["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("another agent session", pre.json["hookSpecificOutput"]["permissionDecisionReason"])

    def test_unowned_port_listener_gets_note_not_denial(self):
        port = free_port()
        user = self.spawn(["python3", "-m", "http.server", str(port), "--bind", "127.0.0.1"])
        self.assertTrue(wait_port(port))
        pre = self.bash("python3 -m http.server %d &" % port, "pre")
        ctx = pre.json["hookSpecificOutput"]["additionalContext"]
        self.assertIn("NOT owned", ctx)
        self.assertNotIn("permissionDecision", pre.json["hookSpecificOutput"])

    def test_touch_updates_last_used(self):
        port, _ = self.start_agent_server()
        d = self.registry()
        d["resources"]["proc-1"]["last_used_at"] -= 1000
        self.save_registry(d)
        before = self.resources("process")[0]["last_used_at"]
        self.bash("curl -s http://localhost:%d/" % port, "pre", tid="t9")
        self.assertGreater(self.resources("process")[0]["last_used_at"], before + 900)


class Closing(Base):
    def test_close_all_mine_kills_registered_only_and_tree(self):
        port, _ = self.start_agent_server()
        pid = self.resources("process")[0]["pid"]
        user_port = free_port()
        user = self.spawn(["python3", "-m", "http.server", str(user_port), "--bind", "127.0.0.1"])
        self.assertTrue(wait_port(user_port))
        r = self.run_cli("close", "--all-mine", "--session", self.sid)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(wait_dead(pid))
        self.assertTrue(pid_alive(user.pid), "user-started server on a different port must survive")
        self.assertEqual(self.resources("process", "closed")[0]["id"], "proc-1")
        self.assertIn("proc-1", r.stdout)

    def test_tree_children_are_killed(self):
        p = self.spawn(["sh", "-c", "sleep 300; true"])
        time.sleep(0.4)
        rid = self.register_proc(p)
        kids = self.mod.descendants(p.pid, self.mod.list_processes())
        self.assertTrue(kids)
        r = self.run_cli("close", rid, "--session", self.sid)
        self.assertTrue(wait_dead(p.pid))
        for k in kids:
            self.assertTrue(wait_dead(k), "child %d should be gone" % k)

    def test_sigterm_then_sigkill_for_stubborn_process(self):
        p = self.spawn([sys.executable, "-c",
                        "import signal,time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\nprint('r',flush=True)\ntime.sleep(300)"],
                       stdout=subprocess.PIPE)
        p.stdout.readline()
        rid = self.register_proc(p)
        t0 = time.time()
        r = self.run_cli("close", rid, "--session", self.sid, "--grace", "0.5")
        self.assertTrue(wait_dead(p.pid))
        self.assertLess(time.time() - t0, 8)

    def test_close_by_prefix_and_dry_run(self):
        p = self.spawn(["sleep", "300"])
        rid = self.register_proc(p)
        r = self.run_cli("close", "--all-mine", "--dry-run", "--session", self.sid)
        self.assertTrue(pid_alive(p.pid))
        self.assertIn("would kill", r.stdout)
        self.run_cli("close", "proc", "--session", self.sid)  # ambiguous/unique prefix handling
        self.assertEqual(self.run_cli("close", "proc-1", "--session", self.sid).returncode, 0)
        self.assertTrue(wait_dead(p.pid))

    def test_keep_and_release_and_except_kept(self):
        a, b = self.spawn(["sleep", "300"]), self.spawn(["sleep", "301"])
        ra, rb = self.register_proc(a), self.register_proc(b)
        self.assertEqual(self.run_cli("keep", ra, "--session", self.sid).returncode, 0)
        self.assertTrue(self.registry()["resources"][ra]["keep_alive"])
        self.run_cli("close", "--all-mine", "--except-kept", "--session", self.sid)
        self.assertTrue(wait_dead(b.pid))
        self.assertTrue(pid_alive(a.pid))
        self.run_cli("release", ra, "--session", self.sid)
        self.assertFalse(self.registry()["resources"][ra]["keep_alive"])
        self.run_cli("close", "--all-mine", "--session", self.sid)
        self.assertTrue(wait_dead(a.pid))


class StopHookTTL(Base):
    def _age(self, rid, seconds):
        d = self.registry()
        d["resources"][rid]["last_used_at"] -= seconds
        self.save_registry(d)

    def test_stop_hook_is_cheap_and_never_closes_fresh_things(self):
        port, _ = self.start_agent_server()
        pid = self.resources("process")[0]["pid"]
        r = self.hook("Stop", {"stop_hook_active": False, "last_assistant_message": "done"})
        self.assertEqual(r.stdout.strip(), "")
        self.assertLess(r.elapsed, 1.5)
        time.sleep(0.5)
        self.assertTrue(pid_alive(pid))
        self.assertEqual(self.resources("process")[0]["state"], "open")

    def test_idle_ttl_closes_only_idle_unkept(self):
        idle, kept, fresh = self.spawn(["sleep", "300"]), self.spawn(["sleep", "301"]), self.spawn(["sleep", "302"])
        ri, rk, rf = self.register_proc(idle), self.register_proc(kept), self.register_proc(fresh)
        self.run_cli("keep", rk, "--session", self.sid)
        self._age(ri, 31 * 60)
        self._age(rk, 31 * 60)
        self._age(rf, 5 * 60)
        r = self.hook("Stop", {"stop_hook_active": False})
        self.assertLess(r.elapsed, 1.5)
        self.assertTrue(wait_dead(idle.pid), "idle server should be swept by the detached cleaner")
        self.assertTrue(pid_alive(kept.pid))
        self.assertTrue(pid_alive(fresh.pid))

    def test_ttl_configurable_via_env(self):
        p = self.spawn(["sleep", "300"])
        rid = self.register_proc(p)
        self._age(rid, 120)
        self.hook("Stop", {}, env={"LEAVENOTRACE_IDLE_TTL_MINUTES": "1"})
        self.assertTrue(wait_dead(p.pid))

    def test_ttl_zero_disables(self):
        p = self.spawn(["sleep", "300"])
        rid = self.register_proc(p)
        self._age(rid, 99999)
        self.hook("Stop", {}, env={"LEAVENOTRACE_IDLE_TTL_MINUTES": "0"})
        time.sleep(0.6)
        self.assertTrue(pid_alive(p.pid))

    def test_ttl_from_config_file(self):
        os.makedirs(self.home, exist_ok=True)
        with open(os.path.join(self.home, "config.json"), "w") as f:
            json.dump({"idle_ttl_minutes": 2}, f)
        p = self.spawn(["sleep", "300"])
        rid = self.register_proc(p)
        self._age(rid, 180)
        self.hook("Stop", {})
        self.assertTrue(wait_dead(p.pid))


class SessionLifecycle(Base):
    def test_session_end_closes_everything_fast(self):
        port, _ = self.start_agent_server()
        pid = self.resources("process")[0]["pid"]
        tmpd = self.hook("SessionStart", {"source": "startup"}).json
        tdir = os.path.join(self.home, "tmp", self.sid)
        self.assertTrue(os.path.isdir(tdir))
        r = self.hook("SessionEnd", {"reason": "other"})
        self.assertLess(r.elapsed, 1.5, "SessionEnd must stay inside Claude Code's 1.5 s budget")
        self.assertTrue(wait_dead(pid))
        self.assertTrue(wait_dead_dir(tdir))
        self.assertTrue(self.registry()["ended"])

    def test_session_end_hands_off_to_detached_cleaner(self):
        p = self.spawn([sys.executable, "-c",
                        "import signal,time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\nprint('r',flush=True)\ntime.sleep(300)"],
                       stdout=subprocess.PIPE)
        p.stdout.readline()
        self.register_proc(p)
        r = self.hook("SessionEnd", {"reason": "other"}, env={"LEAVENOTRACE_GRACE_SECONDS": "2",
                                                                "LEAVENOTRACE_SESSION_END_WAIT_MS": "100"})
        self.assertLess(r.elapsed, 1.5)
        self.assertTrue(pid_alive(p.pid), "stubborn process is still dying after the hook returned")
        self.assertTrue(wait_dead(p.pid, 10), "detached cleaner finished the job after the hook returned")

    def test_session_start_context_and_env_file(self):
        port, _ = self.start_agent_server()
        envf = os.path.join(self.tmp, "envfile")
        r = self.hook("SessionStart", {"source": "startup"}, env={"CLAUDE_ENV_FILE": envf})
        ctx = r.json["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(r.json["hookSpecificOutput"]["hookEventName"], "SessionStart")
        self.assertIn("proc-1", ctx)
        self.assertIn("close --all-mine", ctx)
        self.assertIn("leavenotrace:force", ctx)
        content = open(envf).read()
        self.assertIn("LEAVENOTRACE_SESSION_ID=%s" % self.sid, content)
        self.assertIn("LEAVENOTRACE_TMPDIR=", content)

    def test_crash_recovery_sweeps_dead_owner_only(self):
        # session "crashed": its owner process is dead, but its dev server lives on (reparented)
        dead_owner = subprocess.Popen(["sleep", "0.1"])
        dead_owner.wait()
        orphan = self.spawn(["sleep", "300"])
        store = self.mod.Store(self.home)
        with store.txn("crashed", create=dict(owner_pid=dead_owner.pid, owner_start="boot:12345", cwd=self.work)) as d:
            self.mod.add_resource(d, "process", pid=orphan.pid, pid_start=self.mod.start_token(orphan.pid),
                                  command="sleep 300", command_norm="sleep 300", cwd=self.work)
        # a *live* other session must not be touched
        live_owner = self.spawn(["sleep", "300"])
        bystander = self.spawn(["sleep", "301"])
        with store.txn("alive-other", create=dict(owner_pid=live_owner.pid, owner_start=self.mod.start_token(live_owner.pid), cwd=self.work)) as d:
            self.mod.add_resource(d, "process", pid=bystander.pid, pid_start=self.mod.start_token(bystander.pid),
                                  command="sleep 301", command_norm="sleep 301", cwd=self.work)
        r = self.hook("SessionStart", {"source": "startup"})
        self.assertIn("crashed", r.json["hookSpecificOutput"]["additionalContext"])
        self.assertTrue(wait_dead(orphan.pid), "orphan of crashed session must be closed")
        self.assertTrue(pid_alive(bystander.pid), "live session's resources must be left alone")
        self.assertTrue(pid_alive(live_owner.pid))

    def test_sweep_cli_dry_run_and_real(self):
        dead_owner = subprocess.Popen(["sleep", "0.1"])
        dead_owner.wait()
        orphan = self.spawn(["sleep", "300"])
        store = self.mod.Store(self.home)
        with store.txn("crashed2", create=dict(owner_pid=dead_owner.pid, owner_start="boot:1", cwd=self.work)) as d:
            self.mod.add_resource(d, "process", pid=orphan.pid, pid_start=self.mod.start_token(orphan.pid),
                                  command="sleep 300", command_norm="sleep 300")
        r = self.run_cli("sweep", "--dry-run", "--session", self.sid)
        self.assertTrue(pid_alive(orphan.pid))
        r = self.run_cli("sweep", "--session", self.sid)
        self.assertTrue(wait_dead(orphan.pid))

    def test_unknown_owner_not_swept_when_recent(self):
        orphan = self.spawn(["sleep", "300"])
        store = self.mod.Store(self.home)
        with store.txn("noowner", create=dict(owner_pid=None, owner_start=None, cwd=self.work)) as d:
            self.mod.add_resource(d, "process", pid=orphan.pid, pid_start=self.mod.start_token(orphan.pid),
                                  command="sleep 300", command_norm="sleep 300")
        self.hook("SessionStart", {"source": "startup"})
        time.sleep(0.7)
        self.assertTrue(pid_alive(orphan.pid), "fail closed: unknown owner + recent => do not sweep")

    def test_resumed_session_with_dead_previous_owner_recovers_own_leftovers(self):
        dead_owner = subprocess.Popen(["sleep", "0.1"])
        dead_owner.wait()
        orphan = self.spawn(["sleep", "300"])
        store = self.mod.Store(self.home)
        with store.txn(self.sid, create=dict(owner_pid=dead_owner.pid, owner_start="boot:7", cwd=self.work)) as d:
            self.mod.add_resource(d, "process", pid=orphan.pid, pid_start=self.mod.start_token(orphan.pid),
                                  command="sleep 300", command_norm="sleep 300")
        self.hook("SessionStart", {"source": "resume"})
        self.assertTrue(wait_dead(orphan.pid))
        self.assertEqual(self.registry()["owner_pid"], os.getpid())


def wait_dead_dir(path, timeout=8):
    end = time.time() + timeout
    while time.time() < end:
        if not os.path.exists(path):
            return True
        time.sleep(0.05)
    return not os.path.exists(path)


if __name__ == "__main__":
    unittest.main()
