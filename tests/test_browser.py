import json
import os
import subprocess
import sys
import time
import unittest

from helpers import Base, wait_dead, pid_alive

PW = "mcp__playwright__"
DT = "mcp__chrome-devtools__"


def resp(text, as_dict=True):
    return {"content": [{"type": "text", "text": text}]} if as_dict else text


OLD_LIST = ("- Ran Playwright code:\n```js\nawait page.goto('http://localhost:3000/');\n```\n\n### Open tabs\n"
            "- 1: [Home] (http://localhost:3000/)\n- 2: (current) [Docs] (http://localhost:3000/docs)\n\n### Page state\n- Page URL: http://localhost:3000/docs\n")
NEW_LIST = "### Open tabs\n- 0: Home\n- 1: (current) Docs\n\n### Page\n- Page URL: http://localhost:3000/docs\n- Page Title: Docs\n"


class BrowserTabs(Base):
    def post(self, tool, ti, text, as_dict=True, tid="tb1"):
        return self.hook("PostToolUse", {"tool_name": tool, "tool_input": ti, "tool_response": resp(text, as_dict),
                                         "tool_use_id": tid})

    def pre(self, tool, ti, tid="tb1"):
        return self.hook("PreToolUse", {"tool_name": tool, "tool_input": ti, "tool_use_id": tid})

    def test_parse_tabs_formats(self):
        m = self.mod
        t = m.parse_tabs(OLD_LIST)
        self.assertEqual([(x["ref"], x["url"], x["current"]) for x in t],
                         [(1, "http://localhost:3000/", False), (2, "http://localhost:3000/docs", True)])
        t = m.parse_tabs(NEW_LIST)
        self.assertEqual([(x["ref"], x["url"], x["current"]) for x in t], [(0, None, False), (1, None, True)])
        t = m.parse_tabs("## Pages\n1: My Page (https://example.com) [selected]\n2: https://example.com/b\n")
        self.assertEqual([(x["ref"], x["url"], x["current"]) for x in t],
                         [(1, "https://example.com", True), (2, "https://example.com/b", False)])
        self.assertIsNone(m.parse_tabs("something unrelated 1: a"))
        self.assertIsNone(m.parse_tabs(""))

    def test_playwright_new_tab_registered_old_format(self):
        r = self.post(PW + "browser_tabs", {"action": "new", "url": "http://localhost:3000/docs"}, OLD_LIST)
        tabs = self.resources("browser_tab", "open")
        self.assertEqual(len(tabs), 1)
        self.assertEqual((tabs[0]["tab_ref"], tabs[0]["url"], tabs[0]["server"]), (2, "http://localhost:3000/docs", "playwright"))
        self.assertEqual(tabs[0]["tool_prefix"], PW)
        self.assertIn("registered tab-1", r.json["hookSpecificOutput"]["additionalContext"])

    def test_playwright_new_tab_registered_new_format_url_from_input(self):
        self.post(PW + "browser_tabs", {"action": "new", "url": "http://localhost:3000/docs"}, NEW_LIST, as_dict=False)
        tab = self.resources("browser_tab", "open")[0]
        self.assertEqual((tab["tab_ref"], tab["url"]), (1, "http://localhost:3000/docs"))

    def test_unknown_format_degrades_gracefully(self):
        r = self.post(PW + "browser_tabs", {"action": "new", "url": "http://x.test/a"}, "Tab created. 🎉 no list here")
        tab = self.resources("browser_tab", "open")[0]
        self.assertIsNone(tab["tab_ref"])
        self.assertEqual(tab["url"], "http://x.test/a")
        out = self.run_cli("close", "--all-mine", "--session", self.sid).stdout
        self.assertIn("http://x.test/a", out)
        self.assertIn("browser_tabs", out)
        # garbage / non-text response shapes never crash the hook
        for bad in (None, 42, [1, 2], {"weird": True}):
            self.hook("PostToolUse", {"tool_name": PW + "browser_snapshot", "tool_input": {}, "tool_response": bad, "tool_use_id": "z"})

    def test_duplicate_tab_denied_reuse_hint(self):
        self.post(PW + "browser_tabs", {"action": "new", "url": "http://localhost:3000/docs"}, OLD_LIST)
        for url in ("http://localhost:3000/docs", "http://127.0.0.1:3000/docs/", "HTTP://LOCALHOST:3000/docs#frag"):
            pre = self.pre(PW + "browser_tabs", {"action": "new", "url": url})
            out = pre.json["hookSpecificOutput"]
            self.assertEqual(out["permissionDecision"], "deny", url)
            self.assertIn("index=2", out["permissionDecisionReason"])
            self.assertIn("tab-1", out["permissionDecisionReason"])
        pre = self.pre(PW + "browser_tabs", {"action": "new", "url": "http://localhost:3000/other"})
        self.assertIsNone(pre.json)
        pre = self.pre(PW + "browser_tabs", {"action": "new", "url": "http://localhost:3000/docs#leavenotrace:force"})
        self.assertIsNone(pre.json)
        pre = self.pre(PW + "browser_tabs", {"action": "list"})
        self.assertIsNone(pre.json)

    def test_navigate_registers_implicit_first_tab_only_once(self):
        b = self.spawn([sys.executable, "-c", "import time; time.sleep(300)", "--user-data-dir=/x/ms-playwright/chromium-1"])
        time.sleep(0.3)
        self.post(PW + "browser_navigate", {"url": "http://localhost:5173/"}, "- Ran Playwright code\n### Page state\n- Page URL: http://localhost:5173/\n")
        self.assertEqual(len(self.resources("browser_tab", "open")), 1)
        self.post(PW + "browser_navigate", {"url": "http://localhost:5173/about"}, "### Page state\n- Page URL: http://localhost:5173/about\n")
        tabs = self.resources("browser_tab", "open")
        self.assertEqual(len(tabs), 1)
        self.assertEqual(tabs[0]["url"], "http://localhost:5173/about", "navigation updates the tab url")

    def test_navigate_without_owned_browser_is_not_claimed(self):
        # attach / extension mode: the current tab belongs to the user
        self.post(PW + "browser_navigate", {"url": "http://localhost:5173/"}, "### Page state\n- Page URL: http://localhost:5173/\n")
        self.assertEqual(self.resources("browser_tab"), [])

    def test_navigate_with_preexisting_tabs_is_not_claimed(self):
        self.post(PW + "browser_navigate", {"url": "http://localhost:3000/docs"}, OLD_LIST)
        self.assertEqual(self.resources("browser_tab"), [])

    def test_close_tool_marks_closed_and_shifts_indexes(self):
        self.post(PW + "browser_tabs", {"action": "new", "url": "http://localhost:3000/docs"}, OLD_LIST)  # index 2
        self.post(PW + "browser_tabs", {"action": "new", "url": "http://localhost:3000/third"},
                  "### Open tabs\n- 1: [Home] (http://localhost:3000/)\n- 2: [Docs] (http://localhost:3000/docs)\n- 3: (current) [T] (http://localhost:3000/third)\n")
        self.post(PW + "browser_tabs", {"action": "close", "index": 2},
                  "### Open tabs\n- 1: [Home] (http://localhost:3000/)\n- 2: (current) [T] (http://localhost:3000/third)\n")
        tabs = {t["url"]: t for t in self.resources("browser_tab")}
        self.assertEqual(tabs["http://localhost:3000/docs"]["state"], "closed")
        self.assertEqual(tabs["http://localhost:3000/third"]["state"], "open")
        self.assertEqual(tabs["http://localhost:3000/third"]["tab_ref"], 2)

    def test_close_all_mine_marks_pending_and_prints_exact_instructions(self):
        self.post(PW + "browser_tabs", {"action": "new", "url": "http://localhost:3000/docs"}, OLD_LIST)
        r = self.run_cli("close", "--all-mine", "--session", self.sid)
        self.assertIn("pending_close", r.stdout)
        self.assertIn('mcp__playwright__browser_tabs {"action": "close", "index": 2}', r.stdout)
        self.assertEqual(self.resources("browser_tab")[0]["state"], "pending_close")
        lst = self.run_cli("list", "--session", self.sid).stdout
        self.assertIn("close it:", lst)
        # agent follows the instruction -> PostToolUse closes the entry
        self.post(PW + "browser_tabs", {"action": "close", "index": 2}, "### Open tabs\n- 1: (current) [Home] (http://localhost:3000/)\n")
        self.assertEqual(self.resources("browser_tab")[0]["state"], "closed")
        self.assertIn("nothing open", self.run_cli("list", "--session", self.sid).stdout)

    def test_browser_close_marks_all_tabs_closed(self):
        self.post(PW + "browser_tabs", {"action": "new", "url": "http://localhost:3000/docs"}, OLD_LIST)
        self.post(PW + "browser_close", {}, "closed")
        self.assertEqual(self.resources("browser_tab", "open"), [])

    def test_devtools_new_page_close_page(self):
        pages = "## Pages\n1: Home (http://localhost:3000/)\n2: Docs (http://localhost:3000/docs) [selected]\n"
        self.post(DT + "new_page", {"url": "http://localhost:3000/docs"}, pages)
        tab = self.resources("browser_tab", "open")[0]
        self.assertEqual((tab["server"], tab["tab_ref"], tab["tab_ref_kind"]), ("devtools", 2, "pageId"))
        pre = self.pre(DT + "new_page", {"url": "http://localhost:3000/docs"})
        self.assertEqual(pre.json["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("pageId=2", pre.json["hookSpecificOutput"]["permissionDecisionReason"])
        out = self.run_cli("close", "--all-mine", "--session", self.sid).stdout
        self.assertIn('close_page {"pageId": 2}', out)
        self.post(DT + "close_page", {"pageId": 2}, "## Pages\n1: Home (http://localhost:3000/) [selected]\n")
        self.assertEqual(self.resources("browser_tab")[0]["state"], "closed")

    def test_chrome_extension_tab_best_effort(self):
        self.post("mcp__claude-in-chrome__tabs_create_mcp", {}, "Created new tab. Tab ID: 4242")
        tab = self.resources("browser_tab", "open")[0]
        self.assertEqual((tab["server"], tab["tab_ref"]), ("chrome-ext", 4242))
        out = self.run_cli("close", "--all-mine", "--session", self.sid).stdout
        self.assertIn("ungroup", out)

    def test_plugin_scoped_server_names_match(self):
        self.post("mcp__plugin_foo_playwright__browser_tabs", {"action": "new", "url": "http://a.test/"}, OLD_LIST)
        self.assertEqual(len(self.resources("browser_tab", "open")), 1)

    def test_stop_hook_nudges_once_for_expired_tabs(self):
        self.post(PW + "browser_tabs", {"action": "new", "url": "http://localhost:3000/docs"}, OLD_LIST)
        d = self.registry()
        d["resources"]["tab-1"]["last_used_at"] -= 3600
        self.save_registry(d)
        r = self.hook("Stop", {"stop_hook_active": False})
        ctx = r.json["hookSpecificOutput"]
        self.assertEqual(ctx["hookEventName"], "Stop")
        self.assertIn('browser_tabs {"action": "close", "index": 2}', ctx["additionalContext"])
        self.assertEqual(self.resources("browser_tab")[0]["state"], "pending_close")
        r2 = self.hook("Stop", {"stop_hook_active": False})
        self.assertEqual(r2.stdout.strip(), "", "no repeated nagging")

    def test_stop_hook_active_never_nudges(self):
        self.post(PW + "browser_tabs", {"action": "new", "url": "http://localhost:3000/docs"}, OLD_LIST)
        d = self.registry()
        d["resources"]["tab-1"]["last_used_at"] -= 3600
        self.save_registry(d)
        r = self.hook("Stop", {"stop_hook_active": True})
        self.assertEqual(r.stdout.strip(), "")

    def test_session_end_orphans_tabs_it_cannot_close(self):
        self.post(PW + "browser_tabs", {"action": "new", "url": "http://localhost:3000/docs"}, OLD_LIST)
        self.hook("SessionEnd", {"reason": "other"})
        time.sleep(0.8)
        self.assertEqual(self.resources("browser_tab")[0]["state"], "orphaned")


class BrowserProcess(Base):
    def fake_browser(self):
        return self.spawn([sys.executable, "-c", "import time; time.sleep(300)", "--user-data-dir=/tmp/x/ms-playwright/chromium-1"])

    def test_browser_process_owned_by_session_registered_and_killed_with_tabs(self):
        b = self.fake_browser()
        time.sleep(0.3)
        self.hook("PostToolUse", {"tool_name": PW + "browser_navigate", "tool_input": {"url": "http://localhost:3000/"},
                                  "tool_response": resp("### Page state\n- Page URL: http://localhost:3000/\n"), "tool_use_id": "b1"})
        bp = self.resources("browser_process", "open")
        self.assertEqual(len(bp), 1)
        self.assertEqual(bp[0]["pid"], b.pid)
        self.assertEqual(len(self.resources("browser_tab", "open")), 1)
        r = self.run_cli("close", "--all-mine", "--session", self.sid)
        self.assertTrue(wait_dead(b.pid))
        self.assertEqual(self.resources("browser_tab")[0]["state"], "closed")
        self.assertNotIn("pending_close", r.stdout)

    def test_browser_not_descended_from_owner_is_not_registered(self):
        # started by "someone else": its ancestry does not include the agent process
        helper = self.spawn(["sh", "-c", "setsid %s -c 'import time; time.sleep(300)' ms-playwright-x >/dev/null 2>&1 &" % sys.executable])
        helper.wait()
        time.sleep(0.4)
        self.hook("PostToolUse", {"tool_name": PW + "browser_navigate", "tool_input": {"url": "http://a.test/"},
                                  "tool_response": resp("### Page state\n- Page URL: http://a.test/\n"), "tool_use_id": "b2"})
        self.assertEqual(self.resources("browser_process"), [])


if __name__ == "__main__":
    unittest.main()
