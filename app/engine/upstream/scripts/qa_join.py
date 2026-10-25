#!/usr/bin/env python3
"""Verify every seam (and head/tail) of a cut output file.

- Computes seam positions in output space from cuts.json
- Transcribes a fresh window around each seam (+ head + tail) via refine.sh
- FAILS (exit 1) if any ad-signature text appears in those windows

Usage: qa_join.py <out.mp3> <cuts.json>
"""
import json, re, subprocess, sys, os

SIGNATURE = None
def load_signature():
    global SIGNATURE
    pats = [re.sub(r"^\(\?i\)", "", r) for r, _ in load_labels()]
    if not pats:
        pats = [r"(brought to you by|sponsored( this| by)|pr[eé]sent[eé] par|en partenariat|use (the )?(promo )?code|code promo|voir conditions|for (a )?free (trial|quote))"]
    SIGNATURE = re.compile("|".join(f"({p})" for p in pats), re.I)

def load_labels():
    p = os.path.join("work", "labels.json")
    if os.path.exists(p):
        return [(r, n) for r, n in json.load(open(p))]   # raw regex strings
    return []

def dur(path):
    r = subprocess.run(["ffprobe","-v","error","-show_entries","format=duration",
                        "-of","csv=p=0", path], capture_output=True, text=True)
    return float(r.stdout.strip())

def window(mp3, center, hw, tag):
    r = subprocess.run(["bash", os.path.join(script_dir, "refine.sh"),
                        mp3, str(center), str(hw), tag],
                       capture_output=True, text=True)
    return r.stdout.strip(), r.returncode

script_dir = os.path.dirname(os.path.abspath(__file__))
out, cuts_file = sys.argv[1], sys.argv[2]
load_signature()
cuts = json.load(open(cuts_file))
total = dur(out)

# seams in output space: position of each cut's START edge after prior removals
removed, seams = 0.0, []
for i, (s, e) in enumerate(cuts):
    seams.append((i, s - removed, e))
    removed += (e if e is not None else 0.0) - s
# EOF cuts: no seam after (nothing kept); tail is checked separately
seam_list = [(i, pos) for i, pos, e in seams if e is not None and 10 <= pos <= total - 8]

fail = []
seen = set()
for i, pos in seam_list:
    tag = f"qa_seam{i}_{int(pos)}"
    txt, rc = window(out, pos + 3, 9, tag)
    print(f"--- seam{i} @ output {pos:.1f}s {'(OK transcribed)' if rc == 0 else '(REFINE FAILED!)'}")
    print(txt if txt else "  <no speech>")
    if rc != 0: fail.append(f"seam{i}: refine failed (stale/missing window?)")
    m = SIGNATURE.search(txt)
    if m: fail.append(f"seam{i}: ad signature {m.group(0)!r} leaked: {SIGNATURE.search(txt)}")
    seen.add(tag)

print("--- head (first 25s)")
txt, rc = window(out, 12, 11, "qa_head")
print(txt if txt else "  <no speech>")
m = SIGNATURE.search(txt)
if m: fail.append(f"head: ad signature {m.group(0)!r} leaked")

print("--- tail (last 25s)")
txt, rc = window(out, total - 12, 11, "qa_tail")
print(txt if txt else "  <no speech>")
m = SIGNATURE.search(txt)
if m: fail.append(f"tail: ad signature {m.group(0)!r} leaked")

print(f"output duration: {total:.2f}s")
if fail:
    for f in fail: print("FAIL:", f)
    sys.exit(1)
print("QA PASS: no ad signatures at any seam, head or tail")