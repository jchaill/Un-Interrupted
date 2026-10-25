#!/usr/bin/env python3
"""Find REPEATED transcript blocks — the universal ad signal.

Podcasts insert the same sponsor spot N times per episode (and sometimes the
same host-read ad across episodes). Identical inserts produce near-identical
transcript windows, whatever the language. This script slides windows of
consecutive segments (default 15–75 s of speech) over one or more transcripts,
groups near-duplicate occurrences (normalized-token Jaccard >= threshold), and
keeps only groups with >= 2 distinct occurrences (disjoint in time / file).
Songs/hallucinations collapse to one occurrence and are filtered out.

Usage:  find_repeats.py <tr1.json> [tr2.json ...] [--min-dur 15] [--max-dur 75]
                        [--sim 0.75]
Output: printed report + work/repeats_<tag>.json per transcript, where each
        group carries {"ranges": [[start,end],...]} in SECONDS. Review the
        report, curate, and pass curated ranges to detect_cuts.py --blocks.

Note: whisper may garble or skip part of an insertion (music-heavy openers);
a repeat found late inside a break means the break starts EARLIER — probe the
preceding gap with refine.sh/dip.py and extend the range manually.
"""
import json, re, subprocess, sys, os
from collections import defaultdict

def load(path):
    d = json.load(open(path))
    out = []
    for s in d["transcription"]:
        out.append((s["offsets"]["from"]/1000.0, s["offsets"]["to"]/1000.0, s["text"]))
    return out

NORM = re.compile(r"[\W_]+")
def toks(text):
    return NORM.sub(" ", text.lower()).split()

def dur_probe(p):
    r = subprocess.run(["ffprobe","-v","error","-show_entries","format=duration",
                        "-of","csv=p=0", p], capture_output=True, text=True)
    try: return float(r.stdout.strip())
    except ValueError: return None

def windows(segs, min_dur, max_dur):
    """One candidate per start segment: first crossing of min_dur, capped."""
    n = len(segs)
    for i in range(n):
        t0 = segs[i][0]; toks_w = []; t1 = t0
        for j in range(i, n):
            a, b, t = segs[j]
            if j == i: t1 = b
            else:
                t1 = b
            toks_w.extend(toks(t))
            if t1 - t0 >= max_dur: break
            if t1 - t0 >= min_dur and len(toks_w) >= 20:
                yield dict(i=i, j=j, t0=t0, t1=t1, toks=toks_w)
                break

