import os
import subprocess
import sys
import tempfile
import time
import unittest

from helpers import Base

# A path that is never under a temp root on Linux or macOS and is never created by these tests.
NON_TEMP_NONEXISTENT = "/opt/leavenotrace-test-nonexistent"


def home_is_temp(mod, home=None):
    """True if $HOME lies under a temp root (e.g. HOME=/tmp/lnt-home), where "home-like = not temp" cannot hold."""
    home = home or os.path.expanduser("~")
    return mod.is_temp_path(mod.Store(os.path.join(home, ".lnt-probe")), os.path.join(home, "probe-file"))


class TempFiles(Base):
    def write(self, path, phase, created=True, **extra):
        payload = {"tool_name": "Write", "tool_input": {"file_path": path, "content": "x"}, "tool_use_id": "w-" + os.path.basename(path)}
        payload.update(extra)
        if phase == "post":
            payload["tool_response"] = {"filePath": path, "type": "create" if created else "update"}
        return self.hook("PreToolUse" if phase == "pre" else "PostToolUse", payload)

    def do_write(self, path, **kw):
        self.write(path, "pre", **kw)
        existed = os.path.exists(path)
        with open(path, "w") as f:
            f.write("x")
        self.write(path, "post", created=not existed, **kw)

    def test_new_file_in_tmp_registered_and_deleted_on_close(self):
        p = os.path.join(tempfile.gettempdir(), "lnt-%s.txt" % os.path.basename(self.tmp))
        self.addCleanup(lambda: os.path.exists(p) and os.remove(p))
        self.do_write(p)
        files = self.resources("temp_file", "open")
        self.assertEqual([f["path"] for f in files], [p])
        r = self.run_cli("close", "--all-mine", "--session", self.sid)
        self.assertFalse(os.path.exists(p), r.stdout)

    def test_preexisting_file_not_registered_and_not_deleted(self):
        p = os.path.join(tempfile.gettempdir(), "lnt-pre-%s.txt" % os.path.basename(self.tmp))
        open(p, "w").write("user data")
        self.addCleanup(lambda: os.path.exists(p) and os.remove(p))
        self.do_write(p)
        self.assertEqual(self.resources("temp_file"), [])
        self.run_cli("close", "--all-mine", "--session", self.sid)
        self.assertTrue(os.path.exists(p))

    def test_non_temp_paths_ignored(self):
        p = os.path.join(self.work, "src.py")  # inside the project (the work dir is under /tmp here, so use a marker-less home path)
        home_like = os.path.join(NON_TEMP_NONEXISTENT, "lnt-not-temp-%s" % os.path.basename(self.tmp))
        self.write(home_like, "pre")
        self.write(home_like, "post")
        self.assertEqual(self.resources("temp_file"), [])

    def test_agent_tmp_marker_dir_counts_as_temp(self):
        base = os.path.join(os.path.expanduser("~"), "lnt-proj-%s" % os.path.basename(self.tmp), ".agent-tmp")
        os.makedirs(base)
        self.addCleanup(lambda: __import__("shutil").rmtree(os.path.dirname(base), ignore_errors=True))
        p = os.path.join(base, "scratch.json")
        self.do_write(p)
        self.assertEqual(len(self.resources("temp_file", "open")), 1)
        self.run_cli("close", "--all-mine", "--session", self.sid)
        self.assertFalse(os.path.exists(p))

    def test_agents_own_scratchpad_is_skipped(self):
        scratch = os.path.join(tempfile.gettempdir(), "claude-1000", "proj", self.sid, "scratchpad")
        os.makedirs(scratch, exist_ok=True)
        self.addCleanup(lambda: __import__("shutil").rmtree(os.path.join(tempfile.gettempdir(), "claude-1000", "proj"), ignore_errors=True))
        p = os.path.join(scratch, "notes.md")
        self.do_write(p, scratchpad_dir=scratch)
        self.assertEqual(self.resources("temp_file"), [])

    def test_tampered_registry_cannot_delete_outside_temp(self):
        # a real, existing non-temp file when $HOME is not under a temp root (the normal case); with HOME=/tmp/... we cannot
        # create anything outside temp without touching the machine, so fall back to a never-created non-temp path
        # (the refusal happens on the path check, before the filesystem is consulted).
        if home_is_temp(self.mod):
            victim = os.path.join(NON_TEMP_NONEXISTENT, "lnt-precious-%s" % os.path.basename(self.tmp))
        else:
            victim = os.path.join(os.path.expanduser("~"), "lnt-precious-%s" % os.path.basename(self.tmp))
            open(victim, "w").write("precious")
            self.addCleanup(lambda: os.path.exists(victim) and os.remove(victim))
        store = self.mod.Store(self.home)
        with store.txn(self.sid, create=dict(owner_pid=os.getpid(), owner_start=self.mod.start_token(os.getpid()), cwd=self.work)) as d:
            self.mod.add_resource(d, "temp_file", path=victim)
            self.mod.add_resource(d, "temp_file", path="/tmp")           # the temp root itself
            self.mod.add_resource(d, "temp_file", path="/tmp/../etc/hosts")
            self.mod.add_resource(d, "temp_file", path="relative/path.txt")
        r = self.run_cli("close", "--all-mine", "--session", self.sid)
        self.assertTrue(os.path.exists(victim) or home_is_temp(self.mod))
        self.assertTrue(os.path.isdir("/tmp"))
        self.assertTrue(os.path.exists("/etc/hosts"))
        self.assertEqual(r.stdout.count("refused"), 4, r.stdout)

    def test_is_temp_path_resolves_macos_style_aliases(self):
        """/tmp -> /private/tmp, /var -> /private/var and TMPDIR under /var/folders must all count as temp, roots never."""
        mod, store = self.mod, self.mod.Store(self.home)
        real = os.path.realpath(tempfile.gettempdir())
        self.assertTrue(mod.is_temp_path(store, os.path.join(tempfile.gettempdir(), "x.txt")))
        self.assertTrue(mod.is_temp_path(store, os.path.join(real, "x.txt")))
        self.assertFalse(mod.is_temp_path(store, real))
        self.assertFalse(mod.is_temp_path(store, tempfile.gettempdir()))
        self.assertTrue(mod.is_temp_path(store, "/tmp/x.txt"))
        self.assertTrue(mod.is_temp_path(store, os.path.join(os.path.realpath("/tmp"), "x.txt")))
        self.assertFalse(mod.is_temp_path(store, "/tmp"))
        self.assertFalse(mod.is_temp_path(store, os.path.realpath("/tmp")))
        self.assertFalse(mod.is_temp_path(store, "/tmp/../etc/hosts"))
        self.assertFalse(mod.is_temp_path(store, NON_TEMP_NONEXISTENT + "/x"))
        self.assertFalse(mod.is_temp_path(store, "/etc/hosts"))

    def test_symlink_in_tmp_removes_link_not_target(self):
        target = os.path.join(os.path.expanduser("~"), "lnt-target-%s" % os.path.basename(self.tmp))
        open(target, "w").write("keep me")
        self.addCleanup(lambda: os.path.exists(target) and os.remove(target))
        link = os.path.join(self.work, "..", "lnk")
        link = os.path.normpath(link)
        os.symlink(target, link)
        store = self.mod.Store(self.home)
        with store.txn(self.sid, create=dict(owner_pid=os.getpid(), owner_start=self.mod.start_token(os.getpid()), cwd=self.work)) as d:
            self.mod.add_resource(d, "temp_file", path=link)
        self.run_cli("close", "--all-mine", "--session", self.sid)
        self.assertFalse(os.path.lexists(link))
        self.assertTrue(os.path.exists(target))

    def test_session_tmpdir_helper_and_cleanup(self):
        r = self.run_cli("tmpdir", "--session", self.sid)
        path = r.stdout.strip()
        self.assertEqual(path, os.path.join(self.home, "tmp", self.sid))
        self.assertTrue(os.path.isdir(path))
        self.assertEqual(oct(os.stat(path).st_mode & 0o777), "0o700")
        f = os.path.join(path, "a", "b.txt")
        os.makedirs(os.path.dirname(f))
        open(f, "w").write("x")
        self.write(f, "pre")
        self.write(f, "post")
        self.assertEqual(self.resources("temp_file"), [], "files inside the session tmpdir are covered by the dir itself")
        self.run_cli("close", "--all-mine", "--session", self.sid)
        self.assertFalse(os.path.exists(path))

    def test_session_tmpdir_symlink_swap_is_refused(self):
        r = self.run_cli("tmpdir", "--session", self.sid)
        path = r.stdout.strip()
        os.rmdir(path)
        victim = os.path.join(self.tmp, "victimdir")
        os.makedirs(victim)
        open(os.path.join(victim, "keep"), "w").write("x")
        os.symlink(victim, path)
        self.run_cli("close", "--all-mine", "--session", self.sid)
        self.assertTrue(os.path.exists(os.path.join(victim, "keep")))


if __name__ == "__main__":
    unittest.main()
