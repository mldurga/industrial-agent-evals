"""Four tools over the plant store.

search_tags, read_history, tag_stats, and query_alarms. Time windows are
UTC, start inclusive and end exclusive.
"""

from __future__ import annotations

import math
import re
from datetime import datetime

from plant_evals.store import Alarm, PlantStore, format_time, parse_time

MAX_POINTS = 48
DEFAULT_ALARM_LIMIT = 40


def tool_schemas() -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": "search_tags",
                "description": (
                    "Find tags by a short query. Every word must appear in the tag name, "
                    "description, or equipment. Returns engineering unit, range, and alarm limits."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Short text such as 'bearing temperature' or 'E-201'."},
                        "limit": {"type": "integer", "description": "Maximum rows to return. Default 20."},
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "read_history",
                "description": (
                    "Read one tag between start (inclusive) and end (exclusive). "
                    "Times are UTC, for example 2026-03-02T06:00:00Z. "
                    "Long windows are downsampled."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "tag": {"type": "string"},
                        "start": {"type": "string"},
                        "end": {"type": "string"},
                        "max_points": {"type": "integer"},
                    },
                    "required": ["tag", "start", "end"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "tag_stats",
                "description": (
                    "Count, mean, min, max, first, last, and least-squares slope per hour "
                    "for one tag. Start inclusive, end exclusive, UTC."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "tag": {"type": "string"},
                        "start": {"type": "string"},
                        "end": {"type": "string"},
                    },
                    "required": ["tag", "start", "end"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "query_alarms",
                "description": (
                    "Alarms that overlap a window (active between start inclusive and end exclusive). "
                    "Set onset_only true to keep only alarms whose onset is inside the window. "
                    "Set include_events true to also return operator events in the window."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "start": {"type": "string"},
                        "end": {"type": "string"},
                        "tag": {"type": "string"},
                        "equipment": {"type": "string"},
                        "priority": {
                            "type": "string",
                            "description": "urgent, high, medium, or low.",
                        },
                        "only_unacknowledged": {"type": "boolean"},
                        "onset_only": {"type": "boolean"},
                        "include_events": {"type": "boolean"},
                        "limit": {"type": "integer"},
                    },
                    "required": ["start", "end"],
                    "additionalProperties": False,
                },
            },
        },
    ]


