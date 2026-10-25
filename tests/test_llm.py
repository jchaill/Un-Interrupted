from __future__ import annotations

import json

from app import config
from app.engine import llm
from app.review import build_review


def _transcript(path):
    rows = [
        {"offsets": {"from": 0, "to": 5000}, "text": "Welcome to the show."},
        {"offsets": {"from": 5000, "to": 10000}, "text": "Here is the first story."},
        {"offsets": {"from": 10000, "to": 15000}, "text": "This episode is brought to you by Acme."},
        {"offsets": {"from": 15000, "to": 20000}, "text": "Use code SHOW for twenty percent off."},
        {"offsets": {"from": 20000, "to": 25000}, "text": "Back to the show."},
        {"offsets": {"from": 25000, "to": 30000}, "text": "Thanks for listening."},
    ]
    path.write_text(json.dumps({"transcription": rows}), encoding="utf-8")


def test_extract_json_accepts_fences_and_prose():
    assert llm.extract_json('```json\n{"ads": []}\n```') == {"ads": []}


def test_endpoint_and_model_names():
    assert llm.endpoint("http://127.0.0.1:11434") == "http://127.0.0.1:11434/api/chat"
    assert llm.endpoint("https://ollama.com/api") == "https://ollama.com/api/chat"
    assert llm.api_model("deepseek-v4.1-flash:cloud", "https://ollama.com") == "deepseek-v4.1-flash"
    assert llm.api_model("deepseek-v4.1-flash:cloud", "http://127.0.0.1:11434") == "deepseek-v4.1-flash:cloud"


def test_analyze_maps_segment_ids_and_merges(tmp_path, monkeypatch):
    transcript = tmp_path / "tr_ep1.json"
    _transcript(transcript)
    monkeypatch.setattr(
        llm,
        "_chat",
        lambda *args, **kwargs: (
            json.dumps(
                {
                    "ads": [
                        {
                            "start_seg": 3,
                            "end_seg": 4,
                            "kind": "host-read",
                            "brand": "Acme",
                            "confidence": 0.91,
                            "evidence": "brought to you by Acme",
                        }
                    ]
                }
            ),
            {"model": "fake", "seconds": 0.1},
        ),
    )
    cfg = {
        "llm_enabled": 1,
        "llm_model": "fake:cloud",
        "llm_base_url": "http://127.0.0.1:11434",
        "llm_min_confidence": 0.72,
        "llm_max_chars": 4000,
    }
    result = llm.analyze(
        podcast={"title": "Test", "ad_lang": "en", "labels_json": "[]"},
        transcript=transcript,
        total_duration=60.0,
        cfg=cfg,
    )
    assert result["ok"] is True
    assert len(result["ads"]) == 1
    assert result["ads"][0]["start"] == 10.0
    assert result["ads"][0]["end"] == 20.0
    assert result["ads"][0]["brand"] == "Acme"
    merged = llm.merge_cuts([[8.5, 21.0]], result["ads"], total_duration=30.0)
    assert merged == [[8.5, 21.0]]
    merged = llm.merge_cuts([], result["ads"], total_duration=30.0)
    assert merged == [[10.0, 20.0]]


def test_show_stop_and_low_confidence_are_rejected(tmp_path, monkeypatch):
    transcript = tmp_path / "tr_ep1.json"
    _transcript(transcript)
    monkeypatch.setattr(
        llm,
        "_chat",
        lambda *args, **kwargs: (
            json.dumps(
                {
                    "ads": [
                        {
                            "start_seg": 3,
                            "end_seg": 5,
                            "brand": "Acme",
                            "confidence": 0.99,
                        },
                        {
                            "start_seg": 1,
                            "end_seg": 2,
                            "brand": "Low",
                            "confidence": 0.2,
                        },
                    ]
                }
            ),
            {"model": "fake", "seconds": 0.1},
        ),
    )
    cfg = {
        "llm_enabled": 1,
        "llm_model": "fake:cloud",
        "llm_base_url": "http://127.0.0.1:11434",
        "llm_min_confidence": 0.72,
        "llm_max_chars": 4000,
    }
    result = llm.analyze(
        podcast={
            "title": "Test",
            "ad_lang": "en",
            "labels_json": "[]",
            "ad_show_stop": r"back to the show",
        },
        transcript=transcript,
        total_duration=30.0,
        cfg=cfg,
    )
    assert result["ads"] == []
    reasons = " ".join(str(item.get("reason")) for item in result["rejected"])
    assert "show re-entry" in reasons
    assert "below 0.72" in reasons


