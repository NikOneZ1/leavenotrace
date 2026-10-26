#!/usr/bin/env python3
"""Rename the product everywhere (code constants, plugin manifest, hooks, skill, command, docs, tests).

    python3 scripts/rename.py --name tidyup [--display TidyUp]

The three spellings used throughout the repo are replaced consistently:
    leavenotrace -> <name>      (CLI, plugin name, state dir ~/.<name>, marker `<name>:force`)
    LEAVENOTRACE -> <NAME>      (env var prefix)
    LeaveNoTrace -> <display>   (human-readable)
and bin/leavenotrace is renamed to bin/<name>, commands/leavenotrace.md to commands/<name>.md.
"""
import argparse
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OLD = ("leavenotrace", "LEAVENOTRACE", "LeaveNoTrace")
SKIP_DIRS = {".git", "__pycache__"}
TEXT_EXT = (".py", ".md", ".json", ".sh", ".snippet", ".txt", "")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True, help="new lowercase name (CLI + plugin name), e.g. tidyup")
    ap.add_argument("--display", help="new display name, e.g. TidyUp (default: --name)")
    ap.add_argument("--root", default=ROOT)
    a = ap.parse_args()
    if not re.fullmatch(r"[a-z][a-z0-9-]{1,40}", a.name):
        sys.exit("--name must be kebab-case: lowercase letters, digits, hyphens")
    if a.name.startswith(("claude-", "anthropic-")) or a.name in ("claude", "anthropic"):
        sys.exit("plugin names starting with claude-/anthropic- are reserved by Claude Code")
    display = a.display or a.name
    upper = a.name.upper().replace("-", "_")
    n = 0
    for dp, dns, fns in os.walk(a.root):
        dns[:] = [d for d in dns if d not in SKIP_DIRS]
        for fn in fns:
            p = os.path.join(dp, fn)
            if fn.endswith(".zip") or os.path.splitext(fn)[1] not in TEXT_EXT:
                continue
            try:
                s = open(p, encoding="utf-8").read()
            except Exception:
                continue
            t = s.replace("LeaveNoTrace", display).replace("LEAVENOTRACE", upper).replace("leavenotrace", a.name)
            if t != s:
                open(p, "w", encoding="utf-8").write(t)
                n += 1
    for old, new in ((os.path.join(a.root, "bin", "leavenotrace"), os.path.join(a.root, "bin", a.name)),
                     (os.path.join(a.root, "commands", "leavenotrace.md"), os.path.join(a.root, "commands", a.name + ".md"))):
        if os.path.exists(old) and old != new:
            os.rename(old, new)
    print("renamed in %d files: leavenotrace -> %s, LEAVENOTRACE -> %s, LeaveNoTrace -> %s" % (n, a.name, upper, display))
    print("Review README.md, then re-run the tests: python3 -m unittest discover -s tests")


if __name__ == "__main__":
    main()
