#!/usr/bin/env python3
"""Analyze a whisper.cpp transcript to find ad candidates: keyword hits with
context, inter-segment gaps (jingle positions), repeated-text runs.

Language-agnostic: the keyword pack covers FR + EN sponsor/CTA phrasing. All
hits are candidates for the manual review in the skill's Step 2.
"""
import json, re, sys, glob

KEYWORDS = [
    # generic sponsor / CTA language — FR + EN
    r"coupez?", r"publicit[eé]", r"pr[eé]sent[eé]e? par", r"en partenariat",
    r"sponsor|sponsoring|sponsore[ds]",
    r"brought to (you|us) by", r"this (episode|podcast|show) (is|gets) (sponsored|presented)",
    r"offre sp[eé]ciale", r"offre de lancement", r"profitez", r"profites",
    r"r[eé]ductions?", r"remise", r"livraison offerte", r"free (trial|sample|quote|shipping)",
    r"soldes?", r"euros? de r[eé]duction", r"discount", r"% ?off",
    r"\.(fr|com|net|io|co)\b", r"www\.", r"code promo", r"promo code", r"use code", r"avec le code",
    r"inscrivez[- ]vous", r"sign up", r"rendez[- ]vous sur", r"go to", r"visit ",
    r"sur le site d[eus]", r"t[eé]l[eé]chargez", r"download the app",
    r"disponible sur", r"abonnez[- ]vous", r"premier mois", r"first month",
    r"apr[eè]s (la |cette )?(pub|pause|break)", r"after (the|this) (break|ad)",
    r"wayfair", r"big ?news", r"every style", r"come see",
]
KW = re.compile("|".join(KEYWORDS), re.I)

def load(path):
    d = json.load(open(path))
    segs = []
    for s in d["transcription"]:
        segs.append((s["offsets"]["from"] / 1000.0, s["offsets"]["to"] / 1000.0,
                     s["text"].strip()))
    return segs

def gaps(segs):
    g = []
    for a, b in zip(segs, segs[1:]):
        gap = b[0] - a[1]
        if gap >= 0.8:
            g.append((a[1], b[0], gap))
    return g

def main(path):
    segs = load(path)
    dur = segs[-1][1]
    print(f"file: {path}  segments={len(segs)}  duration={dur/60:.1f} min")
    print("\n== segments with keyword hits (context) ==")
    hits = []
    for i, (t0, t1, txt) in enumerate(segs):
        m = KW.search(txt)
        if m:
            prev = segs[i-1][2][-60:] if i else ""
            nxt = segs[i+1][2][:60] if i+1 < len(segs) else ""
            print(f"[{t0:8.1f} - {t1:8.1f}] KW={m.group(0)!r} | {txt} | prev={prev!r} | next={nxt!r}")
            hits.append((t0, t1))
    print("\n== gaps >= 2.0s (possible jingles) ==")
    for g0, g1, gd in gaps(segs):
        if gd >= 2.0:
            before = [s[2] for s in segs if s[1] <= g0 + 0.01][-1:]
            after = [s[2] for s in segs if s[0] >= g1 - 0.01][:1]
            print(f"  gap [{g0:8.1f} -> {g1:8.1f}] {gd:5.1f}s  before={before} after={after}")
    print("\n== suspicious repeated text (>=3 times) ==")
    from collections import Counter
    texts = Counter(s[2] for s in segs)
    for t, c in texts.items():
        if c >= 3 and len(t.strip()) > 8:
            print(f"  {c}x: {t[:80]!r}")
    print("\nnote: hits are candidates only — read context; show-content false")
    print("positives are normal (e.g. 'il faut couper', prize descriptions).")

if __name__ == "__main__":
    for p in sys.argv[1:] or sorted(glob.glob("work/tr_*.json")):
        main(p)