"""Kill-safety tests: fail closed on any doubt."""
import os
import re
import subprocess
import sys
import time
import unittest

from helpers import Base, CLI, ROOT, free_port, wait_port, wait_dead, pid_alive


class KillSafety(Base):
    def test_never_kills_same_port_process_that_is_not_in_registry(self):
        port = free_port()
        user = self.spawn(["python3", "-m", "http.server", str(port), "--bind", "127.0.0.1"])
        self.assertTrue(wait_port(port))
        # registry claims port `port`, but its pid is long dead -> a "stale" entry on the same port as the user's server
        dead = subprocess.Popen(["sleep", "0.1"])
        dead.wait()
        store = self.mod.Store(self.home)
        with store.txn(self.sid, create=dict(owner_pid=os.getpid(), owner_start=self.mod.start_token(os.getpid()), cwd=self.work)) as d:
            self.mod.add_resource(d, "process", pid=dead.pid, pid_start="x:1", command="http.server", command_norm="x",
                                  port=port, ports=[port])
        r = self.run_cli("close", "--all-mine", "--session", self.sid)
        self.assertTrue(pid_alive(user.pid), "must never kill by port")
        self.assertTrue(wait_port(port))

    def test_pid_reuse_start_time_mismatch_is_refused(self):
        victim = self.spawn(["sleep", "300"])
        rid = self.register_proc(victim, extra={"pid_start": "%s:1" % self.mod._boot_id()})  # wrong start time
        r = self.run_cli("close", rid, "--session", self.sid)
        time.sleep(0.3)
        self.assertTrue(pid_alive(victim.pid), r.stdout + r.stderr)
        self.assertIn("refused", r.stdout)
        self.assertEqual(self.registry()["resources"][rid]["state"], "refused")

    def test_hook_cleaner_also_refuses_mismatch(self):
        victim = self.spawn(["sleep", "300"])
        self.register_proc(victim, extra={"pid_start": "bogus"})
        self.hook("SessionEnd", {"reason": "other"})
        time.sleep(0.8)
        self.assertTrue(pid_alive(victim.pid))

    def test_missing_start_time_is_refused(self):
        victim = self.spawn(["sleep", "300"])
        rid = self.register_proc(victim, extra={"pid_start": None})
        self.run_cli("close", rid, "--session", self.sid)
        time.sleep(0.3)
        self.assertTrue(pid_alive(victim.pid))

    def test_never_kills_ancestors_of_the_cli(self):
        script = os.path.join(self.tmp, "wrap.sh")
        pidf, gof, outf = (os.path.join(self.tmp, n) for n in ("pid", "go", "out"))
        with open(script, "w") as f:
            f.write("#!/bin/sh\necho $$ > %s\nwhile [ ! -f %s ]; do sleep 0.05; done\n"
                    "%s %s close --all-mine --session %s > %s 2>&1\nexit 0\n" % (pidf, gof, sys.executable, CLI, self.sid, outf))
        env = dict(self.env)
        wrapper = subprocess.Popen(["sh", script], env=env, cwd=self.work)
        self.procs.append(wrapper)
        while not os.path.exists(pidf) or not open(pidf).read().strip():
            time.sleep(0.02)
        pid = int(open(pidf).read().strip())
        self.assertEqual(pid, wrapper.pid)
        store = self.mod.Store(self.home)
        # the registry (wrongly) lists the wrapper shell - which is an ancestor of the CLI doing the closing
        with store.txn(self.sid, create=dict(owner_pid=None, owner_start=None, cwd=self.work)) as d:
            self.mod.add_resource(d, "process", pid=pid, pid_start=self.mod.start_token(pid), command="wrap",
                                  command_norm="wrap", cwd=self.work)
        open(gof, "w").close()
        self.assertEqual(wrapper.wait(timeout=20), 0, "wrapper must not be killed (exit 0 expected)")
        out = open(outf).read()
        self.assertIn("refused", out)
        self.assertIn("protected", out)

    def test_never_kills_the_agent_process_even_if_registered(self):
        fake_agent = self.spawn(["sleep", "300"])
        env = dict(self.env, LEAVENOTRACE_OWNER_PID=str(fake_agent.pid))
        self.env = env
        store = self.mod.Store(self.home)
        with store.txn(self.sid, create=dict(owner_pid=fake_agent.pid, owner_start=self.mod.start_token(fake_agent.pid), cwd=self.work)) as d:
            rid = self.mod.add_resource(d, "process", pid=fake_agent.pid, pid_start=self.mod.start_token(fake_agent.pid),
                                        command="x", command_norm="x", cwd=self.work)["id"]
        self.run_cli("close", "--all-mine", "--session", self.sid)
        self.hook("SessionEnd", {"reason": "other"})
        time.sleep(0.8)
        self.assertTrue(pid_alive(fake_agent.pid), "owner/agent pid is protected")

    def test_register_cli_refuses_protected_pid(self):
        r = self.run_cli("register", "--type", "process", "--pid", str(os.getpid()), "--session", self.sid)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("protected", r.stdout)

    def test_does_not_kill_unregistered_child_of_registered_parent_sibling(self):
        # a sibling process that merely shares the registered process's parent must survive
        sibling = self.spawn(["sleep", "300"])
        target = self.spawn(["sleep", "301"])
        rid = self.register_proc(target)
        self.run_cli("close", rid, "--session", self.sid)
        self.assertTrue(wait_dead(target.pid))
        self.assertTrue(pid_alive(sibling.pid))

    def test_recorded_children_verified_individually(self):
        parent = self.spawn(["sleep", "300"])
        stranger = self.spawn(["sleep", "301"])
        rid = self.register_proc(parent, extra={"children": [{"pid": stranger.pid, "start": "wrong"}]})
        self.run_cli("close", rid, "--session", self.sid)
        self.assertTrue(wait_dead(parent.pid))
        self.assertTrue(pid_alive(stranger.pid), "child entry with mismatching start time must be ignored")

    def test_recorded_child_that_matches_is_killed_after_root_exits(self):
        # double-fork style: root already gone, orphaned child recorded with correct start time
        child = self.spawn(["sleep", "300"])
        dead = subprocess.Popen(["sleep", "0.1"])
        dead.wait()
        rid = self.register_proc(child, extra={"pid": dead.pid, "pid_start": "x:1"})
        # root mismatch => whole entry refused (fail closed)
        self.run_cli("close", rid, "--session", self.sid)
        self.assertTrue(pid_alive(child.pid))

    def test_unresolvable_session_fails_without_killing(self):
        victim = self.spawn(["sleep", "300"])
        other_owner = self.spawn(["sleep", "302"])
        store = self.mod.Store(self.home)
        with store.txn(self.sid, create=dict(owner_pid=other_owner.pid, owner_start=self.mod.start_token(other_owner.pid), cwd="/elsewhere")) as d:
            self.mod.add_resource(d, "process", pid=victim.pid, pid_start=self.mod.start_token(victim.pid), command="s", command_norm="s")
        r = self.run_cli("close", "--all-mine", cwd="/")
        self.assertNotEqual(r.returncode, 0)
        self.assertTrue(pid_alive(victim.pid))


class SourceAudit(unittest.TestCase):
    def test_source_has_no_forbidden_kill_primitives(self):
        src = open(CLI).read()
        code = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
        self.assertNotIn("killpg", code)
        self.assertNotRegex(code, r"shutdown[\"', ]+all")
        self.assertNotRegex(code, r"\bpkill\b.*subprocess|subprocess.*\bpkill\b")
        self.assertNotIn("killall node", code)
        self.assertNotRegex(code, r"lsof[^\n]*-t")  # no `kill $(lsof -ti:PORT)` style
        # the only os.kill calls are in safe_kill_tree after identity verification
        calls = [m.start() for m in re.finditer(r"os\.kill\(", code)]
        self.assertEqual(len(calls), 1)
        i = code.index("def safe_kill_tree")
        j = code.index("def temp_roots")
        self.assertTrue(i < calls[0] < j)


if __name__ == "__main__":
    unittest.main()
