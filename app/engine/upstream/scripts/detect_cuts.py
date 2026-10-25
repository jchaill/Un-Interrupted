#!/usr/bin/env python3
"""Detect ad blocks in a whisper.cpp transcript and emit cut JSON + review report.

Works on any podcast / language. Ad-block SEEDS come from (in order of union):
  1. work/labels.json  — per-podcast calibration: [["regex", "Title"], ...]
     (the same file recap.py and qa_join.py use)
  2. --sig "regex"     — extra patterns on the command line (repeatable)
  3. built-in generic sponsor-language cues (bilingual FR/EN)
  4. --blocks file.json — explicit [[start,end],...] ranges in seconds,
     typically curated output of find_repeats.py (repeat = universal ad signal)
Boundary refinement uses RMS dips (works because podcast ads are bracketed by
jingles/edits, not silence). Interior text between seeds of a block belongs to
the ad; blocks merge when <= 25s apart; tails extend through ad-tag text.

Usage: detect_cuts.py <in.mp3> <tr.json> <out.cuts.json> \
         [--sig "regex"] [--labels work/labels.json] [--blocks blocks.json]
Env:   AD_LANG=fr   -> also flag English text inside French episodes
       AD_SHOW_STOP=regex: segments matching this (show re-entry promos, sung
       idents) are never absorbed into a block (keeps them out of cuts).
"""
import json, os, re, subprocess, sys

SMEAR   = re.compile(r"(?i)^\s*(rires?|applaudissements?|musique|applause|sifflets?|rires)\b[\s.!]*$")
GENERIC = r"(?i)(brought to you by|sponsored( this| by)|thanks? to .{0,40} (for|des) (sponsoring|la pub)|pr[eé]sent[eé] par|en partenariat|offert par|avec le(s)? code|use (the )?(promo )?code|code promo|promo code|voir conditions|conditions sur|for (a )?free (trial|quote)|essai gratuit|gratuitement avec)"
DEFAULT_STOP = r"(?i)^\s*(rtl\b|le kiki|kiki express|express\.?)"
EN = re.compile(r"(?i)\b(the|and|every|style|store|furniture|home|free|enjoy|this|coming|see)\b")

TAIL  = re.compile(r"(?i)(voir conditions|conditions sur|\.(com|fr|net|io)\b|every style,? every home|come see|in store|offre|valable|use (the )?code|code promo|brought to you|sponsored)"
)
PROMO_DEFAULT = r"(grosses t[eê]|grosset[eê]|ruquier|sur rtl)"
JINGLE_DEFAULT = r"^\s*(rtl\b|le kiki|kiki express|express\.?)"

def segments(path):
    d = json.load(open(path))
    return [(s["offsets"]["from"]/1000.0, s["offsets"]["to"]/1000.0, s["text"].strip())
            for s in d["transcription"]]

