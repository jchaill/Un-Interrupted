from __future__ import annotations

import json
import os
import re
import shutil
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .. import config
from .adapter import transcript_segments

DEFAULT_MODEL = "deepseek-v4.1-flash:cloud"
DEFAULT_BASE_URL = "http://127.0.0.1:11434"
DEFAULT_MIN_CONFIDENCE = 0.72
DEFAULT_MAX_CHARS = 24_000
DEFAULT_TIMEOUT = 180.0

JSON_SCHEMA_HINT = """{
  "ads": [
    {
      "start_seg": 123,
      "end_seg": 128,
      "kind": "host-read | produced-spot | network-promo | promo-code | free-trial | cta | other",
      "brand": "sponsor or product name",
      "confidence": 0.0,
      "evidence": "short reason, quoting the transcript"
    }
  ]
}"""

SYSTEM_PROMPT = f"""You are an advertising detector for podcast transcripts.

Find every contiguous range of transcript segments that is an advertisement or
a paid promotional read. The sponsor may be new, local, or not in any known
list. Do not rely only on sponsor names: identify the *shape* of the content.

Ad shapes include:
- host-read sponsor reads and live-read endorsements;
- produced commercials and network/podcast promos;
- promo codes, discount offers, free trials, and call-to-action URLs;
- "brought to you by", "supported by", "our friends at" segments;
- two-host ad transitions where the hosts switch into selling a product.

Do NOT mark:
- ordinary conversation, news, or opinions;
- the show's own intro/outro unless it is explicitly sponsored;
- a house promo that only asks listeners to subscribe or rate the show;
- an editorial mention of a company with no offer or call to action.

The input gives numbered transcript segments, one per line. Use only those IDs.
Return JSON only, exactly this shape:
{JSON_SCHEMA_HINT}

Rules:
- start_seg and end_seg are transcript segment IDs, inclusive.
- Return ONE object per sponsor/ad. Never merge two ads into one range.
- A range must not contain two different sponsors, and must not contain show
  conversation between two ads. If ads are separated by show content, return
  separate objects with separate IDs.
- The range must contain the whole ad and as little show content as possible.
- Never return timestamps; the caller converts segment IDs to offsets.
- confidence is a number from 0 to 1.
- Return an empty ads array if there is no advertising.
- Do not invent segment IDs or brands. If the brand is unclear, use an empty string.
"""


class LLMError(RuntimeError):
    pass


