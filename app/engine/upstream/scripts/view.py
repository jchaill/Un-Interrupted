import json, sys
d = json.load(open(sys.argv[1]))
a, b = float(sys.argv[2])*1000, float(sys.argv[3])*1000
for s in d["transcription"]:
    if s["offsets"]["to"] >= a and s["offsets"]["from"] <= b:
        t0, t1 = s["offsets"]["from"]/1000., s["offsets"]["to"]/1000.
        print(f"[{t0:9.2f} -> {t1:9.2f}] {s['text']}")
