"""Run every seed question and write one JSON trace per question."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from plant_evals.agent import run_question
from plant_evals.questions import load_questions
from plant_evals.store import PlantStore, find_root


def collect(
    questions: list[dict],
    out_dir: Path,
    store: PlantStore,
    *,
    fake: bool,
) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    failures = 0
    for item in questions:
        trace = run_question(
            item["question"],
            store=store,
            fake=fake,
            question_id=item["id"],
            category=item["category"],
        )
        path = out_dir / f"{item['id']}.json"
        path.write_text(json.dumps(trace, indent=2) + "\n", encoding="utf-8")
        if trace["error"]:
            failures += 1
            status = "error"
        else:
            status = "ok"
        print(
            f"{item['id']} {item['category']} tools={len(trace['tool_calls'])} "
            f"{trace['duration_ms']:.0f}ms {status} -> {path}"
        )
    return failures


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect one trace per seed question.")
    parser.add_argument("--fake", action="store_true", help="Use the offline stub model.")
    parser.add_argument("--questions", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--data", type=Path, default=None)
    parser.add_argument("--ids", default="", help="Comma-separated question ids to run.")
    args = parser.parse_args()
    root = find_root()
    question_path = args.questions or (root / "questions" / "seed.yaml")
    questions = load_questions(question_path)
    if args.ids.strip():
        wanted = {piece.strip() for piece in args.ids.split(",") if piece.strip()}
        questions = [item for item in questions if item["id"] in wanted]
        missing = wanted - {item["id"] for item in questions}
        if missing:
            raise SystemExit(f"Unknown question ids: {', '.join(sorted(missing))}")
    store = PlantStore.load(args.data or (root / "data"))
    failures = collect(questions, args.out or (root / "traces"), store, fake=args.fake)
    if failures:
        raise SystemExit(f"{failures} trace(s) recorded an error.")


if __name__ == "__main__":
    main()