class Toolset:
    def __init__(self, store: PlantStore):
        self.store = store

    def call(self, name: str, arguments: dict) -> dict:
        if not isinstance(arguments, dict):
            return {"error": "arguments must be an object"}
        try:
            if name == "search_tags":
                return self.search_tags(
                    str(arguments.get("query", "")),
                    int(arguments.get("limit", 20)),
                )
            if name == "read_history":
                return self.read_history(
                    str(arguments["tag"]),
                    str(arguments["start"]),
                    str(arguments["end"]),
                    int(arguments.get("max_points", 24)),
                )
            if name == "tag_stats":
                return self.tag_stats(
                    str(arguments["tag"]),
                    str(arguments["start"]),
                    str(arguments["end"]),
                )
            if name == "query_alarms":
                return self.query_alarms(
                    str(arguments["start"]),
                    str(arguments["end"]),
                    tag=arguments.get("tag"),
                    equipment=arguments.get("equipment"),
                    priority=arguments.get("priority"),
                    only_unacknowledged=bool(arguments.get("only_unacknowledged", False)),
                    onset_only=bool(arguments.get("onset_only", False)),
                    include_events=bool(arguments.get("include_events", False)),
                    limit=int(arguments.get("limit", DEFAULT_ALARM_LIMIT)),
                )
        except KeyError as exc:
            return {"error": f"missing argument {exc}"}
        except (TypeError, ValueError) as exc:
            return {"error": str(exc)}
        return {"error": f"unknown tool {name}"}

    def search_tags(self, query: str, limit: int = 20) -> dict:
        words = [word for word in re.split(r"[^a-z0-9.-]+", query.lower()) if word]
        limit = max(1, min(limit, 50))
        scored: list[tuple[int, dict]] = []
        for spec in self.store.tags:
            blob = " ".join((spec.tag, spec.description, spec.equipment, spec.area)).lower()
            if words and not all(word in blob for word in words):
                continue
            score = sum(blob.count(word) for word in words)
            scored.append((score, spec.as_dict()))
        scored.sort(key=lambda item: (-item[0], item[1]["tag"]))
        rows = [row for _, row in scored[:limit]]
        return {"query": query, "matched": len(scored), "tags": rows}

    def read_history(self, tag: str, start: str, end: str, max_points: int = 24) -> dict:
        spec = self._require_tag(tag)
        window = self._slice(spec.tag, start, end)
        max_points = max(1, min(int(max_points), MAX_POINTS))
        points, truncated = _downsample(window, max_points)
        return {
            "tag": spec.tag,
            "description": spec.description,
            "eng_unit": spec.eng_unit,
            "start": format_time(parse_time(start)),
            "end": format_time(parse_time(end)),
            "count": len(window),
            "truncated": truncated,
            "points": [
                {"timestamp": format_time(moment), "value": round(value, 4)}
                for moment, value in points
            ],
        }

    def tag_stats(self, tag: str, start: str, end: str) -> dict:
        spec = self._require_tag(tag)
        window = self._slice(spec.tag, start, end)
        if not window:
            return {
                "tag": spec.tag,
                "eng_unit": spec.eng_unit,
                "start": format_time(parse_time(start)),
                "end": format_time(parse_time(end)),
                "count": 0,
                "error": "no samples in window",
            }
        values = [value for _, value in window]
        first_t, first_v = window[0]
        last_t, last_v = window[-1]
        min_pair = min(window, key=lambda item: item[1])
        max_pair = max(window, key=lambda item: item[1])
        return {
            "tag": spec.tag,
            "description": spec.description,
            "eng_unit": spec.eng_unit,
            "start": format_time(parse_time(start)),
            "end": format_time(parse_time(end)),
            "count": len(window),
            "mean": round(_mean(values), 4),
            "min": round(min_pair[1], 4),
            "min_time": format_time(min_pair[0]),
            "max": round(max_pair[1], 4),
            "max_time": format_time(max_pair[0]),
            "stdev": round(_stdev(values), 4),
            "first": round(first_v, 4),
            "first_time": format_time(first_t),
            "last": round(last_v, 4),
            "last_time": format_time(last_t),
            "slope_per_hour": round(_slope_per_hour(window), 6),
        }

    def query_alarms(
        self,
        start: str,
        end: str,
        tag: str | None = None,
        equipment: str | None = None,
        priority: str | None = None,
        only_unacknowledged: bool = False,
        onset_only: bool = False,
        include_events: bool = False,
        limit: int = DEFAULT_ALARM_LIMIT,
    ) -> dict:
        start_t = parse_time(start)
        end_t = parse_time(end)
        if end_t <= start_t:
            raise ValueError("end must be after start")
        limit = max(1, min(int(limit), 100))
        matched: list[Alarm] = []
        for alarm in self.store.alarms:
            if onset_only:
                in_window = start_t <= alarm.timestamp < end_t
            else:
                returned = alarm.return_timestamp
                in_window = alarm.timestamp < end_t and (returned is None or returned > start_t)
            if not in_window:
                continue
            if tag and alarm.tag.lower() != str(tag).lower():
                continue
            if equipment and alarm.equipment.lower() != str(equipment).lower():
                continue
            if priority and alarm.priority.lower() != str(priority).lower():
                continue
            if only_unacknowledged and alarm.acknowledged:
                continue
            matched.append(alarm)
        matched.sort(key=lambda alarm: (alarm.timestamp, alarm.alarm_id))
        payload: dict = {
            "start": format_time(start_t),
            "end": format_time(end_t),
            "onset_only": onset_only,
            "matched": len(matched),
            "alarms": [alarm.as_dict() for alarm in matched[:limit]],
        }
        if include_events:
            events = [
                event.as_dict()
                for event in self.store.events
                if start_t <= event.timestamp < end_t
            ]
            payload["events"] = events
        return payload

    def _require_tag(self, tag: str):
        spec = self.store.tag(tag)
        if spec is None:
            known = ", ".join(item.tag for item in self.store.tags[:8])
            raise ValueError(f"unknown tag {tag}. Examples: {known}")
        return spec

    def _slice(self, tag: str, start: str, end: str) -> list[tuple[datetime, float]]:
        start_t = parse_time(start)
        end_t = parse_time(end)
        if end_t <= start_t:
            raise ValueError("end must be after start")
        return [
            (moment, value)
            for moment, value in self.store.history.get(tag, [])
            if start_t <= moment < end_t
        ]


def _mean(values: list[float]) -> float:
    return sum(values) / len(values)


def _stdev(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    center = _mean(values)
    return math.sqrt(sum((value - center) ** 2 for value in values) / (len(values) - 1))


def _slope_per_hour(window: list[tuple[datetime, float]]) -> float:
    origin = window[0][0]
    xs = [(moment - origin).total_seconds() / 3600.0 for moment, _ in window]
    ys = [value for _, value in window]
    mean_x = _mean(xs)
    mean_y = _mean(ys)
    variance = sum((item - mean_x) ** 2 for item in xs)
    if variance == 0:
        return 0.0
    covariance = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    return covariance / variance


def _downsample(
    window: list[tuple[datetime, float]], max_points: int
) -> tuple[list[tuple[datetime, float]], bool]:
    if len(window) <= max_points:
        return window, False
    stride = math.ceil(len(window) / max_points)
    picked = window[::stride]
    if picked[-1][0] != window[-1][0]:
        picked.append(window[-1])
    return picked[: max_points + 1], True
