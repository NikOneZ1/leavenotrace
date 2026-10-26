"""iOS simulator handling, with a mocked `xcrun`."""
import json
import os
import subprocess
import unittest
import uuid

from helpers import Base

FAKE_XCRUN = r'''#!/usr/bin/env python3
import json, os, sys
st, log = os.environ["FAKE_SIM_STATE"], os.environ["FAKE_SIM_LOG"]
args = sys.argv[1:]
with open(log, "a") as f:
    f.write(" ".join(args) + "\n")
d = json.load(open(st))
if args[:3] == ["simctl", "list", "devices"]:
    print(json.dumps({"devices": {"com.apple.CoreSimulator.SimRuntime.iOS-17-5": d}}))
elif args[:2] == ["simctl", "boot"]:
    for x in d:
        if args[2] in (x["udid"], x["name"]):
            x["state"] = "Booted"
    json.dump(d, open(st, "w"))
elif args[:2] == ["simctl", "shutdown"]:
    for x in d:
        if args[2] == x["udid"] or args[2] == "all":
            x["state"] = "Shutdown"
    json.dump(d, open(st, "w"))
'''


class Simulators(Base):
    def setUp(self):
        super().setUp()
        x = os.path.join(self.fakebin, "xcrun")
        with open(x, "w") as f:
            f.write(FAKE_XCRUN)
        os.chmod(x, 0o755)
        self.A, self.B, self.C = (str(uuid.uuid4()).upper() for _ in range(3))
        self.state = os.path.join(self.tmp, "sims.json")
        self.log = os.path.join(self.tmp, "xcrun.log")
        self.set_sims({self.A: "Shutdown", self.B: "Shutdown", self.C: "Shutdown"})
        self.env.update(FAKE_SIM_STATE=self.state, FAKE_SIM_LOG=self.log)
        os.environ["FAKE_SIM_STATE"], os.environ["FAKE_SIM_LOG"] = self.state, self.log  # for the in-process module
        self.addCleanup(lambda: [os.environ.pop(k, None) for k in ("FAKE_SIM_STATE", "FAKE_SIM_LOG")])

    def set_sims(self, states):
        names = {self.A: "iPhone 15", self.B: "iPhone 15 Pro", self.C: "iPad Air"}
        with open(self.state, "w") as f:
            json.dump([{"udid": u, "name": names[u], "state": s, "isAvailable": True} for u, s in states.items()], f)

    def sim_state(self):
        return {d["udid"]: d["state"] for d in json.load(open(self.state))}

    def xcrun(self, *args):
        subprocess.run(["xcrun"] + list(args), env=self.env, check=True)

    def calls(self):
        return open(self.log).read().splitlines() if os.path.exists(self.log) else []

    def test_boot_when_nothing_booted_allowed_and_registered_owned(self):
        cmd = "xcrun simctl boot %s" % self.A
        self.assertIsNone(self.bash(cmd, "pre").json)
        self.xcrun("simctl", "boot", self.A)
        post = self.bash(cmd, "post")
        sims = self.resources("simulator", "open")
        self.assertEqual([(s["udid"], s["owned"]) for s in sims], [(self.A, True)])
        self.assertIn("registered sim-1", post.json["hookSpecificOutput"]["additionalContext"])

    def test_boot_denied_when_another_is_booted_even_if_not_owned(self):
        self.set_sims({self.A: "Shutdown", self.B: "Booted", self.C: "Shutdown"})
        pre = self.bash("xcrun simctl boot %s" % self.A, "pre")
        out = pre.json["hookSpecificOutput"]
        self.assertEqual(out["permissionDecision"], "deny")
        self.assertIn(self.B, out["permissionDecisionReason"])
        self.assertIn("not opened by this agent", out["permissionDecisionReason"])
        self.assertIn("leavenotrace:force", out["permissionDecisionReason"])
        forced = self.bash("xcrun simctl boot %s # leavenotrace:force" % self.A, "pre", tid="t2")
        self.assertIsNone(forced.json)

    def test_boot_of_name_denied_too_and_owned_flag_in_message(self):
        cmd = "xcrun simctl boot %s" % self.A
        self.bash(cmd, "pre")
        self.xcrun("simctl", "boot", self.A)
        self.bash(cmd, "post")
        pre = self.bash('xcrun simctl boot "iPad Air"', "pre", tid="t3")
        r = pre.json["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("opened by this session", r)
        self.assertIn(self.A, r)

    def test_not_claimed_when_already_booted_before(self):
        self.set_sims({self.A: "Booted", self.B: "Shutdown", self.C: "Shutdown"})
        cmd = "xcrun simctl boot %s" % self.A
        self.assertIsNone(self.bash(cmd, "pre").json, "booting the already-booted target is a harmless no-op")
        self.bash(cmd, "post")
        self.assertEqual(self.resources("simulator"), [])

    def test_close_shuts_down_only_owned_never_all(self):
        cmd = "xcrun simctl boot %s" % self.A
        self.bash(cmd, "pre")
        self.xcrun("simctl", "boot", self.A)
        self.bash(cmd, "post")
        # a simulator the *user* booted afterwards
        self.set_sims({self.A: "Booted", self.B: "Booted", self.C: "Shutdown"})
        r = self.run_cli("close", "--all-mine", "--session", self.sid)
        self.assertEqual(self.sim_state(), {self.A: "Shutdown", self.B: "Booted", self.C: "Shutdown"}, r.stdout + r.stderr)
        shutdowns = [c for c in self.calls() if "shutdown" in c]
        self.assertEqual(shutdowns, ["simctl shutdown %s" % self.A])
        self.assertFalse([c for c in self.calls() if c.endswith(" all")])

    def test_non_owned_registered_simulator_is_never_shut_down(self):
        self.set_sims({self.A: "Booted", self.B: "Shutdown", self.C: "Shutdown"})
        r = self.run_cli("register", "--type", "simulator", "--udid", self.A, "--not-owned", "--session", self.sid)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = self.run_cli("close", "--all-mine", "--session", self.sid).stdout
        self.assertEqual(self.sim_state()[self.A], "Booted")
        self.assertEqual([c for c in self.calls() if "shutdown" in c], [])
        self.assertIn("not owned", out)

    def test_tampered_registry_with_non_udid_is_refused(self):
        store = self.mod.Store(self.home)
        with store.txn(self.sid, create=dict(owner_pid=os.getpid(), owner_start=self.mod.start_token(os.getpid()), cwd=self.work)) as d:
            self.mod.add_resource(d, "simulator", udid="all", owned=True)
        self.run_cli("close", "--all-mine", "--session", self.sid)
        self.assertEqual([c for c in self.calls() if "shutdown" in c], [])

    def test_ambiguous_new_boots_are_not_claimed(self):
        cmd = "xcrun simctl boot booted"
        self.bash(cmd, "pre")
        self.xcrun("simctl", "boot", self.A)
        self.xcrun("simctl", "boot", self.B)  # e.g. the user booted one at the same moment
        self.bash(cmd, "post")
        self.assertEqual(self.resources("simulator"), [])

    def test_xcodebuildmcp_boot_sim_tool(self):
        tool = "mcp__XcodeBuildMCP__boot_sim"
        self.hook("PreToolUse", {"tool_name": tool, "tool_input": {"simulatorId": self.A}, "tool_use_id": "x1"})
        self.xcrun("simctl", "boot", self.A)
        self.hook("PostToolUse", {"tool_name": tool, "tool_input": {"simulatorId": self.A}, "tool_response": "ok", "tool_use_id": "x1"})
        self.assertEqual(self.resources("simulator", "open")[0]["udid"], self.A)
        pre = self.hook("PreToolUse", {"tool_name": tool, "tool_input": {"simulatorId": self.B}, "tool_use_id": "x2"})
        self.assertEqual(pre.json["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_without_xcrun_everything_is_inert(self):
        os.remove(os.path.join(self.fakebin, "xcrun"))
        pre = self.bash("xcrun simctl boot %s" % self.A, "pre")
        self.assertIsNone(pre.json)
        self.bash("xcrun simctl boot %s" % self.A, "post")
        self.assertEqual(self.resources("simulator"), [])


if __name__ == "__main__":
    unittest.main()
