#!/usr/bin/env python3
"""Resolve real file paths robustly: matches a substring against filenames
after Unicode NFC-normalization on both sides — immune to decomposed forms
(macOS NFD "travaillé" = ...l-l-e-◌́) that break shell globs on ASCII-looking
substrings. Prints one full path per match (exit 1 if none).

Usage: findpath.py <substring> [directory]     # substring is also NFC-normalized
"""
import glob, os, sys, unicodedata

def nfc(s):
    return unicodedata.normalize("NFC", s)

pat = nfc(sys.argv[1])
directory = sys.argv[2] if len(sys.argv) > 2 else "."
hits = []
for f in sorted(os.listdir(directory)):
    if pat in nfc(f):
        hits.append(os.path.join(directory, f) if directory != "." else f)
if not hits:
    print(f"no match for {pat!r} in {directory}", file=sys.stderr)
    sys.exit(1)
print("\n".join(hits))