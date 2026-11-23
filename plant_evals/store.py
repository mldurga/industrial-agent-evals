"""Load the plant CSVs and parse UTC timestamps."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


def find_root(start: Path | None = None) -> Path:
    """Repo root: directory that holds pyproject.toml and the data folder."""
    current = (start or Path.cwd()).resolve()
    for candidate in (current, *current.parents):
        if (candidate / "pyproject.toml").is_file() and (candidate / "data").is_dir():
            return candidate
    return current


def parse_time(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def format_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class Tag:
    tag: str
    description: str
    equipment: str
    area: str
    eng_unit: str
    data_type: str
    range_min: float
    range_max: float
    alarm_limits: str

    def as_dict(self) -> dict:
        return {
            "tag": self.tag,
            "description": self.description,
            "equipment": self.equipment,
            "area": self.area,
            "eng_unit": self.eng_unit,
            "data_type": self.data_type,
            "range_min": self.range_min,
            "range_max": self.range_max,
            "alarm_limits": self.alarm_limits,
        }


@dataclass(frozen=True)
class Alarm:
    alarm_id: str
    timestamp: datetime
    tag: str
    equipment: str
    priority: str
    condition: str
    setpoint: float
    value: float
    message: str
    return_timestamp: datetime | None
    acknowledged: bool
    ack_timestamp: datetime | None
    ack_user: str

    def as_dict(self) -> dict:
        return {
            "alarm_id": self.alarm_id,
            "timestamp": format_time(self.timestamp),
            "tag": self.tag,
            "equipment": self.equipment,
            "priority": self.priority,
            "condition": self.condition,
            "setpoint": self.setpoint,
            "value": self.value,
            "message": self.message,
            "return_timestamp": format_time(self.return_timestamp)
            if self.return_timestamp
            else "",
            "acknowledged": self.acknowledged,
            "ack_timestamp": format_time(self.ack_timestamp) if self.ack_timestamp else "",
            "ack_user": self.ack_user,
        }


@dataclass(frozen=True)
class Event:
    timestamp: datetime
    equipment: str
    event_type: str
    message: str
    actor: str

    def as_dict(self) -> dict:
        return {
            "timestamp": format_time(self.timestamp),
            "equipment": self.equipment,
            "event_type": self.event_type,
            "message": self.message,
            "actor": self.actor,
        }


@dataclass
class PlantStore:
    data_dir: Path
    tags: list[Tag]
    history: dict[str, list[tuple[datetime, float]]]
    alarms: list[Alarm]
    events: list[Event]

    def tag(self, name: str) -> Tag | None:
        for item in self.tags:
            if item.tag.lower() == name.lower():
                return item
        return None

    @classmethod
    def load(cls, data_dir: Path) -> PlantStore:
        tags = _read_tags(data_dir / "tags.csv")
        history = _read_history(data_dir / "history.csv")
        alarms = _read_alarms(data_dir / "alarms.csv")
        events = _read_events(data_dir / "events.csv")
        return cls(data_dir, tags, history, alarms, events)


def _read_tags(path: Path) -> list[Tag]:
    tags: list[Tag] = []
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            tags.append(
                Tag(
                    tag=row["tag"],
                    description=row["description"],
                    equipment=row["equipment"],
                    area=row["area"],
                    eng_unit=row["eng_unit"],
                    data_type=row["data_type"],
                    range_min=float(row["range_min"]),
                    range_max=float(row["range_max"]),
                    alarm_limits=row["alarm_limits"],
                )
            )
    return tags


def _read_history(path: Path) -> dict[str, list[tuple[datetime, float]]]:
    series: dict[str, list[tuple[datetime, float]]] = {}
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            series.setdefault(row["tag"], []).append(
                (parse_time(row["timestamp"]), float(row["value"]))
            )
    for points in series.values():
        points.sort(key=lambda item: item[0])
    return series


def _optional_time(value: str) -> datetime | None:
    text = (value or "").strip()
    if not text:
        return None
    return parse_time(text)


def _read_alarms(path: Path) -> list[Alarm]:
    alarms: list[Alarm] = []
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            alarms.append(
                Alarm(
                    alarm_id=row["alarm_id"],
                    timestamp=parse_time(row["timestamp"]),
                    tag=row["tag"],
                    equipment=row["equipment"],
                    priority=row["priority"],
                    condition=row["condition"],
                    setpoint=float(row["setpoint"]),
                    value=float(row["value"]),
                    message=row["message"],
                    return_timestamp=_optional_time(row["return_timestamp"]),
                    acknowledged=row["acknowledged"].lower() == "true",
                    ack_timestamp=_optional_time(row["ack_timestamp"]),
                    ack_user=row["ack_user"],
                )
            )
    return alarms


def _read_events(path: Path) -> list[Event]:
    events: list[Event] = []
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            events.append(
                Event(
                    timestamp=parse_time(row["timestamp"]),
                    equipment=row["equipment"],
                    event_type=row["event_type"],
                    message=row["message"],
                    actor=row["actor"],
                )
            )
    return events
