#!/usr/bin/env python3
"""Build leavenotrace.zip (no zip binary needed). Usage: python3 scripts/package.py [out.zip]"""
import os
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
name = os.path.basename(ROOT)
out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(ROOT), name + ".zip")
SKIP = {".git", "__pycache__", ".DS_Store"}
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    for dp, dns, fns in os.walk(ROOT):
        dns[:] = sorted(d for d in dns if d not in SKIP)
        for fn in sorted(fns):
            if fn in SKIP or fn.endswith((".pyc", ".zip")):
                continue
            p = os.path.join(dp, fn)
            zi = zipfile.ZipInfo.from_file(p, os.path.join(name, os.path.relpath(p, ROOT)))
            zi.compress_type = zipfile.ZIP_DEFLATED
            with open(p, "rb") as f:
                z.writestr(zi, f.read())
print("wrote", out)
