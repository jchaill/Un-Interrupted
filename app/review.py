from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Any

from .engine.adapter import transcript_segments


def _compile_labels(labels_json: str | None) -> list[tuple[str, re.Pattern[str], str]]:
    out: list[tuple[str, re.Pattern[str], str]] = []
    try:
        rows = json.loads(labels_json or "[]")
    except json.JSONDecodeError:
        rows = []
    if not isinstance(rows, list):
        return out
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) != 2:
            continue
        source, title = str(row[0]), str(row[1])
        try:
            out.append((source, re.compile(source, re.I), title))
        except re.error:
            continue
    return out


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", (text or "").lower())).strip()


def _tokens(text: str) -> set[str]:
    return {t for t in _norm(text).split() if len(t) > 2}


def _similarity(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    jaccard = len(ta & tb) / len(ta | tb)
    ratio = SequenceMatcher(None, _norm(a), _norm(b)).ratio()
    return max(jaccard, ratio)


def _cut_text(segments: list[dict[str, Any]], start: float, end: float | None) -> str:
    end = end if end is not None else float("inf")
    parts = []
    for seg in segments:
        mid = (seg["start"] + seg["end"]) / 2
        if start - 0.25 <= mid <= end + 0.25:
            parts.append(seg["text"])
    return " ".join(parts)


def _excerpt(
    segments: list[dict[str, Any]], start: float, end: float | None, context: int = 4
) -> list[dict[str, Any]]:
    end_value = end if end is not None else float("inf")
    indexes = [
        i
        for i, seg in enumerate(segments)
        if (seg["end"] >= start - 4 and seg["start"] <= end_value + 4)
    ]
    if not indexes:
        # Find the nearest segment.
        nearest = min(
            range(len(segments)),
            key=lambda i: abs((segments[i]["start"] + segments[i]["end"]) / 2 - start),
            default=None,
        )
        indexes = [nearest] if nearest is not None else []
    if not indexes:
        return []
    lo = max(0, indexes[0] - context)
    hi = min(len(segments), indexes[-1] + context + 1)
    out = []
    for seg in segments[lo:hi]:
        mid = (seg["start"] + seg["end"]) / 2
        out.append(
            {
                "start": round(seg["start"], 2),
                "end": round(seg["end"], 2),
                "text": seg["text"],
                "inside": start - 0.25 <= mid <= end_value + 0.25,
            }
        )
    return out


def build_review(
    transcript_path,
    cuts: list[list[float | None]],
    *,
    labels_json: str | None,
    total_duration: float | None,
) -> dict[str, Any]:
    segments = transcript_segments(transcript_path)
    labels = _compile_labels(labels_json)
    normalized_cuts: list[dict[str, Any]] = []
    for raw in cuts:
        try:
            start = max(0.0, float(raw[0]))
            end = float(raw[1]) if raw[1] is not None else None
        except (TypeError, ValueError, IndexError):
            continue
        if end is not None and end <= start:
            continue
        if total_duration is not None:
            start = min(start, total_duration)
            end = min(end, total_duration) if end is not None else None
        if end is not None and end <= start:
            continue
        normalized_cuts.append({"start": start, "end": end})
    normalized_cuts.sort(key=lambda c: c["start"])

    # Repeat detection: identical sponsor reads appear several times per episode.
    texts = [_cut_text(segments, c["start"], c["end"]) for c in normalized_cuts]
    repeat_groups: list[int | None] = [None] * len(normalized_cuts)
    group_id = 0
    for i in range(len(texts)):
        if repeat_groups[i] is not None or len(texts[i].split()) < 8:
            continue
        members = [
            j
            for j in range(i, len(texts))
            if repeat_groups[j] is None and _similarity(texts[i], texts[j]) >= 0.55
        ]
        if len(members) >= 2:
            for j in members:
                repeat_groups[j] = group_id
            group_id += 1

    rows = []
    labels_hit = 0
    warnings: list[str] = []
    removed = 0.0
    for index, cut in enumerate(normalized_cuts):
        start, end = cut["start"], cut["end"]
        text = texts[index]
        label_title = None
        for _source, regex, title in labels:
            if regex.search(text):
                label_title = title
                labels_hit += 1
                break
        duration = (end - start) if end is not None else (
            (total_duration - start) if total_duration else None
        )
        if duration:
            removed += max(0.0, duration)
        cut_warnings = []
        if duration is not None and duration < 3:
            cut_warnings.append("Very short cut; check it is not clipping show audio.")
        if end is None:
            cut_warnings.append("Cut extends to the end of the episode.")
        if total_duration and start < 2.0 and index == 0:
            cut_warnings.append("Cut starts at the very beginning; confirm the cold open is not show content.")
        if not label_title and repeat_groups[index] is None:
            cut_warnings.append("No sponsor label matched and this block is not a repeat; verify manually.")
        rows.append(
            {
                "index": index + 1,
                "start": round(start, 2),
                "end": round(end, 2) if end is not None else None,
                "duration": round(duration, 1) if duration is not None else None,
                "label": label_title,
                "repeat_group": repeat_groups[index],
                "text": text[:1200],
                "excerpt": _excerpt(segments, start, end),
                "warnings": cut_warnings,
            }
        )

    ratio = (removed / total_duration) if total_duration and total_duration > 0 else 0.0
    repeat_count = sum(1 for g in repeat_groups if g is not None)
    if not normalized_cuts:
        warnings.append(
            "No ad blocks were detected. Listen to the episode or inspect the transcript; "
            "you can add cut ranges manually or skip it."
        )
    if ratio > 0.25:
        warnings.append(f"Removing {ratio * 100:.1f}% of the episode is unusually large; check the ranges.")
    if normalized_cuts and total_duration and ratio < 0.01:
        warnings.append(f"Removing only {ratio * 100:.2f}% is suspiciously small; a break may have been missed.")
    if normalized_cuts and labels_hit == 0:
        warnings.append("No calibrated sponsor label matched any block; only generic/repeat signals were used.")
    generic_only = all(
        not row["label"] and row["repeat_group"] is None for row in rows
    )
    if rows and generic_only:
        warnings.append("Every block is unlabelled and non-repeating; review carefully before publishing.")

    # Long untranscribed gaps can hide garbled ad openers (jingles/music).
    gaps = []
    for a, b in zip(segments, segments[1:]):
        gap = b["start"] - a["end"]
        if gap >= 6 and not any(
            c["start"] - 2 <= a["end"] <= (c["end"] or float("inf")) + 2
            for c in normalized_cuts
        ):
            gaps.append((a["end"], b["start"], gap))
    for start, end, gap in gaps[:5]:
        warnings.append(
            f"Untranscribed gap {start:.0f}–{end:.0f}s ({gap:.1f}s) may hide a garbled ad opener."
        )
    if len(gaps) > 5:
        warnings.append(f"{len(gaps) - 5} more untranscribed gaps ≥6s; inspect the transcript.")

    confidence = 0.25
    if normalized_cuts:
        confidence += 0.30 * (labels_hit / len(normalized_cuts))
        if repeat_count:
            confidence += 0.25
        if 0.01 <= ratio <= 0.25:
            confidence += 0.10
        if len(normalized_cuts) >= 2:
            confidence += 0.08
    else:
        confidence = 0.0
    if normalized_cuts and labels_hit == 0 and repeat_count == 0:
        confidence = min(confidence, 0.35)
    if ratio > 0.25 or (normalized_cuts and ratio < 0.01):
        confidence = min(confidence, 0.50)
    if any("No sponsor label" in w for w in warnings):
        confidence = min(confidence, 0.60)
    confidence = round(min(confidence, 0.95), 2)

    return {
        "cuts": rows,
        "warnings": warnings,
        "confidence": confidence,
        "removed_seconds": round(removed, 2),
        "ratio": round(ratio, 4),
        "n_cuts": len(normalized_cuts),
        "labels_hit": labels_hit,
        "repeated_blocks": repeat_count,
        "gap_warnings": len(gaps),
        "total_duration": round(total_duration, 2) if total_duration else None,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
