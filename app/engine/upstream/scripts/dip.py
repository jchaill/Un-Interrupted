#!/usr/bin/env python3
"""List real RMS dips near a transcript boundary. Usage: dip.py <mp3> <center> [halfwidth]"""
import subprocess, sys, re
mp3 = sys.argv[1]; t0 = float(sys.argv[2])
hw = float(sys.argv[3]) if len(sys.argv) > 3 else 3.0
s, e = max(0, t0 - hw), t0 + hw
p = subprocess.run(
    ["ffmpeg","-hide_banner","-loglevel","error","-ss",str(s),"-t",str(e-s),"-i",mp3,
     "-af","astats=metadata=1:reset=1,ametadata=print:key=lavfi.astats.Overall.RMS_level:file=-",
     "-f","null","-"], capture_output=True, text=True)
pts = []
t = None
for line in p.stdout.splitlines():
    if "pts_time" in line:
        m = re.search(r"pts_time:([\d.]+)", line)
        t = float(m.group(1)) + s
    else:
        m = re.search(r"RMS_level=(-?[\d.]+|-inf)", line)
        if m and t is not None:
            v = m.group(1)
            pts.append((t, -120.0 if v == "-inf" else float(v)))
# exclude window edges (decoder priming artifacts) and points outside +-2.75s
pts = [(tt,v) for tt,v in pts if s+1.2 < tt < e-0.35 and abs(tt-t0) <= 2.75]
if not pts:
    print(f"  no points near {t0:.2f}"); sys.exit(0)
med = sorted(v for _, v in pts)[len(pts)//2]
dips = []
for i in range(1, len(pts)-1):
    tt, v = pts[i]
    if v < med - 10 and v <= pts[i-1][1] and v <= pts[i+1][1]:
        dips.append((tt, v))
if not dips:
    best = min(pts, key=lambda kv: kv[1])
    print(f"  NO DIP (median {med:.1f}); quietest {best[0]:.2f} @{best[1]:.1f} dB")
else:
    out = " | ".join(f"{tt:.2f}@{v:.1f}dB" for tt, v in dips)
    print(f"  median {med:.1f} | dips: {out}")