def test_ratio_guard_drops_low_confidence_ads(tmp_path, monkeypatch):
    transcript = tmp_path / "tr_ep1.json"
    _transcript(transcript)
    monkeypatch.setattr(
        llm,
        "_chat",
        lambda *args, **kwargs: (
            json.dumps(
                {
                    "ads": [
                        {"start_seg": 1, "end_seg": 2, "confidence": 0.99},
                        {"start_seg": 3, "end_seg": 4, "confidence": 0.90},
                        {"start_seg": 5, "end_seg": 6, "confidence": 0.80},
                    ]
                }
            ),
            {"model": "fake", "seconds": 0.1},
        ),
    )
    cfg = {
        "llm_enabled": 1,
        "llm_model": "fake:cloud",
        "llm_base_url": "http://127.0.0.1:11434",
        "llm_min_confidence": 0.72,
        "llm_max_chars": 4000,
    }
    result = llm.analyze(
        podcast={"title": "Test", "ad_lang": "en", "labels_json": "[]"},
        transcript=transcript,
        total_duration=30.0,
        cfg=cfg,
    )
    # Each candidate is 10s of a 30s episode, so the 25% guard must reject them all.
    assert result["ads"] == []
    assert any("25%" in str(item.get("reason")) for item in result["rejected"])


def test_review_marks_llm_only_cuts(tmp_path):
    transcript = tmp_path / "tr_ep1.json"
    _transcript(transcript)
    result = {
        "ok": True,
        "enabled": True,
        "model": "fake:cloud",
        "ads": [
            {
                "start": 10.0,
                "end": 20.0,
                "brand": "Acme",
                "kind": "host-read",
                "confidence": 0.91,
                "evidence": "brought to you by Acme",
            }
        ],
        "chunks": 1,
    }
    review = build_review(
        transcript,
        [[10.0, 20.0]],
        labels_json="[]",
        total_duration=30.0,
        llm_result=result,
    )
    assert review["llm_supported"] == 1
    assert review["llm_only"] == 1
    assert review["cuts"][0]["sources"] == ["llm"]
    assert review["cuts"][0]["llm_brand"] == "Acme"
    assert any("LLM-only cut" in warning for warning in review["warnings"])
    assert review["confidence"] <= 0.70


def test_settings_reads_env_override(monkeypatch):
    monkeypatch.setenv("UNINTERUPTED_LLM_MODEL", "from-env:cloud")
    monkeypatch.setenv("OLLAMA_API_KEY", "secret")
    opts = llm.settings({"llm_enabled": 0, "llm_model": "stored:cloud"})
    assert opts["model"] == "from-env:cloud"
    assert opts["api_key"] == "secret"
    assert opts["enabled"] is True
    assert opts["min_confidence"] == config.default_config()["llm_min_confidence"]


def test_refine_trims_small_overlap_with_a_different_known_ad(monkeypatch):
    segments = [
        {"start": float(i), "end": float(i + 1), "text": f"segment {i + 1}"}
        for i in range(12)
    ]
    ad = {
        "start": 2.0,
        "end": 9.0,
        "start_seg": 3,
        "end_seg": 9,
        "brand": "Grainger",
        "confidence": 0.95,
    }
    hints = [
        {
            "start": 0.0,
            "end": 5.0,
            "start_seg": 1,
            "end_seg": 5,
            "label": "Wayfair",
        }
    ]
    monkeypatch.setattr(
        llm,
        "_chat",
        lambda *args, **kwargs: (
            json.dumps(
                {
                    "ads": [
                        {
                            "start_seg": 3,
                            "end_seg": 9,
                            "brand": "Grainger",
                            "confidence": 0.95,
                        }
                    ]
                }
            ),
            {"model": "fake", "seconds": 0.1},
        ),
    )
    cleaned, _meta, error = llm._refine_ad(
        ad,
        segments=segments,
        known_hints=hints,
        podcast={"title": "Test"},
        opts={"timeout": 1, "min_confidence": 0.72, "model": "fake"},
        cfg={},
        show_stop=None,
        total_duration=12.0,
    )
    assert error is None
    assert len(cleaned) == 1
    assert cleaned[0]["start"] == 5.0
    assert cleaned[0]["end"] == 9.0


