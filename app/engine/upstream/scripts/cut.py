#!/usr/bin/env python3
"""Remove ad segments from an MP3 via stream copy.
Usage: cut.py <in.mp3> <out.mp3> <cuts.json>   # cuts.json: [[start,end|null],...]
"""
import json, subprocess, sys, os, tempfile, shutil

def dur(path):
    r = subprocess.run(["ffprobe","-v","error","-show_entries","format=duration",
                        "-of","csv=p=0", path], capture_output=True, text=True)
    return float(r.stdout.strip())

in_path, out_path, cuts_file = sys.argv[1], sys.argv[2], sys.argv[3]
cuts = [(float(a), float(b) if b else None) for a, b in json.load(open(cuts_file))]
total = dur(in_path)
cuts = [(max(0, s), e if e is not None else total) for s, e in cuts]
cuts.sort()
# complement = keep ranges
keeps, prev_end = [], 0.0
for s, e in cuts:
    if s > prev_end + 0.2: keeps.append((prev_end, s))
    prev_end = max(prev_end, e)
if total - prev_end > 0.2: keeps.append((prev_end, total))
removed = sum(total for (s, e) in [x for x in cuts])
removed_len = sum(e - s for s, e in cuts)

# container handling: MP3 = proven stream-copy path; other containers skip the
# mp3-only flags; CUT_REENCODE='<codec> <bitrate>' (e.g. "aac 128k") re-encodes
# segments instead of stream-copying them (use if a container misbehaves in concat)
IS_MP3 = os.path.splitext(in_path)[1].lower() == ".mp3"
REENC = os.environ.get("CUT_REENCODE", "").split()
CODEC = ["-c:a", REENC[0]] + (["-b:a", REENC[1]] if len(REENC) > 1 else []) if REENC else ["-c:a", "copy"]
XING  = ["-write_xing", "0"] if IS_MP3 else []
ID3V2 = ["-id3v2_version", "3"] if IS_MP3 else []

os.makedirs("work", exist_ok=True)
tmp = tempfile.mkdtemp(prefix="cuts_", dir="work")
try:
    listfile = os.path.join(tmp, "list.txt")
    for i, (s, e) in enumerate(keeps):
        seg = os.path.join(tmp, f"seg_{i:02d}{'.mp3' if IS_MP3 else os.path.splitext(in_path)[1]}")
        r = subprocess.run(["ffmpeg","-hide_banner","-loglevel","error",
            "-ss", f"{s:.3f}", "-to", f"{e:.3f}", "-i", in_path,
            *CODEC,"-map_metadata","-1", *XING, seg])
        if r.returncode: sys.exit(f"segment {i} failed")
        listfile_line = f"file '{seg}'"
        open(listfile, "a").write(listfile_line + "\n")
    r = subprocess.run(["ffmpeg","-hide_banner","-loglevel","error","-y",
        "-f","concat","-safe","0","-i", listfile, "-i", in_path,
        "-map","0:a","-map_metadata","1", *CODEC, *ID3V2, out_path])
    if r.returncode: sys.exit("concat failed")
finally:
    shutil.rmtree(tmp, ignore_errors=True)   # no leftovers, even on failure
outd = dur(out_path)
expected = total - removed_len
print(f"in={total:.2f}s removed={removed_len:.2f}s ({len(cuts)} cuts) -> out={outd:.2f}s (expected {expected:.2f}, delta {outd-expected:+.2f}s)")
print(f"keep ranges: " + ", ".join(f"{s:.1f}-{e:.1f}" for s,e in keeps))
