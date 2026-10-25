#!/usr/bin/env python3
"""End-of-run recap: per-file lengths, ads removed (total + sponsor titles),
QC status. Run AFTER verify_audio.sh and BEFORE cleanup --all.

Auto-discovery, from the directory containing the MP3s:
  pairs  : "<base>.mp3"  +  "<base> (sans pubs).mp3"
  tag    : work/index.json (tag -> original mp3), as maintained by transcribe.sh
  tr     : work/tr_<tag>.json       cuts: work/cuts_<tag>.json
  QC     : work/verify.log lines (PASS|FAIL|file|duration|bitrate)

Sponsor titles come from SPONSORS below (add a pattern + title when a new
campaign appears — this is what the per-episode review turns up); override or
extend with work/labels.json ([regex, "title"] pairs) without editing code.

Writes work/recap.txt and prints it. Exit 0 if everything paired, else 1.
"""
import json, os, re, subprocess, sys, glob, unicodedata

def nfc(s):
    return unicodedata.normalize("NFC", s)

SUFFIXES = [" (no ads).mp3", " (sans pubs).mp3"]   # new standard + legacy files

# Sponsor titles come from work/labels.json (["regex", "Title"], ...) — the same
# calibration file detect_cuts.py and qa_join.py use. Unrecognized ads fall
# back to a transcript snippet so they get mapped during review.

def dur(p):
    r = subprocess.run(["ffprobe","-v","error","-show_entries","format=duration",
                        "-of","csv=p=0", p], capture_output=True, text=True)
    return float(r.stdout.strip())

def mmss(sec):
    s = int(round(max(0, sec)))
    return f"{s//60}:{s%60:02d}"

def load_labels():
    p = os.path.join("work", "labels.json")
    if os.path.exists(p):
        return [(re.compile(pat), name) for pat, name in json.load(open(p))]
    return []

LABELS = load_labels()

def sponsor_for(text):
    for rx, name in LABELS:
        if rx.search(text): return name
    snippet = text.strip()[:48]
    return f"Sponsor non identifié (à mapper : «{snippet}…»)" if snippet else None

def main():
    index = {}
    ipath = os.path.join("work", "index.json")
    if os.path.exists(ipath): index = json.load(open(ipath))
    by_base = {nfc(os.path.basename(v["mp3"]))[:-4]: (t, v) for t, v in index.items()}

    qc = {}
    qpath = os.path.join("work", "verify.log")
    if os.path.exists(qpath):
        for line in open(qpath):
            parts = line.strip().split("|")
            if len(parts) >= 2: qc[nfc(parts[1])] = parts[0]

    mp3s = sorted(glob.glob("*.mp3") + glob.glob("*/*.mp3"))   # episodes may live in subfolders
    originals = [f for f in mp3s if not any(f.endswith(s) for s in SUFFIXES)]
    out_files = mp3s
    lines, rows, problems = [], [], []
    t_orig = t_out = t_ads = 0.0
    lines.append("=" * 74)
    lines.append(" ADS REMOVAL RECAP")
    lines.append("=" * 74)
    n = 0
    for mp3 in originals:
        base = nfc(os.path.basename(mp3))[:-4]
        disp = os.path.basename(mp3)
        matches = [f for f in out_files
                   for s in SUFFIXES
                   if nfc(os.path.basename(f)).endswith(s)
                   and nfc(os.path.basename(f))[:-len(s)] == base]
        out = matches[0] if matches else base + SUFS
        n += 1
        if not os.path.exists(out):
            problems.append(f"{mp3}: pas de version (sans pubs)")
            continue
        d_in, d_out = dur(mp3), dur(out)
        info = by_base.get(base)
        rangelist, labels = [], {}
        if info:
            tag = info[0]
            cutp = os.path.join("work", f"cuts_{tag}.json")
            trp = os.path.join("work", f"tr_{tag}.json")
            if os.path.exists(cutp) and os.path.exists(trp):
                cuts = json.load(open(cutp))
                segs = [(s["offsets"]["from"]/1000, s["offsets"]["to"]/1000, s["text"])
                        for s in json.load(open(trp))["transcription"]]
                for s, e in cuts:
                    e2 = e if e is not None else dur(mp3)
                    txt = " ".join(t for a, b, t in segs if b > s - 1.0 and a < e2 + 1.0)
                    name = sponsor_for(txt)
                    labels.setdefault(name, []).append((s, e2))
                    rangelist.append(f"{mmss(s)}–{mmss(e2)}")
            else:
                problems.append(f"{mp3}: transcript/cut-plan manquant (tag {tag})")
        else:
            problems.append(f"{mp3}: pas d'entrée work/index.json pour ce fichier")
        qcst = qc.get(nfc(os.path.basename(out)), "n/a")
        removed = d_in - d_out
        t_orig += d_in; t_out += d_out; t_ads += removed
        lines.append("")
        lines.append(f"{n}. {disp}")
        lines.append(f"   original length    : {mmss(d_in)}")
        lines.append(f"   (no ads) length    : {mmss(d_out)}  (−{mmss(removed)})")
        lines.append(f"   ads removed        : {mmss(removed)} ({sum(len(v) for v in labels.values())} breaks)")
        for name in sorted(labels, key=lambda k: str(k)):
            totl = sum(e - s for s, e in labels[name])
            lines.append(f"      - {name}: ×{len(labels[name])} ({mmss(totl)})  [{', '.join(rangelist)}]")
        lines.append(f"   QC                 : {qcst} (verify_audio.sh)")
        rows.append((base, d_in, d_out, removed))
    lines.append("")
    lines.append("-" * 74)
    if rows:
        lines.append(f"TOTAL original {mmss(t_orig)} → (no ads) {mmss(t_out)}   |   ads removed {mmss(t_ads)}")
    if problems:
        lines.append("")
        lines.append("NEEDS ATTENTION:")
        for p in problems: lines.append(f"  - {p}")
    lines.append("=" * 74)
    report = "\n".join(lines) + "\n"
    os.makedirs("work", exist_ok=True)
    open(os.path.join("work", "recap.txt"), "w").write(report)
    print(report, end="")
    return 1 if problems else 0

if __name__ == "__main__":
    sys.exit(main())