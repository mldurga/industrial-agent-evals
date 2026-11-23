import json
from pathlib import Path

import pytest
import yaml

from plant_evals.agent import run_question
from plant_evals.collect import collect
from plant_evals.fake_model import M3H_TO_LPS, parse_windows
from plant_evals.questions import load_questions
from plant_evals.review import format_trace, record_annotation
from plant_evals.store import find_root

CATEGORIES = {
    "tag_lookup",
    "trend_root_cause",
    "alarm_triage",
    "unit_conversion",
    "ambiguous_unanswerable",
    "multi_step",
}
REFUSALS = {"q22", "q23", "q24", "q25"}


def test_seed_file_has_ids_and_no_expected_answers():
    path = find_root() / "questions" / "seed.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    questions = load_questions(path)
    assert len(questions) == 30
    assert {item["category"] for item in questions} == CATEGORIES
    counts: dict[str, int] = {}
    for item in questions:
        counts[item["category"]] = counts.get(item["category"], 0) + 1
        assert set(raw["questions"][questions.index(item)]) == {"id", "category", "question"}
    assert all(count >= 4 for count in counts.values())
    for item in questions:
        if item["id"] in REFUSALS or item["category"] == "tag_lookup":
            continue
        assert parse_windows(item["question"]), item["id"]


def test_fake_traces_cover_the_seed_set(store):
    questions = load_questions(find_root() / "questions" / "seed.yaml")
    by_id = {}
    for item in questions:
        trace = run_question(
            item["question"],
            store=store,
            fake=True,
            question_id=item["id"],
            category=item["category"],
        )
        by_id[item["id"]] = trace
        assert trace["error"] is None
        assert trace["model"] == "fake"
        assert trace["duration_ms"] >= 0
        assert trace["messages"][0]["role"] == "system"
        assert trace["messages"][1]["role"] == "user"
        assert trace["final_answer"]
        assert "OPENAI_API_KEY" not in json.dumps(trace)

    assert "CW-P101-FT-101" in by_id["q01"]["final_answer"]
    assert "m3/h" in by_id["q01"]["final_answer"]
    assert by_id["q01"]["tool_calls"][0]["name"] == "search_tags"

    for qid in REFUSALS:
        assert by_id[qid]["tool_calls"] == []
        assert "don't know" in by_id[qid]["final_answer"].lower()

    assert by_id["q26"]["tool_calls"]
    assert "don't know" in by_id["q26"]["final_answer"].lower()

    flow = next(call for call in by_id["q17"]["tool_calls"] if call["name"] == "tag_stats")
    mean = flow["result"]["mean"]
    assert f"{mean / M3H_TO_LPS:.3f}" in by_id["q17"]["final_answer"]
    assert "3.6" in by_id["q17"]["final_answer"]

    assert [call["name"] for call in by_id["q14"]["tool_calls"]] == ["query_alarms"]
    assert "chattering" in by_id["q14"]["final_answer"].lower()
    assert "peaked" in by_id["q08"]["final_answer"].lower()
    assert "flood" in by_id["q12"]["final_answer"].lower()
    assert by_id["q12"]["tool_calls"][0]["arguments"]["onset_only"] is True
    assert "onset_only" not in by_id["q16"]["tool_calls"][0]["arguments"]
    assert "urgent" in by_id["q16"]["final_answer"].lower()
    assert "separate" in by_id["q28"]["final_answer"].lower()
    assert "minutes apart" in by_id["q30"]["final_answer"].lower()
    assert "not both 1" in by_id["q30"]["final_answer"]


def test_collect_writes_one_file(store, tmp_path: Path):
    questions = [
        {
            "id": "q22",
            "category": "ambiguous_unanswerable",
            "question": "Is the plant okay?",
        }
    ]
    failures = collect(questions, tmp_path, store, fake=True)
    assert failures == 0
    written = json.loads((tmp_path / "q22.json").read_text(encoding="utf-8"))
    assert written["question_id"] == "q22"
    assert "don't know" in written["final_answer"].lower()


def test_review_records_first_failure(tmp_path: Path):
    trace_path = tmp_path / "q01.json"
    trace = {
        "question_id": "q01",
        "category": "tag_lookup",
        "question": "What is the tag?",
        "model": "fake",
        "started_at": "2026-03-01T00:00:00Z",
        "duration_ms": 1.5,
        "messages": [
            {"role": "system", "content": "SECRET-PROMPT"},
            {"role": "user", "content": "What is the tag?"},
            {"role": "assistant", "content": "CW-P101-FT-101", "tool_calls": []},
        ],
        "tool_calls": [],
        "final_answer": "CW-P101-FT-101",
        "error": None,
    }
    trace_path.write_text(json.dumps(trace), encoding="utf-8")
    rendered = format_trace(trace)
    assert "What is the tag?" in rendered
    assert "CW-P101-FT-101" in rendered
    assert "SECRET-PROMPT" not in rendered

    out = tmp_path / "open_codes.csv"
    record_annotation(
        out,
        question_id="q01",
        category="tag_lookup",
        verdict="fail",
        note="Invented a unit.",
        model="fake",
        trace_file=str(trace_path),
        reviewed_at="2026-03-05T00:00:00Z",
    )
    text = out.read_text(encoding="utf-8")
    assert "Invented a unit." in text
    record_annotation(
        out,
        question_id="q01",
        category="tag_lookup",
        verdict="pass",
        note="this note is dropped on a pass",
        model="fake",
        trace_file=str(trace_path),
        reviewed_at="2026-03-05T01:00:00Z",
    )
    rows = out.read_text(encoding="utf-8").strip().splitlines()
    assert len(rows) == 2
    assert ",pass," in rows[1]
    assert "this note" not in rows[1]

    with pytest.raises(ValueError):
        record_annotation(
            out,
            question_id="q02",
            category="tag_lookup",
            verdict="fail",
            note="  ",
            model="fake",
            trace_file=str(trace_path),
        )