def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    opts = {a.split("=")[0] if "=" in a else a: (a.split("=")[1] if "=" in a else True)
            for a in sys.argv[1:] if a.startswith("--")}
    min_d = float(opts.get("--min-dur", 15)); max_d = float(opts.get("--max-dur", 75))
    sim = float(opts.get("--sim", 0.75))
    if not args:
        sys.exit(__doc__)
    files = args

    members, shingles = [], defaultdict(list)
    for fi, path in enumerate(files):
        segs = load(path)
        ws = list(windows(segs, min_d, max_d))
        for w in ws:
            wid = len(members)
            w["file"] = os.path.basename(path); w["fi"] = fi
            members.append(w)
            tset = w["toks"]
            grams = {tuple(tset[k:k+8]) for k in range(max(1, len(tset)-7))}
            for g in grams:
                shingles[g].append(wid)
    print(f"windows: {len(members)} from {len(files)} transcript(s)")

    # candidate pairs sharing >= 3 distinctive 8-grams
    cand = set()
    for g, lst in shingles.items():
        if len(lst) > 400:          # shingle too common -> not distinctive
            continue
        if len(lst) < 2: continue
        for a in range(len(lst)):
            for b in range(a+1, len(lst)):
                cand.add((lst[a], lst[b]))
    # verify + union-find
    parent = list(range(len(members)))
    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]; x = parent[x]
        return x
    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb: parent[max(ra,rb)] = min(ra,rb)
    verified = 0
    for a, b in cand:
        A, B = members[a], members[b]
        sa, sb = set(A["toks"]), set(B["toks"])
        inter = len(sa & sb)
        if not inter: continue
        j = inter / len(sa | sb)
        if j >= sim:
            union(a, b); verified += 1
    # group members; count DISTINCT occurrences (disjoint spans per file)
    groups = defaultdict(list)
    for w_i in range(len(members)):
        groups[find(w_i)].append(w_i)
    report = {}
    any_found = False
    kept, kept_ranges = [], defaultdict(list)
    raw_groups = []
    for root, mem in sorted(groups.items()):
        if len(mem) < 2: continue
        # cluster overlapping members per file into occurrences
        byfile = defaultdict(list)
        for w in mem:
            A = members[w]
            byfile[A["fi"]].append(A)
        occ_by = defaultdict(list)
        for fi, lst in byfile.items():
            for A in lst: A["span"] = A["t1"] - A["t0"]
            lst.sort(key=lambda A: A["t0"])
            cur = None
            clusters = []
            for A in lst:
                if cur is None: cur = [A["t0"], A["t1"], A]; continue
                overlap = min(cur[1], A["t1"]) - max(cur[0], A["t0"])
                if overlap > 0.4 * min(cur[1]-cur[0], A["span"]):
                    cur = [cur[0], max(cur[1], A["t1"]), cur[2]]
                else:
                    clusters.append(cur); cur = [A["t0"], A["t1"], A]
            if cur: clusters.append(cur)
            # anchors inside one insertion produce near-adjacent clusters — merge
            for o in clusters:
                fo = occ_by[fi]
                if fo and o[0] - fo[-1][1] < 15: fo[-1][1] = max(fo[-1][1], o[1])
                else: fo.append(o)
        occ = [(o[0], o[1], o[2]) for fi, ol in occ_by.items() for o in ol]
        occ.sort(key=lambda o: (o[2]["fi"], o[0]))
        reps = len(occ)
        if reps < 2: continue
        rep = max((members[w] for w in mem), key=lambda A: A["t1"]-A["t0"])
        if len(set(rep["toks"])) < 25: continue   # hallucinations / catchphrases
        raw_groups.append((rep, occ))
    # dedupe: skip groups where every occurrence is mostly inside an already-
    # reported group's occurrence in the SAME file (overlapping window anchors)
    for rep, occ in raw_groups:
        dup = False
        for ks, ke in kept:
            cov = True
            for (t0, t1, A) in occ:
                rs = kept_ranges.get(A["fi"], [])
                inside = any(max(0, min(t1, r1) - max(t0, r0)) > 0.6 * (t1 - t0)
                             for r0, r1 in rs)
                if not inside: cov = False; break
            if cov and occ: dup = True; break
        if dup: continue
        kept.append((rep["t0"], rep["t1"]))
        for (t0, t1, A) in occ:
            kept_ranges.setdefault(A["fi"], []).append((t0, t1))
        any_found = True
        print(f"\nREPEAT GROUP: {len(occ)} occurrences | rep text: {' '.join(rep['toks'])[:110]}…")
        per = defaultdict(list)
        for (t0, t1, A) in occ:
            print(f"   [{A['file']:28s}] {t0:9.1f} - {t1:9.1f}  ({t1-t0:5.1f}s)")
            per[A["fi"]].append([round(t0,1), round(t1,1)])
        tag = os.path.basename(files[0]).replace("tr_","").replace(".json","")
        report.setdefault(tag, {"groups": []})["groups"].append(
            {"ranges": per[0], "n": len(occ), "text": " ".join(rep["toks"])[:200]})
    for fi, path in enumerate(files):
        tag = os.path.basename(path).replace("tr_","").replace(".json","")
        os.makedirs("work", exist_ok=True)
        json.dump(report.get(tag, {"groups": []}),
                  open(f"work/repeats_{tag}.json", "w"))
    if not any_found: print("no repeated blocks found (single-occurrence content only)")

if __name__ == "__main__":
    main()