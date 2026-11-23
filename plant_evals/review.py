"""Open coding: show one trace and record pass or fail plus the first failure.

The note is about the first failure in the trace. Later mistakes are often
the consequence of that one. Pass rows store an empty note.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from plant_evals.store import find_root, format_time

COLUMNS = [
    "question_id",
    "category",
    "verdict",
    "first_failure_note",
    "model",
    "trace_file",
    "reviewed_at",
]

_DISPLAY_LINES = 45


def format_trace(trace: dict, *, full: bool = False) -> str:
    lines = [
        f"Trace {trace.get('question_id') or '(no id)'}  category={trace.get('category') or '-'}",
        f"Model: {trace.get('model')}    duration_ms: {trace.get('duration_ms')}    "
        f"started_at: {trace.get('started_at')}",
        "",
        "Question",
        str(trace.get("question", "")).strip(),
        "",
    ]
    if trace.get("error"):
        lines.extend(["Error", str(trace["error"]), ""])
    for message in trace.get("messages") or []:
        role = message.get("role")
        if role == "system" and not full:
            lines.append("--- system (hidden; pass --full to show) ---")
            lines.append("")
            continue
        lines.append(f"--- {role} ---")
        if message.get("tool_calls"):
            for call in message["tool_calls"]:
                lines.append(f"tool call {call.get('name')} ({call.get('id')})")
                lines.extend(_dump(call.get("arguments")))
        content = message.get("content")
        if content not in (None, "", []):
            if role == "tool":
                lines.append(f"result {message.get('name')}")
                lines.extend(_dump(content))
            else:
                lines.append(str(content).strip())
        lines.append("")
    lines.extend(["Final answer", str(trace.get("final_answer", "")).strip(), ""])
    return "\n".join(lines)


def record_annotation(
    path: Path,
    *,
    question_id: str,
    category: str,
    verdict: str,
    note: str,
    model: str,
    trace_file: str,
    reviewed_at: str | None = None,
) -> None:
    if verdict not in {"pass", "fail"}:
        raise ValueError("verdict must be pass or fail")
    note = note.strip()
    if verdict == "fail" and not note:
        raise ValueError("a fail needs a note about the first failure")
    if verdict == "pass":
        note = ""
    rows = _read_rows(path)
    new_row = {
        "question_id": question_id,
        "category": category,
        "verdict": verdict,
        "first_failure_note": note,
        "model": model,
        "trace_file": trace_file,
        "reviewed_at": reviewed_at or format_time(datetime.now(timezone.utc)),
    }
    replaced = False
    for index, row in enumerate(rows):
        if row.get("question_id") == question_id:
            rows[index] = new_row
            replaced = True
            break
    if not replaced:
        rows.append(new_row)
    _write_rows(path, rows)


def pending_traces(trace_dir: Path, annotation_path: Path) -> list[Path]:
    done = {row["question_id"] for row in _read_rows(annotation_path) if row.get("question_id")}
    paths = sorted(trace_dir.glob("*.json"))
    return [path for path in paths if path.stem not in done]


def main() -> None:
    parser = argparse.ArgumentParser(description="Review traces and record open codes.")
    parser.add_argument("--traces", type=Path, default=None, help="Directory of trace JSON files.")
    parser.add_argument("--out", type=Path, default=None, help="annotations/open_codes.csv")
    parser.add_argument("--trace", type=Path, default=None, help="One trace file to print or annotate.")
    parser.add_argument("--verdict", choices=["pass", "fail"], default=None)
    parser.add_argument("--note", default="")
    parser.add_argument("--list", action="store_true", help="Show which traces are annotated.")
    parser.add_argument("--full", action="store_true", help="Include the system prompt.")
    args = parser.parse_args()

    root = find_root()
    trace_dir = args.traces or (root / "traces")
    out_path = args.out or (root / "annotations" / "open_codes.csv")

    if args.list:
        _print_list(trace_dir, out_path)
        return

    if args.verdict:
        if args.trace is None:
            raise SystemExit("Pass --trace with --verdict.")
        trace = _load(args.trace)
        try:
            record_annotation(
                out_path,
                question_id=str(trace.get("question_id") or args.trace.stem),
                category=str(trace.get("category") or ""),
                verdict=args.verdict,
                note=args.note,
                model=str(trace.get("model") or ""),
                trace_file=str(args.trace),
            )
        except ValueError as exc:
            raise SystemExit(str(exc)) from None
        print(f"Recorded {args.verdict} for {trace.get('question_id') or args.trace.stem} in {out_path}")
        return

    if args.trace:
        print(format_trace(_load(args.trace), full=args.full))
        return

    paths = pending_traces(trace_dir, out_path)
    if not paths:
        print(f"No unannotated traces in {trace_dir}.")
        return
    if not sys.stdin.isatty():
        raise SystemExit("stdin is not a terminal. Pass --trace and --verdict to record a row.")
    for index, path in enumerate(paths, start=1):
        trace = _load(path)
        print(f"\n[{index}/{len(paths)}] {path}\n")
        print(format_trace(trace, full=args.full))
        choice = _ask("Verdict [p]ass, [f]ail, [s]kip, [q]uit: ").strip().lower()
        if choice in {"q", "quit"}:
            return
        if choice in {"s", "skip", ""}:
            continue
        if choice in {"p", "pass"}:
            verdict, note = "pass", ""
        elif choice in {"f", "fail"}:
            verdict = "fail"
            note = _ask("First failure (what went wrong first): ").strip()
            if not note:
                print("A fail needs a note. Skipping this trace.")
                continue
        else:
            print("Unknown choice, skipping.")
            continue
        record_annotation(
            out_path,
            question_id=str(trace.get("question_id") or path.stem),
            category=str(trace.get("category") or ""),
            verdict=verdict,
            note=note,
            model=str(trace.get("model") or ""),
            trace_file=str(path),
        )
        print(f"Recorded {verdict}.")


def _ask(prompt: str) -> str:
    try:
        return input(prompt)
    except EOFError:
        return "q"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _dump(value: object) -> list[str]:
    text = json.dumps(value, indent=2)
    rows = text.splitlines()
    if len(rows) <= _DISPLAY_LINES:
        return rows
    hidden = len(rows) - _DISPLAY_LINES
    return rows[:_DISPLAY_LINES] + [f"... ({hidden} more lines in the trace file)"]


def _read_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _write_rows(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column, "") for column in COLUMNS})


def _print_list(trace_dir: Path, annotation_path: Path) -> None:
    annotations = {row["question_id"]: row for row in _read_rows(annotation_path)}
    paths = sorted(trace_dir.glob("*.json"))
    if not paths:
        print(f"No traces in {trace_dir}.")
        return
    for path in paths:
        row = annotations.get(path.stem)
        if row:
            print(f"{path.stem}  {row.get('verdict')}  {row.get('first_failure_note', '')}")
        else:
            print(f"{path.stem}  pending")


if __name__ == "__main__":
    main()