def settings(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = cfg or config.load_config()
    model = (
        os.environ.get("UNINTERUPTED_LLM_MODEL")
        or cfg.get("llm_model")
        or DEFAULT_MODEL
    ).strip()
    base_url = (
        os.environ.get("UNINTERUPTED_LLM_BASE_URL")
        or cfg.get("llm_base_url")
        or DEFAULT_BASE_URL
    ).strip()
    api_key = (
        os.environ.get("UNINTERUPTED_LLM_API_KEY")
        or os.environ.get("OLLAMA_API_KEY")
        or cfg.get("llm_api_key")
        or ""
    ).strip()
    try:
        min_confidence = float(cfg.get("llm_min_confidence", DEFAULT_MIN_CONFIDENCE))
    except (TypeError, ValueError):
        min_confidence = DEFAULT_MIN_CONFIDENCE
    try:
        max_chars = int(cfg.get("llm_max_chars", DEFAULT_MAX_CHARS))
    except (TypeError, ValueError):
        max_chars = DEFAULT_MAX_CHARS
    try:
        timeout = float(cfg.get("llm_timeout", DEFAULT_TIMEOUT))
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT
    return {
        "enabled": True,
        "model": model,
        "base_url": base_url.rstrip("/"),
        "api_key": api_key,
        "min_confidence": max(0.1, min(0.99, min_confidence)),
        "max_chars": max(4_000, min(80_000, max_chars)),
        "timeout": max(30.0, min(900.0, timeout)),
    }


def endpoint(base_url: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/api/chat"):
        return base
    if base.endswith("/api"):
        return base + "/chat"
    return base + "/api/chat"


def tags_endpoint(base_url: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/api/chat"):
        return base[: -len("/chat")] + "/tags"
    if base.endswith("/api"):
        return base + "/tags"
    return base + "/api/tags"


def api_model(model: str | None, base_url: str) -> str:
    name = (model or DEFAULT_MODEL).strip()
    # The hosted API uses "deepseek-v4.1-flash"; the local signed-in daemon
    # uses the ":cloud" suffix. Accept either form in the UI.
    if "ollama.com" in base_url and name.endswith(":cloud"):
        return name[: -len(":cloud")]
    return name


def _post_json(url: str, payload: dict[str, Any], api_key: str, timeout: float) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:800]
        raise LLMError(f"Ollama returned HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise LLMError(f"Cannot reach Ollama at {url}: {exc.reason}") from exc
    except TimeoutError as exc:
        raise LLMError(f"Ollama request timed out after {int(timeout)}s") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LLMError(f"Ollama returned invalid JSON: {raw[:500]}") from exc
    if not isinstance(data, dict):
        raise LLMError("Ollama returned an unexpected reply")
    if data.get("error"):
        raise LLMError(str(data["error"])[:800])
    return data


def _get_json(url: str, api_key: str, timeout: float) -> dict[str, Any]:
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise LLMError(f"Ollama returned HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise LLMError(f"Cannot reach Ollama at {url}: {exc.reason}") from exc
    except TimeoutError as exc:
        raise LLMError(f"Ollama request timed out after {int(timeout)}s") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LLMError(f"Ollama returned invalid JSON: {raw[:500]}") from exc
    if not isinstance(data, dict):
        raise LLMError("Ollama returned an unexpected reply")
    if data.get("error"):
        raise LLMError(str(data["error"])[:500])
    return data


def discover(cfg: dict[str, Any] | None = None, *, timeout: float = 2.0) -> dict[str, Any]:
    """Check the Ollama binary, daemon, and model list for the settings page."""
    opts = settings(cfg)
    base_url = opts["base_url"]
    current = opts["model"]
    current_name = api_model(current, base_url)
    binary = shutil.which("ollama")
    if not binary:
        for candidate in (
            "/opt/homebrew/bin/ollama",
            "/usr/local/bin/ollama",
            "/Applications/Ollama.app/Contents/Resources/ollama",
        ):
            if Path(candidate).is_file():
                binary = candidate
                break
    url = tags_endpoint(base_url)
    result: dict[str, Any] = {
        "ok": False,
        "installed": bool(binary),
        "binary": binary,
        "daemon_ok": False,
        "endpoint": url,
        "current": current,
        "models": [],
        "error": None,
        "hints": [],
        "cloud": "ollama.com" in base_url,
    }
    if "ollama.com" in base_url and not opts["api_key"]:
        result["error"] = (
            "A direct ollama.com endpoint needs an API key. Add one below, or use "
            "the signed-in local daemon at http://127.0.0.1:11434."
        )
    else:
        try:
            data = _get_json(url, opts["api_key"], timeout)
        except LLMError as exc:
            result["error"] = str(exc)
        else:
            result["daemon_ok"] = True
            result["ok"] = True
            rows = data.get("models") or []
            names: set[str] = set()
            for row in rows if isinstance(rows, list) else []:
                if not isinstance(row, dict):
                    continue
                name = str(row.get("name") or row.get("model") or "").strip()
                if not name or name in names:
                    continue
                names.add(name)
                details = row.get("details") if isinstance(row.get("details"), dict) else {}
                result["models"].append(
                    {
                        "name": name,
                        "size": row.get("size"),
                        "modified_at": row.get("modified_at"),
                        "parameter_size": details.get("parameter_size") or "",
                        "capabilities": row.get("capabilities") or [],
                        "cloud": (
                            bool(row.get("remote_model"))
                            or name.endswith(":cloud")
                            or "ollama.com" in base_url
                        ),
                    }
                )
            result["models"].sort(key=lambda item: (not item["cloud"], item["name"].lower()))
            if names and current_name not in names:
                result["models"].insert(
                    0,
                    {
                        "name": current if "ollama.com" in base_url else current,
                        "current": True,
                        "listed": False,
                        "cloud": current_name.endswith(":cloud") or "ollama.com" in base_url,
                    },
                )
            for model in result["models"]:
                model["selected"] = str(model["name"]) in {current, current_name}
                model.setdefault("listed", True)
                model.setdefault("current", False)
    if not result["installed"]:
        result["hints"].append(
            "Ollama is not installed. Install the Ollama app, or run "
            "`brew install ollama` in Terminal."
        )
    if result["installed"] and not result["daemon_ok"] and not result["cloud"]:
        result["hints"].append(
            "Ollama is installed but its daemon is not answering. Open the Ollama "
            "app, or run `ollama serve`, then check the base URL."
        )
    if result["daemon_ok"] and not result["models"]:
        result["hints"].append(
            "Ollama is running but has no models. Run `ollama run <model>` once in "
            "Terminal to make a model available (for example "
            "`ollama run llama3.2`, or `ollama run deepseek-v4.1-flash:cloud` "
            "after `ollama signin`)."
        )
    if result["daemon_ok"] and result["models"]:
        result["hints"].append(
            "Only models that have been run or pulled appear in this list. Run "
            "`ollama run <model>` once, then click Check again. Cloud models also "
            "need `ollama signin`."
        )
    return result


def _chat(
    system: str,
    user: str,
    *,
    cfg: dict[str, Any] | None = None,
    timeout: float | None = None,
    max_tokens: int | None = None,
) -> tuple[str, dict[str, Any]]:
    opts = settings(cfg)
    url = endpoint(opts["base_url"])
    if "ollama.com" in opts["base_url"] and not opts["api_key"]:
        raise LLMError(
            "A direct ollama.com endpoint needs an API key. Add one in Settings, "
            "or use the signed-in local Ollama daemon at http://127.0.0.1:11434."
        )
    payload: dict[str, Any] = {
        "model": api_model(opts["model"], opts["base_url"]),
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": False,
        "format": "json",
        "options": {"temperature": 0},
        "think": False,
    }
    if max_tokens:
        payload["options"]["num_predict"] = int(max_tokens)
    started = time.time()
    try:
        data = _post_json(url, payload, opts["api_key"], timeout or opts["timeout"])
    except LLMError as exc:
        # Some cloud models/models reject the thinking control or JSON mode.
        # Retry once with the most portable payload before giving up.
        message = str(exc).lower()
        if "http 400" not in message or not any(
            marker in message for marker in ("think", "format", "options")
        ):
            raise
        payload.pop("think", None)
        payload.pop("format", None)
        data = _post_json(url, payload, opts["api_key"], timeout or opts["timeout"])
    content = (
        (data.get("message") or {}).get("content")
        if isinstance(data.get("message"), dict)
        else None
    )
    if not isinstance(content, str) or not content.strip():
        raise LLMError("Ollama returned an empty reply")
    meta = {
        "model": data.get("model") or payload["model"],
        "endpoint": url,
        "seconds": round(time.time() - started, 2),
        "prompt_tokens": data.get("prompt_eval_count"),
        "output_tokens": data.get("eval_count"),
        "thinking": (data.get("message") or {}).get("thinking") if isinstance(data.get("message"), dict) else None,
    }
    return content, meta


def extract_json(text: str) -> Any:
    value = (text or "").strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*", "", value, flags=re.I)
        value = re.sub(r"\s*```$", "", value)
    decoder = json.JSONDecoder()
    for match in re.finditer(r"[\[{]", value):
        try:
            parsed, _end = decoder.raw_decode(value[match.start() :])
            return parsed
        except json.JSONDecodeError:
            continue
    raise LLMError(f"Model did not return valid JSON: {text[:500]}")


def _chunks(segments: list[dict[str, Any]], max_chars: int) -> list[tuple[int, int]]:
    """Return [start, end) segment ranges with a small overlap."""
    chunks: list[tuple[int, int]] = []
    total = len(segments)
    start = 0
    while start < total:
        end = start
        size = 0
        while end < total:
            line = f"[{end + 1}] {segments[end]['start']:.2f}-{segments[end]['end']:.2f} {segments[end]['text']}\n"
            if end > start and size + len(line) > max_chars:
                break
            size += len(line)
            end += 1
        chunks.append((start, end))
        if end >= total:
            break
        start = max(start + 1, end - 4)
    return chunks


def _format_segment(segment: dict[str, Any], index: int) -> str:
    return (
        f"[{index + 1}] {segment['start']:.2f}-{segment['end']:.2f} "
        f"{segment['text']}"
    )


def _label_rows(labels_json: str | None) -> list[tuple[re.Pattern[str], str]]:
    try:
        rows = json.loads(labels_json or "[]")
    except json.JSONDecodeError:
        return []
    out: list[tuple[re.Pattern[str], str]] = []
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, (list, tuple)) and len(row) == 2:
            try:
                out.append((re.compile(str(row[0]), re.I), str(row[1]).strip()))
            except re.error:
                continue
    return out


def _known_brands(labels_json: str | None) -> list[str]:
    brands: list[str] = []
    for _regex, title in _label_rows(labels_json):
        if title and title not in brands:
            brands.append(title)
    return brands[:80]


def _label_for_text(text: str, labels: list[tuple[re.Pattern[str], str]]) -> str:
    for regex, title in labels:
        if regex.search(text):
            return title
    return ""


def _known_cut_hints(
    segments: list[dict[str, Any]],
    known_cuts: list[dict[str, Any]] | None,
    labels_json: str | None,
) -> list[dict[str, Any]]:
    if not known_cuts:
        return []
    labels = _label_rows(labels_json)
    hints: list[dict[str, Any]] = []
    for cut in known_cuts:
        try:
            start = float(cut["start"])
            end = float(cut["end"]) if cut.get("end") is not None else segments[-1]["end"]
        except (KeyError, TypeError, ValueError):
            continue
        indexes = [
            i
            for i, segment in enumerate(segments)
            if segment["end"] > start and segment["start"] < end
        ]
        if not indexes:
            continue
        text = " ".join(segments[i]["text"] for i in indexes)
        hints.append(
            {
                "start_seg": indexes[0] + 1,
                "end_seg": indexes[-1] + 1,
                "label": _label_for_text(text, labels) or str(cut.get("label") or "known ad"),
            }
        )
    return hints


def _brand_tokens(value: str) -> set[str]:
    text = re.sub(r"[^\w\s]", " ", (value or "").lower())
    stop = {"the", "and", "ads", "ad", "inc", "llc", "tv", "com", "card", "network"}
    return {token for token in text.split() if len(token) > 2 and token not in stop}


def _brands_match(left: str, right: str) -> bool:
    a, b = _brand_tokens(left), _brand_tokens(right)
    if not a or not b:
        return False
    if " ".join(sorted(a)) in " ".join(sorted(b)) or " ".join(sorted(b)) in " ".join(sorted(a)):
        return True
    return len(a & b) / min(len(a), len(b)) >= 0.5


def _overlapping_hints(ad: dict[str, Any], hints: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for hint in hints:
        # Hints carry segment IDs, not timestamps; map through the caller.
        hint_start = float(hint.get("start", 0))
        hint_end = float(hint.get("end", 0))
        if _overlap(ad["start"], ad["end"], hint_start, hint_end) > 2.0:
            out.append(hint)
    return out


def _hints_with_times(
    hints: list[dict[str, Any]], segments: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for hint in hints:
        try:
            start_idx = int(hint["start_seg"]) - 1
            end_idx = int(hint["end_seg"]) - 1
        except (KeyError, TypeError, ValueError):
            continue
        if not (0 <= start_idx < len(segments) and 0 <= end_idx < len(segments)):
            continue
        out.append(
            {
                **hint,
                "start": float(segments[start_idx]["start"]),
                "end": float(segments[end_idx]["end"]),
            }
        )
    return out


def _refine_ad(
    ad: dict[str, Any],
    *,
    segments: list[dict[str, Any]],
    known_hints: list[dict[str, Any]],
    podcast: dict[str, Any],
    opts: dict[str, Any],
    cfg: dict[str, Any] | None,
    show_stop: re.Pattern[str] | None,
    total_duration: float | None,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None, str | None]:
    """Ask a second, narrow question when a proposed range is suspicious."""
    start_idx = max(0, int(ad["start_seg"]) - 1 - 6)
    end_idx = min(len(segments), int(ad["end_seg"]) + 6)
    body = "\n".join(_format_segment(segments[i], i) for i in range(start_idx, end_idx))
    relevant_hints = [
        hint
        for hint in known_hints
        if hint["end"] > float(segments[start_idx]["start"])
        and hint["start"] < float(segments[end_idx - 1]["end"])
    ]
    if relevant_hints:
        hint_text = "\n".join(
            f"- segment IDs {hint['start_seg']}-{hint['end_seg']}: {hint['label']}"
            for hint in relevant_hints
        )
        known_text = (
            "Known ads that MUST NOT be included in your ranges:\n" + hint_text
        )
    else:
        known_text = "Known ads: none in this excerpt."
    brand = ad.get("brand") or "the sponsor"
    user = (
        f"Podcast: {podcast.get('title') or 'Unknown'}\n"
        f"A previous pass proposed segment IDs {ad['start_seg']}-{ad['end_seg']} "
        f"as one ad for {brand}. That range may contain show conversation or "
        "another sponsor.\n"
        "Return only the exact ad range(s) for that sponsor. If the range contains "
        "several ads, return each as a separate object. Stop at the first segment "
        "where the show resumes.\n"
        f"{known_text}\n"
        f"Excerpt (analyze segment IDs {start_idx + 1} through {end_idx}):\n"
        f"{body}\n"
    )
    try:
        content, meta = _chat(
            SYSTEM_PROMPT, user, cfg=cfg, timeout=opts["timeout"]
        )
    except LLMError as exc:
        return [], None, str(exc)
    meta["chunk"] = f"refine-{ad['start_seg']}-{ad['end_seg']}"
    meta["segments"] = [start_idx + 1, end_idx]
    try:
        parsed = extract_json(content)
    except LLMError as exc:
        return [], meta, str(exc)
    accepted, refused = _normalize_ads(
        parsed,
        segments=segments,
        chunk_start=start_idx,
        chunk_end=end_idx,
        min_confidence=max(0.5, opts["min_confidence"] - 0.15),
        show_stop=show_stop,
        total_duration=total_duration,
        source_model=opts["model"],
    )
    cleaned: list[dict[str, Any]] = []
    for candidate in accepted:
        bad_overlap = False
        for hint in relevant_hints:
            if _brands_match(candidate.get("brand") or "", hint.get("label") or ""):
                continue
            overlap = min(candidate["end"], hint["end"]) - max(candidate["start"], hint["start"])
            if overlap <= 2.0:
                continue
            # A small overlap at one edge is usually the tail/head of an adjacent
            # known ad (for example a host-read that starts mid-segment). Trim it.
            bridges = candidate["start"] < hint["start"] and candidate["end"] > hint["end"]
            if overlap <= 5.0 and not bridges:
                if candidate["start"] < hint["end"] <= candidate["end"]:
                    candidate["start"] = round(hint["end"], 2)
                elif candidate["end"] > hint["start"] >= candidate["start"]:
                    candidate["end"] = round(hint["start"], 2)
                if candidate["end"] - candidate["start"] >= 3.0:
                    continue
            bad_overlap = True
            break
        if bad_overlap:
            refused.append({"reason": "refined range overlaps a different known ad", "ad": candidate})
            continue
        cleaned.append(candidate)
    if refused and not cleaned:
        return [], meta, "no valid range after refinement"
    return cleaned, meta, None


def _normalize_ads(
    raw: Any,
    *,
    segments: list[dict[str, Any]],
    chunk_start: int,
    chunk_end: int,
    min_confidence: float,
    show_stop: re.Pattern[str] | None,
    total_duration: float | None,
    source_model: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows = raw.get("ads") if isinstance(raw, dict) else raw
    if not isinstance(rows, list):
        return [], [{"reason": "reply has no ads array"}]
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            rejected.append({"reason": "ad is not an object", "ad": row})
            continue
        try:
            start_id = int(row["start_seg"])
            end_id = int(row["end_seg"])
        except (KeyError, TypeError, ValueError):
            rejected.append({"reason": "missing or non-integer segment IDs", "ad": row})
            continue
        if not (1 <= start_id <= len(segments) and 1 <= end_id <= len(segments)):
            rejected.append({"reason": "segment ID out of range", "ad": row})
            continue
        if start_id > end_id:
            rejected.append({"reason": "start segment is after end segment", "ad": row})
            continue
        # The model must stay inside the chunk it was given.
        if start_id - 1 < chunk_start or end_id - 1 >= chunk_end:
            rejected.append({"reason": "segment ID outside the supplied chunk", "ad": row})
            continue
        try:
            confidence = float(row.get("confidence", 0))
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = max(0.0, min(1.0, confidence))
        start = float(segments[start_id - 1]["start"])
        end = float(segments[end_id - 1]["end"])
        if total_duration is not None:
            end = min(end, float(total_duration))
        if end - start < 3.0:
            rejected.append({"reason": "shorter than 3 seconds", "ad": row})
            continue
        if confidence < min_confidence:
            rejected.append(
                {"reason": f"confidence {confidence:.2f} below {min_confidence:.2f}", "ad": row}
            )
            continue
        text = " ".join(
            str(segments[i].get("text") or "") for i in range(start_id - 1, end_id)
        ).strip()
        if show_stop and show_stop.search(text):
            rejected.append({"reason": "overlaps the configured show re-entry pattern", "ad": row})
            continue
        accepted.append(
            {
                "start": round(start, 2),
                "end": round(end, 2),
                "start_seg": start_id,
                "end_seg": end_id,
                "kind": str(row.get("kind") or "other")[:60],
                "brand": str(row.get("brand") or "")[:120],
                "confidence": round(confidence, 3),
                "evidence": str(row.get("evidence") or "")[:500],
                "text": text[:1200],
                "model": source_model,
            }
        )
    return accepted, rejected


def _overlap(a_start: float, a_end: float, b_start: float, b_end: float) -> float:
    return max(0.0, min(a_end, b_end) - max(a_start, b_start))


def _dedupe_ads(ads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered = sorted(ads, key=lambda item: (item["start"], item["end"]))
    merged: list[dict[str, Any]] = []
    for ad in ordered:
        if not merged:
            merged.append(dict(ad))
            continue
        last = merged[-1]
        overlap = _overlap(last["start"], last["end"], ad["start"], ad["end"])
        shorter = max(0.01, min(last["end"] - last["start"], ad["end"] - ad["start"]))
        adjacent = ad["start"] - last["end"] <= 0.75
        if overlap / shorter >= 0.5 or adjacent:
            last["start"] = min(last["start"], ad["start"])
            last["end"] = max(last["end"], ad["end"])
            last["confidence"] = max(last["confidence"], ad["confidence"])
            if not last.get("brand"):
                last["brand"] = ad.get("brand", "")
            if not last.get("evidence"):
                last["evidence"] = ad.get("evidence", "")
            models = list(last.get("models") or [last.get("model")])
            if ad.get("model") and ad["model"] not in models:
                models.append(ad["model"])
            last["models"] = models
        else:
            merged.append(dict(ad))
    return merged


def _apply_ratio_guard(
    ads: list[dict[str, Any]], total_duration: float | None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not total_duration or total_duration <= 0:
        return ads, []
    removed = sum(max(0.0, ad["end"] - ad["start"]) for ad in ads)
    if removed / total_duration <= 0.25:
        return ads, []
    rejected: list[dict[str, Any]] = []
    kept = sorted(ads, key=lambda item: item["confidence"], reverse=True)
    removed = 0.0
    chosen: list[dict[str, Any]] = []
    for ad in kept:
        duration = max(0.0, ad["end"] - ad["start"])
        if removed + duration <= total_duration * 0.25:
            chosen.append(ad)
            removed += duration
        else:
            rejected.append({"reason": "would remove more than 25% of the episode", "ad": ad})
    chosen.sort(key=lambda item: item["start"])
    return chosen, rejected


def analyze(
    *,
    podcast: dict[str, Any],
    transcript: Path,
    total_duration: float | None,
    cfg: dict[str, Any] | None = None,
    known_cuts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    opts = settings(cfg)
    segments = transcript_segments(transcript)
    if not segments:
        raise LLMError("Transcript has no usable segments")
    if total_duration is None:
        total_duration = float(segments[-1]["end"]) or None

    show_stop = None
    try:
        if podcast.get("ad_show_stop"):
            show_stop = re.compile(str(podcast["ad_show_stop"]), re.I)
    except re.error:
        show_stop = None
    brands = _known_brands(podcast.get("labels_json"))
    known_hints = _known_cut_hints(segments, known_cuts, podcast.get("labels_json"))
    language = podcast.get("ad_lang") or podcast.get("language") or "auto"
    chunk_ranges = _chunks(segments, opts["max_chars"])
    ads: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    calls: list[dict[str, Any]] = []
    errors: list[str] = []
    parsed_chunks = 0

    for chunk_index, (chunk_start, chunk_end) in enumerate(chunk_ranges, start=1):
        body = "\n".join(
            _format_segment(segments[i], i) for i in range(chunk_start, chunk_end)
        )
        brand_hint = ", ".join(brands) if brands else "(none configured)"
        user = (
            f"Podcast: {podcast.get('title') or 'Unknown'}\n"
            f"Ad language: {language}\n"
            f"Known sponsor hints (not exhaustive): {brand_hint}\n"
            f"Show re-entry regex (never include segments matching it): "
            f"{podcast.get('ad_show_stop') or '(none)'}\n"
            f"Transcript chunk {chunk_index} of {len(chunk_ranges)}. "
            f"Analyze segment IDs {chunk_start + 1} through {chunk_end}.\n\n"
            f"{body}\n"
        )
        try:
            content, meta = _chat(
                SYSTEM_PROMPT,
                user,
                cfg=cfg,
                timeout=opts["timeout"],
            )
        except LLMError as exc:
            errors.append(f"chunk {chunk_index}: {exc}")
            rejected.append({"reason": str(exc), "chunk": chunk_index})
            continue
        meta["chunk"] = chunk_index
        meta["segments"] = [chunk_start + 1, chunk_end]
        calls.append(meta)
        try:
            parsed = extract_json(content)
        except LLMError as exc:
            errors.append(f"chunk {chunk_index}: {exc}")
            rejected.append({"reason": str(exc), "chunk": chunk_index})
            continue
        parsed_chunks += 1
        accepted, refused = _normalize_ads(
            parsed,
            segments=segments,
            chunk_start=chunk_start,
            chunk_end=chunk_end,
            min_confidence=opts["min_confidence"],
            show_stop=show_stop,
            total_duration=total_duration,
            source_model=opts["model"],
        )
        ads.extend(accepted)
        for item in refused:
            item["chunk"] = chunk_index
            rejected.append(item)

    if parsed_chunks == 0:
        raise LLMError("; ".join(errors[:4]) or "All LLM chunks failed")

    ads = _dedupe_ads(ads)
    hints_with_times = _hints_with_times(known_hints, segments)
    refined_ads: list[dict[str, Any]] = []
    for ad in ads:
        overlapping = _overlapping_hints(ad, hints_with_times)
        matching = [
            hint
            for hint in overlapping
            if _brands_match(ad.get("brand") or "", hint.get("label") or "")
        ]
        different = [hint for hint in overlapping if hint not in matching]
        if matching:
            # The regex detector already has a precise cut for this sponsor.
            # Prefer it and drop the wider LLM suggestion.
            continue
        suspicious = bool(different) or (ad["end"] - ad["start"] > 120.0)
        if not suspicious:
            refined_ads.append(ad)
            continue
        cleaned, meta, error = _refine_ad(
            ad,
            segments=segments,
            known_hints=hints_with_times,
            podcast=podcast,
            opts=opts,
            cfg=cfg,
            show_stop=show_stop,
            total_duration=total_duration,
        )
        if meta:
            calls.append(meta)
        if error:
            errors.append(f"refine {ad['start_seg']}-{ad['end_seg']}: {error}")
            rejected.append(
                {"reason": f"suspicious range dropped: {error}", "ad": ad}
            )
            continue
        refined_ads.extend(cleaned)
    ads = _dedupe_ads(refined_ads)
    ads, ratio_rejected = _apply_ratio_guard(ads, total_duration)
    rejected.extend(ratio_rejected)
    return {
        "ok": True,
        "partial_error": "; ".join(errors[:4]) if errors else None,
        "enabled": True,
        "model": opts["model"],
        "endpoint": endpoint(opts["base_url"]),
        "ads": ads,
        "rejected": rejected[:100],
        "chunks": len(chunk_ranges),
        "calls": calls,
        "known_hints": known_hints,
        "removed_seconds": round(sum(max(0.0, a["end"] - a["start"]) for a in ads), 2),
        "generated_at": config.now_iso(),
    }


def failed_result(message: str, cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    opts = settings(cfg)
    return {
        "ok": False,
        "enabled": True,
        "model": opts["model"],
        "endpoint": endpoint(opts["base_url"]),
        "ads": [],
        "rejected": [],
        "chunks": 0,
        "calls": [],
        "error": message[:1000],
        "generated_at": config.now_iso(),
    }


def merge_cuts(
    regex_cuts: list[list[float | None]],
    llm_ads: list[dict[str, Any]],
    *,
    total_duration: float | None,
) -> list[list[float | None]]:
    """Union regex cuts with LLM candidates, preferring the wider range."""
    merged: list[list[float | None]] = []
    for row in regex_cuts:
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue
        try:
            start = max(0.0, float(row[0]))
            end = float(row[1]) if row[1] is not None else None
        except (TypeError, ValueError):
            continue
        if end is not None and end <= start:
            continue
        merged.append([start, end])
    for ad in llm_ads:
        start = float(ad["start"])
        end = float(ad["end"])
        matched = False
        for cut in merged:
            cut_end = cut[1] if cut[1] is not None else (total_duration or end)
            overlap = _overlap(cut[0], float(cut_end), start, end)
            shorter = max(0.01, min(float(cut_end) - cut[0], end - start))
            if overlap / shorter >= 0.25 or abs(start - float(cut_end)) <= 2:
                cut[0] = min(float(cut[0]), start)
                cut[1] = max(float(cut_end), end)
                matched = True
        if not matched:
            merged.append([start, end])
    merged.sort(key=lambda row: row[0])
    combined: list[list[float | None]] = []
    for start, end in merged:
        if total_duration is not None:
            start = min(start, total_duration)
            end = min(end, total_duration) if end is not None else None
        if end is not None and end <= start:
            continue
        if combined:
            previous = combined[-1]
            previous_end = previous[1] if previous[1] is not None else end
            if end is not None and previous_end is not None and start - previous_end <= 0.75:
                previous[1] = max(float(previous_end), end)
                continue
        combined.append([round(start, 2), round(end, 2) if end is not None else None])
    return combined


def test_connection(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    content, meta = _chat(
        "You are a health check. Reply with JSON only.",
        'Return exactly {"ok": true} and nothing else.',
        cfg=cfg,
        max_tokens=40,
    )
    parsed = extract_json(content)
    if not isinstance(parsed, dict) or parsed.get("ok") is not True:
        raise LLMError(f"Unexpected health-check reply: {content[:300]}")
    return meta
