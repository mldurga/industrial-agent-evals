"""Load seed questions. Each item is an id, a category, and the question text."""

from __future__ import annotations

from pathlib import Path

import yaml

REQUIRED_KEYS = ("id", "category", "question")


def load_questions(path: Path) -> list[dict]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "questions" not in data:
        raise ValueError(f"{path} must contain a 'questions' list")
    questions = data["questions"]
    if not isinstance(questions, list) or not questions:
        raise ValueError(f"{path} has no questions")
    seen: set[str] = set()
    for item in questions:
        if not isinstance(item, dict):
            raise ValueError("each question must be a mapping")
        extra = set(item) - set(REQUIRED_KEYS)
        missing = [key for key in REQUIRED_KEYS if key not in item]
        if missing or extra:
            raise ValueError(
                f"question keys must be {REQUIRED_KEYS}, got {sorted(item)}"
            )
        qid = str(item["id"])
        if qid in seen:
            raise ValueError(f"duplicate question id {qid}")
        seen.add(qid)
        item["id"] = qid
        item["category"] = str(item["category"])
        item["question"] = str(item["question"]).strip()
    return questions
