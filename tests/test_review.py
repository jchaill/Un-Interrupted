import json

from app.review import build_review


def _transcript(segments):
    return {
        "transcription": [
            {"offsets": {"from": int(start * 1000), "to": int(end * 1000)}, "text": text}
            for start, end, text in segments
        ]
    }


def test_review_finds_label_and_repeats(tmp_path):
    path = tmp_path / "tr.json"
    path.write_text(
        json.dumps(
            _transcript(
                [
                    (0, 2, "Welcome to the show."),
                    (2, 6, "This episode is brought to you by Acme."),
                    (6, 9, "Acme makes great things. Use code SHOW."),
                    (9, 12, "Back to the show."),
                    (12, 16, "This episode is brought to you by Acme."),
                    (16, 19, "Acme makes great things. Use code SHOW."),
                    (19, 24, "Back to the show."),
                ]
            )
        )
    )
    review = build_review(
        path,
        [[2, 9], [12, 19]],
        labels_json='[["(?i)acme","Acme Corp"]]',
        total_duration=24.0,
    )
    assert review["n_cuts"] == 2
    assert review["labels_hit"] == 2
    assert review["repeated_blocks"] == 2
    assert review["confidence"] <= 0.5
    assert review["ratio"] > 0.5
    assert any("unusually large" in w for w in review["warnings"])
    assert review["cuts"][0]["label"] == "Acme Corp"
    assert all(any(line["inside"] for line in cut["excerpt"]) for cut in review["cuts"])


def test_review_without_cuts_is_never_confident(tmp_path):
    path = tmp_path / "tr.json"
    path.write_text(json.dumps(_transcript([(0, 5, "Hello.")])))
    review = build_review(path, [], labels_json="[]", total_duration=5.0)
    assert review["confidence"] == 0.0
    assert review["n_cuts"] == 0
    assert review["no_ads"] is True
    assert review["warnings"]


def test_review_flags_unlabelled_cut(tmp_path):
    path = tmp_path / "tr.json"
    path.write_text(
        json.dumps(
            _transcript(
                [
                    (0, 5, "Hello and welcome."),
                    (5, 12, "This is a long block of words that does not mention a sponsor at all."),
                    (12, 20, "And now the show continues."),
                ]
            )
        )
    )
    review = build_review(path, [[5, 12]], labels_json="[]", total_duration=20.0)
    assert review["labels_hit"] == 0
    assert review["confidence"] <= 0.35
    assert any("No calibrated sponsor label" in w for w in review["warnings"])
    assert any("verify manually" in w for w in review["cuts"][0]["warnings"])