def rms_dips(mp3, lo, hi):
    s, e = max(0, lo - 0.5), hi + 0.6
    p = subprocess.run(["ffmpeg","-hide_banner","-loglevel","error","-ss",str(s),
        "-t",str(e-s),"-i",mp3,
        "-af","astats=metadata=1:reset=1,ametadata=print:key=lavfi.astats.Overall.RMS_level:file=-",
        "-f","null","-"], capture_output=True, text=True)
    pts, t = [], None
    for line in p.stdout.splitlines():
        if "pts_time" in line:
            m = re.search(r"pts_time:([\d.]+)", line); t = float(m.group(1)) + s
        else:
            m = re.search(r"RMS_level=(-?[\d.]+|-inf)", line)
            if m and t is not None:
                pts.append((t, -120.0 if m.group(1)=="-inf" else float(m.group(1))))
    pts = [(tt,v) for tt,v in pts if lo <= tt <= hi]
    if len(pts) < 5: return []
    med = sorted(v for _,v in pts)[len(pts)//2]
    return [(tt,v) for i,(tt,v) in enumerate(pts[1:-1],1)
            if v < med-10 and v <= pts[i-1][1] and v <= pts[i+1][1]]

def pick(dips):
    if not dips: return None
    hard = [d for d in dips if d[1] < -60]
    if hard: return min(hard, key=lambda d: d[1])[0]
    deep = [d for d in dips if d[1] < -35]
    if deep: return max(deep, key=lambda d: d[1])[0]
    return None

def start_cut(mp3, sig_start, prev_end):
    gap = sig_start - prev_end
    if gap > 3.0:
        win_lo, win_hi = prev_end + 0.5, sig_start - 0.5
        if win_hi > win_lo:
            dips = sorted((t,v) for t,v in rms_dips(mp3, win_lo, win_hi))
            hard = [d for d in dips if d[1] < -60]
            if hard: return hard[0][0]
            deep = [d for d in dips if d[1] < -45]
            if deep: return deep[0][0]
        return prev_end + min(0.9, gap * 0.3)
    hard = [d for d in rms_dips(mp3, sig_start - 1.0, sig_start + 0.6) if d[1] < -60]
    if hard: return min(hard, key=lambda d: d[1])[0]
    return sig_start + 0.6

def end_cut(mp3, sig_end, limit=None):
    lo = sig_end - 0.6
    hi = min(sig_end + 12.0, max(sig_end + 0.6, (limit + 2.0) if limit else sig_end + 6.0))
    cap = (limit + 0.9) if limit else hi
    dips = [d for d in rms_dips(mp3, lo, hi) if d[0] <= cap]
    d = pick(dips)
    if d is not None: return d
    return min(hi, sig_end + 0.6)

def load_labels(p):
    if p and os.path.exists(p):
        return [(r, n) for r, n in json.load(open(p))]   # RAW regex strings
    return []

def main():
    args, sig_args, labels_p, blocks_p = [], [], None, None
    it = iter(sys.argv[1:])
    for a in it:
        if a == "--sig": sig_args.append(next(it))
        elif a == "--labels": labels_p = next(it)
        elif a == "--blocks": blocks_p = next(it)
        else: args.append(a)
    mp3, tr, outjson = args[0], args[1], args[2]
    p = subprocess.run(["ffprobe","-v","error","-show_entries","format=duration",
                        "-of","csv=p=0", mp3], capture_output=True, text=True)
    total = float(p.stdout.strip())
    segs = segments(tr)

    # ---- seeds ----
    strip_i = lambda r: re.sub(r"^\(\?i\)", "", r)
    label_rs = [strip_i(r) for r, _ in load_labels(labels_p or os.path.join("work","labels.json"))]
    extra = [strip_i(s) for s in sig_args + ([os.environ["AD_SIG"]] if os.environ.get("AD_SIG") else [])]
    pat = "|".join([GENERIC] + [f"({r})" for r in label_rs + extra])
    SIG = re.compile(pat)  # GENERIC carries the leading (?i) for the whole pattern
    stop_src = re.sub(r"\(\?i\)", "", os.environ.get("AD_SHOW_STOP", PROMO_DEFAULT) + "|" + JINGLE_DEFAULT)
    show_stop = re.compile(stop_src, re.I)
    ad_lang = os.environ.get("AD_LANG", "auto")

    sigidx = [i for i,(a,b,t) in enumerate(segs) if SIG.search(t)]
    if blocks_p:
        rngs = json.load(open(blocks_p))
        for s, e in rngs:
            pre = [i for i,(a,b,t) in enumerate(segs) if b > s - 1.5 and a < e + 1.5]
            if pre: sigidx.extend([pre[0], pre[-1]])

    blocks = []
    for i in sigidx:
        if blocks and segs[i][0] - segs[blocks[-1][1]][1] <= 25:
            blocks[-1][1] = i
        else:
            blocks.append([i, i])

    cuts, report = [], []
    for bi,(i0,i1) in enumerate(blocks):
        j = i1
        while j+1 < len(segs):
            gap = segs[j+1][0] - segs[j][1]
            t = segs[j+1][2]
            en_ok = (ad_lang == "fr")
            if gap <= 1.2 and (TAIL.search(t) or (en_ok and len(EN.findall(t)) >= 1)) \
               and not show_stop.search(t):
                j += 1
            else:
                break
        i1 = j
        t_start, t_end = segs[i0][0], segs[i1][1]
        near_eof = (total - t_end) < 20
        if bi == 0 and t_start <= 25:
            cut_s = 0.0
        else:
            prev_end = 0.0
            for k in range(i0-1, -1, -1):
                if (segs[k][1]-segs[k][0]) < 0.6 or SMEAR.search(segs[k][2]):
                    continue
                prev_end = segs[k][1]; break
            cut_s = start_cut(mp3, t_start, prev_end)
        cut_e = end_cut(mp3, t_end, segs[i1+1][0] if i1+1 < len(segs) else None)
        e_out = None if near_eof else round(cut_e, 2)
        report.append(f"BLOCK {bi}: [{t_start:.2f} - {t_end:.2f}] first={segs[i0][2][:48]!r}")
        report.append(f"          last={segs[i1][2][:48]!r}")
        report.append(f"    -> cut [{cut_s:.2f} , {e_out}] (near_eof={near_eof})")
        cuts.append([round(cut_s,2), e_out])

    report.append("\nOTHER CANDIDATES (review):")
    for i,(a,b,t) in enumerate(segs):
        if any(bl[0] <= i <= bl[1] for bl in blocks): continue
        if GENERIC_REVIEW.search(t) or (ad_lang == "fr" and len(EN.findall(t)) >= 4):
            report.append(f"    [{a:9.2f}] {t[:75]}")
    report.append("\nLONG UNTRANSCRIBED GAPS >= 6s (check for garbled ad openers):")
    for i in range(len(segs)-1):
        b, c = segs[i][1], segs[i+1][0]
        if c - b >= 6 and not any(bl[0]-2 <= i <= bl[1]+2 for bl in blocks):
            report.append(f"    gap [{b:9.2f} -> {c:9.2f}] {c-b:4.1f}s | before {segs[i][2][:40]!r} after {segs[i+1][2][:40]!r}")

    json.dump(cuts, open(outjson,"w"))
    print("\n".join(report))
    print(f"\ncuts -> {outjson}: {cuts}")

GENERIC_REVIEW = re.compile(r"(brought to you|sponsored|pr[eé]sent[eé] par|en partenariat|offert par|code promo|use code|voir conditions|essai gratuit|\.(com|fr|net|io))", re.I)

if __name__ == "__main__":
    main()