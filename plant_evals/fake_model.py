"""Deterministic stand-in for the chat API.

The stub plans a few tool calls from the wording of the question, then writes
an answer from the tool results. It is for tests and offline runs. It is not
a substitute for reviewing traces from a real model.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from plant_evals.store import PlantStore, format_time, parse_time

M3H_TO_LPS = 3.6
KPA_TO_BAR = 100.0
KPA_PER_PSI = 6.894757

_TAG = re.compile(r"CW-[A-Z0-9]+(?:-[A-Z0-9]+)+")
_BETWEEN = re.compile(
    r"between\s+(\d{1,2}:\d{2})\s+and\s+(\d{1,2}:\d{2})\s+UTC\s+on\s+(\d{1,2})\s+(March|Mar)\s+(\d{4})",
    re.IGNORECASE,
)
_THROUGH = re.compile(
    r"from\s+(\d{1,2})\s+(March|Mar)\s+(\d{4})\s+(\d{1,2}:\d{2})\s+UTC\s+"
    r"through\s+(\d{1,2})\s+(March|Mar)\s+(\d{4})\s+(\d{1,2}:\d{2})\s+UTC",
    re.IGNORECASE,
)
_ON_DAY = re.compile(r"on\s+(\d{1,2})\s+(March|Mar)\s+(\d{4})", re.IGNORECASE)
_THRESHOLD = (
    ("falling below", "low"),
    ("below", "low"),
    ("reaching", "high"),
    ("exceeding", "high"),
    ("exceed", "high"),
    ("above", "high"),
)


def parse_windows(question: str) -> list[tuple[str, str]]:
    windows: list[tuple[str, str]] = []
    for match in _BETWEEN.finditer(question):
        day = _stamp(int(match.group(5)), match.group(4), int(match.group(3)), match.group(1))
        end = _stamp(int(match.group(5)), match.group(4), int(match.group(3)), match.group(2))
        if end > day:
            windows.append((format_time(day), format_time(end)))
    for match in _THROUGH.finditer(question):
        start = _stamp(int(match.group(3)), match.group(2), int(match.group(1)), match.group(4))
        end = _stamp(int(match.group(7)), match.group(6), int(match.group(5)), match.group(8))
        if end > start:
            windows.append((format_time(start), format_time(end)))
    if windows:
        return windows
    match = _ON_DAY.search(question)
    if match:
        start = datetime(
            int(match.group(3)),
            _month(match.group(2)),
            int(match.group(1)),
            tzinfo=timezone.utc,
        )
        return [(format_time(start), format_time(start + timedelta(days=1)))]
    return []


def refusal_text(question: str) -> str | None:
    if _hedge(question):
        return None
    text = question.lower().strip()
    if text.rstrip("?").strip() == "is the plant okay":
        return (
            "I don't know. The question does not name a unit, a tag, or a time window, "
            "and the plant data does not define 'okay'."
        )
    if "last week" in text or ("setpoint" in text and "why" in text):
        return "I don't know. The historian has no record of setpoint changes from last week."
    if "outside operator" in text or "procedure" in text:
        return (
            "I don't know. The event notes do not record shift roles or whether a procedure was followed."
        )
    if "tomorrow" in text or text.startswith("what will"):
        return (
            "I don't know. There is no forecast in the plant data. "
            "History runs from 2026-03-01T00:00:00Z through 2026-03-04T00:00:00Z."
        )
    if "vendor" in text or "datasheet" in text:
        return "I don't know. Vendor datasheets are not in the plant data."
    return None


def plan_calls(question: str, store: PlantStore) -> list[dict]:
    if refusal_text(question):
        return []
    tags = _tags_in(question, store)
    windows = parse_windows(question)
    if _is_lookup(question) and not tags:
        return [{"name": "search_tags", "arguments": {"query": _lookup_query(question), "limit": 20}}]
    if not tags:
        if windows and (_wants_alarms(question) or not _wants_history(question)):
            return [_alarm_call(question, windows[0], None)]
        return [{"name": "search_tags", "arguments": {"query": _lookup_query(question), "limit": 20}}]

    if len(windows) == len(tags) and len(windows) > 1:
        pairs = list(zip(tags, windows))
    elif windows:
        pairs = [(tag, windows[0]) for tag in tags]
    else:
        pairs = []
    calls: list[dict] = []
    alarm_for_tag = False
    for tag, window in pairs:
        before = _before_tag(question, tag).lower()
        alarm_focus = _wants_alarms(question) and not _wants_history(question) and not _wants_stats(question)
        if alarm_focus or any(word in before for word in ("alarm", "chatter", "unacknowledged")):
            calls.append(_alarm_call(question, window, tag))
            alarm_for_tag = True
        elif _wants_stats(question) and not _wants_history(question):
            calls.append({"name": "tag_stats", "arguments": {"tag": tag, "start": window[0], "end": window[1]}})
        else:
            calls.append(
                {
                    "name": "read_history",
                    "arguments": {"tag": tag, "start": window[0], "end": window[1], "max_points": 48},
                }
            )
    if _wants_alarms(question) and not alarm_for_tag and windows:
        calls.insert(0, _alarm_call(question, windows[0], None))
    return calls[:5]


class FakeClient:
    def __init__(self, store: PlantStore):
        self.store = store

    def complete(self, messages: list[dict], tools: list[dict] | None) -> dict:
        question = _user_question(messages)
        done = _done_calls(messages)
        stop = any(
            message.get("role") == "user" and "Stop calling tools" in str(message.get("content", ""))
            for message in messages
        )
        planned = [] if stop else plan_calls(question, self.store)
        if tools and len(done) < len(planned):
            call = planned[len(done)]
            return {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": f"call_{len(done) + 1}",
                        "name": call["name"],
                        "arguments": call["arguments"],
                    }
                ],
            }
        return {"role": "assistant", "content": compose_answer(question, done), "tool_calls": []}


def compose_answer(question: str, done: list[tuple[dict, dict]]) -> str:
    if not done:
        return refusal_text(question) or "I don't know. The question did not map onto the plant tools."
    parts: list[str] = []
    for call, result in done:
        if result.get("error"):
            parts.append(f"{call['name']} returned an error: {result['error']}")
            continue
        name = call["name"]
        if name == "search_tags":
            parts.append(_format_search(result))
        elif name == "tag_stats":
            parts.append(_format_stats(result))
        elif name == "read_history":
            parts.append(_format_history(question, result))
        elif name == "query_alarms":
            parts.append(_format_alarms(question, result))
    extra = " ".join(
        piece
        for piece in (
            _slope_note(question, done),
            _drop_note(question, done),
            _which_first(question, done),
            _deltas(question, done),
            _both_running(question, done),
            _windows_note(question, done),
            _conversions(question, done),
        )
        if piece
    )
    if extra:
        parts.append(extra)
    if _hedge(question):
        parts.append(
            "I don't know whether this was a bearing failure or a faulty instrument. "
            "The historian shows the tag values only."
        )
    text = "\n".join(part for part in parts if part)
    return text or "I don't know. The tool results do not answer the question."


def _month(name: str) -> int:
    return {"march": 3, "mar": 3}[name.lower()]


def _stamp(year: int, month_name: str, day: int, hm: str) -> datetime:
    hour, minute = (int(part) for part in hm.split(":"))
    if hour == 24 and minute == 0:
        return datetime(year, _month(month_name), day, tzinfo=timezone.utc) + timedelta(days=1)
    return datetime(year, _month(month_name), day, hour, minute, tzinfo=timezone.utc)


def _hedge(question: str) -> bool:
    text = question.lower()
    return "definitely" in text and ("faulty" in text or "bearing failure" in text)


def _is_lookup(question: str) -> bool:
    text = question.lower()
    if "between" in text or "through" in text:
        return False
    marks = (
        "what is the tag",
        "which tag",
        "list the temperature",
        "is there a",
        "alarm limits",
    )
    return any(mark in text for mark in marks)


def _lookup_query(question: str) -> str:
    text = question.lower()
    if "basin level" in text:
        return "basin level"
    if "p-101b" in text or "standby" in text:
        return "P-101B running"
    if "bearing" in text:
        return "bearing temperature"
    if "e-201" in text and "temperature" in text:
        return "E-201 temperature"
    if "discharge flow" in text:
        return "discharge flow"
    return "cooling water"


def _wants_alarms(question: str) -> bool:
    text = question.lower()
    return any(word in text for word in ("alarm", "chatter", "unacknowledged", "priority", "urgent"))


def _wants_history(question: str) -> bool:
    text = question.lower()
    marks = (
        "timeline",
        "which came first",
        "how long",
        "what time",
        "behave",
        "collapse",
        "reaching",
        "falling below",
        "what happened",
        "what did",
        "moved",
        "running",
    )
    return any(mark in text for mark in marks)


def _wants_stats(question: str) -> bool:
    text = question.lower()
    marks = ("mean", "average", "slope", "drift", "litre", "fahrenheit", "metres", " in bar", "psi")
    return any(mark in text for mark in marks)


def _tags_in(question: str, store: PlantStore) -> list[str]:
    found: list[str] = []
    for match in _TAG.findall(question):
        if match not in found and store.tag(match) is not None:
            found.append(match)
    return found


def _before_tag(question: str, tag: str) -> str:
    index = question.lower().find(tag.lower())
    if index < 0:
        return ""
    return question[max(0, index - 70) : index]


def _alarm_call(question: str, window: tuple[str, str], tag: str | None) -> dict:
    text = question.lower()
    arguments: dict = {"start": window[0], "end": window[1], "limit": 40}
    if any(phrase in text for phrase in ("came in", "how many", "occurred")):
        arguments["onset_only"] = True
    if tag:
        arguments["tag"] = tag
    if "timeline" in text:
        arguments["include_events"] = True
    return {"name": "query_alarms", "arguments": arguments}


def _user_question(messages: list[dict]) -> str:
    for message in messages:
        if message.get("role") == "user" and "Stop calling tools" not in str(message.get("content", "")):
            return str(message.get("content", ""))
    return ""


def _done_calls(messages: list[dict]) -> list[tuple[dict, dict]]:
    pending: list[dict] = []
    done: list[tuple[dict, dict]] = []
    for message in messages:
        if message.get("role") == "assistant":
            pending.extend(message.get("tool_calls") or [])
        elif message.get("role") == "tool" and pending:
            call = pending.pop(0)
            content = message.get("content")
            if not isinstance(content, dict):
                content = {"error": "tool result was not an object"}
            done.append(({"name": call["name"], "arguments": call.get("arguments") or {}}, content))
    return done


def _format_search(result: dict) -> str:
    rows = result.get("tags") or []
    if not rows:
        return f"No tags matched {result.get('query')!r}."
    lines = []
    for row in rows:
        limits = f" Alarm limits: {row['alarm_limits']}." if row.get("alarm_limits") else ""
        lines.append(
            f"{row['tag']}: {row['description']} on {row['equipment']}, "
            f"unit {row['eng_unit']}, range {row['range_min']} to {row['range_max']}.{limits}"
        )
    more = result.get("matched", len(rows)) - len(rows)
    if more > 0:
        lines.append(f"{more} more tags matched and were not listed.")
    return " ".join(lines)


def _format_stats(result: dict) -> str:
    if result.get("count", 0) == 0:
        return f"{result.get('tag')}: no samples in {result.get('start')} to {result.get('end')}."
    return (
        f"{result['tag']} ({result['eng_unit']}) from {result['start']} to {result['end']}: "
        f"n={result['count']}, mean={result['mean']}, min={result['min']} at {result['min_time']}, "
        f"max={result['max']} at {result['max_time']}, first={result['first']} at {result['first_time']}, "
        f"last={result['last']} at {result['last_time']}, slope={result['slope_per_hour']} {result['eng_unit']}/h."
    )


def _format_history(question: str, result: dict) -> str:
    points = result.get("points") or []
    tag = result.get("tag", "")
    if not points:
        return f"{tag}: no samples in the window."
    low = min(points, key=lambda point: point["value"])
    high = max(points, key=lambda point: point["value"])
    sentence = (
        f"{tag} ({result['eng_unit']}): {result['count']} samples, "
        f"first {points[0]['value']} at {points[0]['timestamp']}, "
        f"last {points[-1]['value']} at {points[-1]['timestamp']}, "
        f"min {low['value']} at {low['timestamp']}, max {high['value']} at {high['timestamp']}."
    )
    sentence += " " + _movement(tag, points, low, high)
    kind = _threshold(_after_tag(question, tag))
    if kind:
        sentence += " " + _crossing(tag, points, kind)
    return sentence


def _movement(tag: str, points: list[dict], low: dict, high: dict) -> str:
    """Describe the path, not only the end point. A trip that recovers is a dip, not a fall."""
    first = points[0]["value"]
    last = points[-1]["value"]
    end_delta = last - first
    peak_over = high["value"] - max(first, last)
    dip_under = min(first, last) - low["value"]
    if peak_over >= 5 and peak_over >= abs(end_delta):
        return f"{tag} peaked at {high['value']} at {high['timestamp']}."
    if dip_under >= 5 and dip_under >= abs(end_delta):
        return f"{tag} dipped to {low['value']} at {low['timestamp']}."
    if abs(end_delta) > 1 and peak_over < 5 and dip_under < 5:
        verb = "rose" if end_delta > 0 else "fell"
        return f"{tag} {verb} from {first} to {last}."
    if abs(end_delta) >= 0.5:
        return f"{tag} changed from {first} to {last}."
    return f"{tag} ended near where it started."


def _after_tag(question: str, tag: str) -> str:
    index = question.find(tag)
    if index < 0:
        return ""
    return question[index : index + 160]


def _threshold(clause: str) -> tuple[str, float] | None:
    found: list[tuple[int, str, float]] = []
    lowered = clause.lower()
    for phrase, kind in _THRESHOLD:
        match = re.search(rf"{phrase}\s+(-?\d+(?:\.\d+)?)", lowered)
        if match:
            found.append((match.start(), kind, float(match.group(1))))
    if not found:
        return None
    found.sort()
    return found[0][1], found[0][2]


def _crossing(tag: str, points: list[dict], kind: tuple[str, float]) -> str:
    direction, limit = kind
    if direction == "low":
        hit = next((point for point in points if point["value"] < limit), None)
        if hit is None:
            return f"{tag} did not go below {limit:g}."
        recover = next(
            (
                point
                for point in points
                if point["timestamp"] > hit["timestamp"] and point["value"] >= limit
            ),
            None,
        )
        if recover:
            return (
                f"{tag} went below {limit:g} at {hit['timestamp']} (value {hit['value']}) "
                f"and was back at or above {limit:g} at {recover['timestamp']}."
            )
        return f"{tag} went below {limit:g} at {hit['timestamp']} (value {hit['value']}) and did not recover in the window."
    hit = next((point for point in points if point["value"] >= limit), None)
    if hit is None:
        return f"{tag} did not reach {limit:g}."
    return f"{tag} reached {limit:g} at {hit['timestamp']} (value {hit['value']})."


def _format_alarms(question: str, result: dict) -> str:
    alarms = result.get("alarms") or []
    matched = result.get("matched", len(alarms))
    if matched == 0:
        text = f"No alarms in {result['start']} to {result['end']}."
        if result.get("events"):
            text += " " + _format_events(result["events"])
        return text
    rank = {"urgent": 0, "high": 1, "medium": 2, "low": 3}
    counts: dict[str, int] = {}
    for alarm in alarms:
        counts[alarm["priority"]] = counts.get(alarm["priority"], 0) + 1
    summary = ", ".join(f"{name}={counts[name]}" for name in sorted(counts, key=lambda item: rank.get(item, 9)))
    lines = [f"{matched} alarms in the window (by priority {summary})."]
    first = sorted(alarms, key=lambda alarm: (rank.get(alarm["priority"], 9), alarm["timestamp"]))[0]
    lines.append(
        f"Highest priority shown is {first['priority']}: {first['message']} "
        f"at {first['timestamp']} on {first['tag']}."
    )
    if len(alarms) >= 6:
        times = sorted(parse_time(alarm["timestamp"]) for alarm in alarms)
        span = (times[-1] - times[0]).total_seconds() / 60.0
        if span <= 40:
            lines.append(
                f"The onsets span {span:.0f} minutes. Triage them as one alarm flood "
                "until the initiating event is clear."
            )
    if "unacknowledged" in question.lower() or "30 minutes" in question.lower():
        lines.append(_unacked_sentence(alarms))
    chatter = _chatter_note(alarms)
    if chatter:
        lines.append(chatter)
    detail = "; ".join(
        f"{alarm['timestamp']} {alarm['priority']} {alarm['message']} acknowledged={alarm['acknowledged']}"
        for alarm in alarms[:8]
    )
    lines.append(detail + ".")
    if result.get("events"):
        lines.append(_format_events(result["events"]))
    return " ".join(lines)


def _format_events(events: list[dict]) -> str:
    shown = "; ".join(f"{event['timestamp']} {event['event_type']}: {event['message']}" for event in events[:6])
    return f"Operator events: {shown}."


def _unacked_sentence(alarms: list[dict]) -> str:
    from datetime import timedelta

    held = []
    for alarm in alarms:
        onset = parse_time(alarm["timestamp"])
        if not alarm["acknowledged"]:
            held.append(alarm)
            continue
        if parse_time(alarm["ack_timestamp"]) - onset >= timedelta(minutes=30):
            held.append(alarm)
    if not held:
        return "None of these alarms were still unacknowledged 30 minutes after onset."
    shown = "; ".join(f"{alarm['timestamp']} {alarm['priority']} {alarm['message']}" for alarm in held[:12])
    return f"Still unacknowledged 30 minutes after onset: {shown}."


def _chatter_note(alarms: list[dict]) -> str:
    grouped: dict[str, list[datetime]] = {}
    for alarm in alarms:
        grouped.setdefault(alarm["tag"], []).append(parse_time(alarm["timestamp"]))
    notes = []
    for tag, times in grouped.items():
        if len(times) < 5:
            continue
        times.sort()
        gaps = [(later - earlier).total_seconds() / 60.0 for earlier, later in zip(times, times[1:])]
        if gaps and max(gaps) <= 15:
            notes.append(
                f"{tag} came in {len(times)} times, with at most {max(gaps):.0f} minutes between onsets. "
                "That pattern is a chattering alarm."
            )
    return " ".join(notes)


def _slope_note(question: str, done: list[tuple[dict, dict]]) -> str:
    if "slope" not in question.lower() and "drift" not in question.lower():
        return ""
    bits = []
    for _call, result in done:
        if "slope_per_hour" in result:
            bits.append(f"{result['tag']} {result['slope_per_hour']} {result['eng_unit']}/h")
    if not bits:
        return ""
    return "Slopes: " + "; ".join(bits) + "."


def _drop_note(question: str, done: list[tuple[dict, dict]]) -> str:
    if "collapse" not in question.lower() and "same minutes" not in question.lower():
        return ""
    drops = []
    for _call, result in done:
        points = result.get("points") or []
        if len(points) < 2:
            continue
        first = points[0]["value"]
        hit = next((point for point in points if first > 1 and point["value"] < first * 0.5), None)
        if hit:
            drops.append((result["tag"], hit["timestamp"]))
    if len(drops) < 2:
        return ""
    if len({item[1] for item in drops}) == 1:
        names = " and ".join(item[0] for item in drops)
        return f"{names} both drop at {drops[0][1]}."
    return "Drop times differ: " + ", ".join(f"{tag} at {moment}" for tag, moment in drops) + "."


def _which_first(question: str, done: list[tuple[dict, dict]]) -> str:
    if "which came first" not in question.lower():
        return ""
    hits = []
    for _call, result in done:
        points = result.get("points") or []
        kind = _threshold(_after_tag(question, result.get("tag", "")))
        if not kind or not points:
            continue
        direction, limit = kind
        if direction == "low":
            hit = next((point for point in points if point["value"] < limit), None)
        else:
            hit = next((point for point in points if point["value"] >= limit), None)
        if hit:
            hits.append((hit["timestamp"], result["tag"], direction, limit))
    if len(hits) < 2:
        return ""
    hits.sort()
    first, second = hits[0], hits[1]
    minutes = int((parse_time(second[0]) - parse_time(first[0])).total_seconds() // 60)
    return (
        f"{first[1]} crossed its threshold first at {first[0]}. "
        f"{second[1]} crossed at {second[0]}. The crossings are {minutes} minutes apart."
    )


def _deltas(question: str, done: list[tuple[dict, dict]]) -> str:
    stats = {
        result["tag"]: result
        for _call, result in done
        if "mean" in result and "tag" in result
    }
    pairs = re.findall(r"(CW-[A-Z0-9-]+)\s+minus\s+(CW-[A-Z0-9-]+)", question)
    lines = []
    deltas = []
    for left, right in pairs:
        if left in stats and right in stats:
            diff = stats[left]["mean"] - stats[right]["mean"]
            lines.append(f"Mean {left} minus {right} is {diff:.3f} {stats[left]['eng_unit']}.")
            deltas.append((f"{left} minus {right}", abs(diff)))
    if len(deltas) >= 2 and "larger" in question.lower():
        winner = max(deltas, key=lambda item: item[1])
        lines.append(f"The larger absolute delta is {winner[0]} at {winner[1]:.3f}.")
    return " ".join(lines)


def _both_running(question: str, done: list[tuple[dict, dict]]) -> str:
    if "running" not in question.lower() and "both" not in question.lower():
        return ""
    series: dict[str, dict[str, float]] = {}
    for _call, result in done:
        if result.get("points") and result.get("tag"):
            series[result["tag"]] = {point["timestamp"]: point["value"] for point in result["points"]}
    left = series.get("CW-P101-ST-101")
    right = series.get("CW-P101-ST-102")
    if not left or not right:
        return ""
    both = [moment for moment in left.keys() & right.keys() if left[moment] >= 0.5 and right[moment] >= 0.5]
    if both:
        return f"Both run-status tags were 1 at {len(both)} sample(s), first at {min(both)}."
    return "CW-P101-ST-101 and CW-P101-ST-102 were not both 1 on any returned sample."


def _windows_note(question: str, done: list[tuple[dict, dict]]) -> str:
    if "same hour" not in question.lower() and "were they separate" not in question.lower():
        return ""
    spans = []
    for call, _result in done:
        arguments = call.get("arguments") or {}
        if arguments.get("start") and arguments.get("end"):
            spans.append((arguments.get("tag") or call["name"], arguments["start"], arguments["end"]))
    if len(spans) < 2:
        return ""
    first_end = parse_time(spans[0][2])
    second_start = parse_time(spans[1][1])
    start_a, end_a = parse_time(spans[0][1]), first_end
    start_b, end_b = second_start, parse_time(spans[1][2])
    if start_a < end_b and start_b < end_a:
        return "The two windows overlap."
    return (
        f"The windows are separate. {spans[0][0]} covers {spans[0][1]} to {spans[0][2]}. "
        f"{spans[1][0]} covers {spans[1][1]} to {spans[1][2]}."
    )


def _conversions(question: str, done: list[tuple[dict, dict]]) -> str:
    text = question.lower()
    lines = []
    for _call, result in done:
        if "mean" not in result:
            continue
        mean = result["mean"]
        unit = result.get("eng_unit")
        tag = result.get("tag")
        if unit == "m3/h" and "litre" in text:
            lines.append(
                f"Mean {tag} {mean} m3/h is {mean / M3H_TO_LPS:.3f} L/s (divide m3/h by {M3H_TO_LPS:g})."
            )
        if unit == "kPa" and " in bar" in text:
            lines.append(
                f"Mean {tag} {mean} kPa is {mean / KPA_TO_BAR:.3f} bar (divide kPa by {KPA_TO_BAR:g})."
            )
        if unit == "kPa" and "psi" in text:
            lines.append(
                f"Mean {tag} {mean} kPa is {mean / KPA_PER_PSI:.3f} psi (divide kPa by {KPA_PER_PSI})."
            )
        if unit == "degC" and "fahrenheit" in text:
            lines.append(
                f"Mean {tag} {mean} degC is {mean * 9.0 / 5.0 + 32.0:.3f} degF (F = C * 9/5 + 32)."
            )
        if unit == "%" and "metre" in text:
            match = re.search(r"(\d+(?:\.\d+)?)\s*metres", text)
            if match:
                span = float(match.group(1))
                metres = mean / 100.0 * span
                lines.append(
                    f"{tag} at {mean}% of a {span:g} m span is {metres:.3f} m "
                    f"(percent / 100 * {span:g})."
                )
    return " ".join(lines)