def test_refine_rejects_a_range_that_swallows_another_ad(monkeypatch):
    segments = [
        {"start": float(i), "end": float(i + 1), "text": f"segment {i + 1}"}
        for i in range(12)
    ]
    ad = {
        "start": 1.0,
        "end": 10.0,
        "start_seg": 2,
        "end_seg": 10,
        "brand": "Grainger",
        "confidence": 0.95,
    }
    hints = [
        {
            "start": 4.0,
            "end": 7.0,
            "start_seg": 5,
            "end_seg": 7,
            "label": "Wayfair",
        }
    ]
    monkeypatch.setattr(
        llm,
        "_chat",
        lambda *args, **kwargs: (
            json.dumps(
                {
                    "ads": [
                        {
                            "start_seg": 2,
                            "end_seg": 10,
                            "brand": "Grainger",
                            "confidence": 0.95,
                        }
                    ]
                }
            ),
            {"model": "fake", "seconds": 0.1},
        ),
    )
    cleaned, _meta, error = llm._refine_ad(
        ad,
        segments=segments,
        known_hints=hints,
        podcast={"title": "Test"},
        opts={"timeout": 1, "min_confidence": 0.72, "model": "fake"},
        cfg={},
        show_stop=None,
        total_duration=12.0,
    )
    assert cleaned == []
    assert error == "no valid range after refinement"


def test_discover_lists_models_and_keeps_current(monkeypatch):
    monkeypatch.setattr(llm.shutil, "which", lambda name: "/opt/homebrew/bin/ollama")
    monkeypatch.setattr(
        llm,
        "_get_json",
        lambda url, api_key, timeout: {
            "models": [
                {
                    "name": "deepseek-v4-pro:cloud",
                    "remote_model": "deepseek-v4-pro",
                    "size": 344,
                    "details": {"parameter_size": ""},
                    "capabilities": ["completion", "thinking"],
                }
            ]
        },
    )
    found = llm.discover(
        {
            "llm_model": "deepseek-v4.1-flash:cloud",
            "llm_base_url": "http://127.0.0.1:11434",
        },
        timeout=1,
    )
    assert found["ok"] is True
    assert found["installed"] is True
    assert found["daemon_ok"] is True
    assert found["models"][0]["name"] == "deepseek-v4.1-flash:cloud"
    assert found["models"][0]["current"] is True
    assert found["models"][0]["selected"] is True
    assert found["models"][1]["name"] == "deepseek-v4-pro:cloud"
    assert found["models"][1]["cloud"] is True


def test_discover_explains_ollama_run_when_no_models(monkeypatch):
    monkeypatch.setattr(llm.shutil, "which", lambda name: "/opt/homebrew/bin/ollama")
    monkeypatch.setattr(llm, "_get_json", lambda url, api_key, timeout: {"models": []})
    found = llm.discover(
        {
            "llm_model": "deepseek-v4.1-flash:cloud",
            "llm_base_url": "http://127.0.0.1:11434",
        },
        timeout=1,
    )
    assert found["ok"] is True
    assert found["models"] == []
    assert any("ollama run" in hint for hint in found["hints"])


def test_discover_explains_unreachable_daemon(monkeypatch):
    monkeypatch.setattr(llm.shutil, "which", lambda name: "/opt/homebrew/bin/ollama")

    def fail(url, api_key, timeout):
        raise llm.LLMError("Cannot reach Ollama")

    monkeypatch.setattr(llm, "_get_json", fail)
    found = llm.discover(
        {
            "llm_model": "deepseek-v4.1-flash:cloud",
            "llm_base_url": "http://127.0.0.1:11434",
        },
        timeout=1,
    )
    assert found["ok"] is False
    assert found["installed"] is True
    assert found["daemon_ok"] is False
    assert any("daemon is not answering" in hint for hint in found["hints"])
