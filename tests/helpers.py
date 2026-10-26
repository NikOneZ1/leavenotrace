"""Shared helpers for the LeaveNoTrace test-suite (stdlib unittest, real processes)."""
import importlib.machinery
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLI = os.path.join(ROOT, "bin", "leavenotrace")


def load_module():
    loader = importlib.machinery.SourceFileLoader("leavenotrace_mod", CLI)
    spec = importlib.util.spec_from_loader("leavenotrace_mod", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def wait_port(port, timeout=8.0):
    end = time.time() + timeout
    while time.time() < end:
        try:
            socket.create_connection(("127.0.0.1", port), 0.2).close()
            return True
        except OSError:
            time.sleep(0.05)
    return False


def pid_alive(pid):
    """True if pid exists and is not a zombie (Linux /proc, otherwise `ps`)."""
    if os.path.isdir("/proc/self"):
        try:
            with open("/proc/%d/stat" % pid) as f:
                return f.read().rsplit(")", 1)[1].split()[0] not in ("Z", "X")
        except Exception:
            return False
    try:
        out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, universal_newlines=True, timeout=10).stdout.strip()
    except Exception:
        return False
    return bool(out) and out[0] not in ("Z", "X")


def cwd_of_all():
    """{pid: cwd} for every process we can see (Linux /proc, otherwise one `lsof` call)."""
    res = {}
    if os.path.isdir("/proc/self"):
        for name in os.listdir("/proc"):
            if name.isdigit():
                try:
                    res[int(name)] = os.readlink("/proc/%s/cwd" % name)
                except Exception:
                    pass
        return res
    try:
        out = subprocess.run(["lsof", "-nP", "-a", "-d", "cwd", "-Fpn"], stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, universal_newlines=True, timeout=30).stdout
    except Exception:
        return res
    cur = None
    for line in out.splitlines():
        if line.startswith("p"):
            cur = int(line[1:])
        elif line.startswith("n") and cur is not None:
            res[cur] = line[1:]
    return res


def wait_dead(pid, timeout=8.0):
    end = time.time() + timeout
    while time.time() < end:
        if not pid_alive(pid):
            return True
        time.sleep(0.05)
    return not pid_alive(pid)


class Base(unittest.TestCase):
    """Isolated state dir; the test process itself plays the role of the agent ("claude") process."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lnt-test-")
        self.home = os.path.join(self.tmp, "state")
        self.work = os.path.join(self.tmp, "work")
        os.makedirs(self.work)
        self.sid = "sess-" + os.path.basename(self.tmp)
        self.procs = []
        self.fakebin = os.path.join(self.tmp, "fakebin")
        os.makedirs(self.fakebin)
        self.env = dict(os.environ, LEAVENOTRACE_HOME=self.home, LEAVENOTRACE_OWNER_PID=str(os.getpid()),
                        LEAVENOTRACE_GRACE_SECONDS="1", LEAVENOTRACE_PORT_WAIT_SECONDS="4",
                        PATH=self.fakebin + os.pathsep + os.environ["PATH"])
        self.env.pop("LEAVENOTRACE_SESSION_ID", None)
        self.env.pop("CLAUDE_ENV_FILE", None)
        self.mod = load_module()

    def tearDown(self):
        for p in self.procs:
            try:
                p.kill()
                p.wait(timeout=2)
            except Exception:
                pass
        # reap anything left over whose cwd lives in our temp dir (reparented test servers)
        mine = (self.tmp, os.path.realpath(self.tmp))
        for pid, cwd in cwd_of_all().items():
            if cwd.startswith(mine) and pid != os.getpid():
                try:
                    os.kill(pid, 9)
                except Exception:
                    pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- running things ------------------------------------------------------------
    def run_cli(self, *args, env=None, cwd=None, input=None):
        e = dict(self.env)
        e.update(env or {})
        return subprocess.run([sys.executable, CLI] + list(args), env=e, cwd=cwd or self.work, input=input,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=60)

    def hook(self, event, payload=None, sid=None, env=None, **extra):
        p = {"session_id": sid or self.sid, "cwd": self.work, "hook_event_name": event, "transcript_path": "/dev/null"}
        p.update(payload or {})
        p.update(extra)
        t0 = time.time()
        r = self.run_cli("hook", {"PreToolUse": "pre-tool-use", "PostToolUse": "post-tool-use", "Stop": "stop",
                                  "SessionStart": "session-start", "SessionEnd": "session-end"}[event],
                         env=env, input=json.dumps(p))
        r.elapsed = time.time() - t0
        r.json = json.loads(r.stdout) if r.stdout.strip() else None
        self.assertEqual(r.returncode, 0, r.stderr)
        return r

    def bash(self, command, phase, background=False, tid="toolu_1", **kw):
        ti = {"command": command}
        if background:
            ti["run_in_background"] = True
        payload = {"tool_name": "Bash", "tool_input": ti, "tool_use_id": tid}
        if phase == "post":
            payload["tool_response"] = {"stdout": "", "stderr": "", "interrupted": False}
        return self.hook("PreToolUse" if phase == "pre" else "PostToolUse", payload, **kw)

    def spawn(self, args, **kw):
        kw.setdefault("cwd", self.work)
        kw.setdefault("stdout", subprocess.DEVNULL)
        kw.setdefault("stderr", subprocess.DEVNULL)
        p = subprocess.Popen(args, **kw)
        self.procs.append(p)
        return p

    def registry(self, sid=None):
        with open(os.path.join(self.home, "sessions", (sid or self.sid) + ".json")) as f:
            return json.load(f)

    def save_registry(self, d, sid=None):
        with open(os.path.join(self.home, "sessions", (sid or self.sid) + ".json"), "w") as f:
            json.dump(d, f)

    def resources(self, rtype=None, state=None, sid=None):
        out = []
        for r in self.registry(sid)["resources"].values():
            if (rtype is None or r["type"] == rtype) and (state is None or r["state"] == state):
                out.append(r)
        return out

    def start_agent_server(self, port=None, tid="toolu_1", via="sh"):
        """Start `python3 -m http.server PORT` the way an agent would, then run the Pre/Post hooks."""
        port = port or free_port()
        cmd = "python3 -m http.server %d --bind 127.0.0.1" % port
        pre = self.bash(cmd + " &", "pre", tid=tid)
        self.assertIsNone(pre.json, pre.stdout)
        if via == "sh":
            subprocess.run(["sh", "-c", "nohup %s >/dev/null 2>&1 &" % cmd], cwd=self.work)
        else:
            self.spawn(["python3", "-m", "http.server", str(port), "--bind", "127.0.0.1"])
        self.assertTrue(wait_port(port))
        post = self.bash(cmd + " &", "post", tid=tid)
        return port, post

    def register_proc(self, popen, cmd_norm="sleep", sid=None, extra=None):
        """Put a live process into the registry by hand (for safety tests)."""
        mod = self.mod
        sid = sid or self.sid
        store = mod.Store(self.home)
        with store.txn(sid, create=dict(owner_pid=os.getpid(), owner_start=mod.start_token(os.getpid()), cwd=self.work)) as d:
            r = mod.add_resource(d, "process", pid=popen.pid, pid_start=mod.start_token(popen.pid), command=cmd_norm,
                                 command_norm=cmd_norm, cwd=self.work)
            if extra:
                r.update(extra)
            return r["id"]
