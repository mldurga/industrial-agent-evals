"""Write a seeded cooling-water plant: tags, history, alarms, and events.

The series is deterministic for a given seed. Two runs with the same seed
write the same files. The story in the data:

- 1 March 2026 is quiet.
- P-101A bearing temperature ramps up before dawn on 2 March, then the pump
  trips at 06:10 UTC. P-101B starts at 06:20. That window is an alarm flood.
- E-201 process outlet temperature drifts up across the three days.
- On 3 March the E-201 bypass valve drifts open through the morning, and the
  basin level chatters around its high limit that afternoon.
"""

from __future__ import annotations

import argparse
import csv
import math
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from plant_evals.store import format_time

SEED = 42
START = datetime(2026, 3, 1, tzinfo=timezone.utc)
END = datetime(2026, 3, 4, tzinfo=timezone.utc)
STEP = timedelta(minutes=5)

TRIP_OFF = datetime(2026, 3, 2, 6, 10, tzinfo=timezone.utc)
TRIP_ON = datetime(2026, 3, 2, 6, 20, tzinfo=timezone.utc)
RAMP_START = datetime(2026, 3, 2, 4, 0, tzinfo=timezone.utc)
BYPASS_START = datetime(2026, 3, 3, 8, 0, tzinfo=timezone.utc)
BYPASS_END = datetime(2026, 3, 3, 14, 0, tzinfo=timezone.utc)
CHATTER_START = datetime(2026, 3, 3, 15, 0, tzinfo=timezone.utc)
CHATTER_END = datetime(2026, 3, 3, 16, 30, tzinfo=timezone.utc)

AREA = "COOLING-WATER"
FLOOD_START = datetime(2026, 3, 2, 6, 0, tzinfo=timezone.utc)
FLOOD_END = datetime(2026, 3, 2, 6, 40, tzinfo=timezone.utc)


@dataclass(frozen=True)
class TagSpec:
    tag: str
    description: str
    equipment: str
    eng_unit: str
    data_type: str
    range_min: float
    range_max: float


TAGS: tuple[TagSpec, ...] = (
    TagSpec("CW-P101-FT-101", "Cooling water pump discharge flow", "P-101", "m3/h", "analog", 0, 800),
    TagSpec("CW-P101-PT-101", "Pump P-101A discharge pressure", "P-101A", "kPa", "analog", 0, 1000),
    TagSpec("CW-P101-PT-100", "Pump P-101A suction pressure", "P-101A", "kPa", "analog", 0, 500),
    TagSpec("CW-P101-TT-101", "Pump P-101A bearing temperature", "P-101A", "degC", "analog", 0, 150),
    TagSpec("CW-P101-IT-101", "Pump P-101A motor current", "P-101A", "A", "analog", 0, 300),
    TagSpec("CW-P101-ZT-101", "Pump P-101A discharge valve position", "P-101A", "%", "analog", 0, 100),
    TagSpec("CW-P101-ST-101", "Pump P-101A running status", "P-101A", "0/1", "digital", 0, 1),
    TagSpec("CW-P101-ST-102", "Standby pump P-101B running status", "P-101B", "0/1", "digital", 0, 1),
    TagSpec("CW-P101-IT-102", "Pump P-101B motor current", "P-101B", "A", "analog", 0, 300),
    TagSpec("CW-HDR-PT-110", "Cooling water header pressure", "CW-HEADER", "kPa", "analog", 0, 1000),
    TagSpec("CW-E201-FT-201", "E-201 heat exchanger tube-side process flow", "E-201", "m3/h", "analog", 0, 400),
    TagSpec("CW-E201-TT-201", "E-201 heat exchanger process inlet temperature", "E-201", "degC", "analog", 0, 200),
    TagSpec("CW-E201-TT-202", "E-201 heat exchanger process outlet temperature", "E-201", "degC", "analog", 0, 200),
    TagSpec("CW-E201-TT-203", "E-201 heat exchanger cooling-water inlet temperature", "E-201", "degC", "analog", 0, 100),
    TagSpec("CW-E201-TT-204", "E-201 heat exchanger cooling-water outlet temperature", "E-201", "degC", "analog", 0, 100),
    TagSpec("CW-E201-PT-201", "E-201 heat exchanger process inlet pressure", "E-201", "kPa", "analog", 0, 1200),
    TagSpec("CW-E201-PT-202", "E-201 heat exchanger process outlet pressure", "E-201", "kPa", "analog", 0, 1200),
    TagSpec("CW-E201-ZT-201", "E-201 heat exchanger cooling-water bypass valve position", "E-201", "%", "analog", 0, 100),
    TagSpec("CW-TK301-LT-301", "Cooling water basin level", "TK-301", "%", "analog", 0, 100),
    TagSpec("CW-TK301-TT-301", "Cooling water basin temperature", "TK-301", "degC", "analog", 0, 80),
)

TAG_BY_NAME = {spec.tag: spec for spec in TAGS}


@dataclass(frozen=True)
class Rule:
    tag: str
    condition: str
    direction: str
    setpoint: float
    priority: str
    message: str

    def tripped(self, value: float) -> bool:
        if self.direction == "high":
            return value >= self.setpoint
        return value <= self.setpoint


RULES: tuple[Rule, ...] = (
    Rule("CW-P101-FT-101", "LO", "low", 250, "high", "Cooling water flow low"),
    Rule("CW-P101-FT-101", "LOLO", "low", 30, "urgent", "Cooling water flow lost"),
    Rule("CW-P101-PT-101", "LO", "low", 200, "high", "Pump discharge pressure low"),
    Rule("CW-P101-TT-101", "HI", "high", 85, "high", "P-101A bearing temperature high"),
    Rule("CW-P101-TT-101", "HIHI", "high", 95, "urgent", "P-101A bearing temperature high-high"),
    Rule("CW-E201-TT-202", "HI", "high", 55, "high", "E-201 process outlet temperature high"),
    Rule("CW-E201-TT-202", "HIHI", "high", 70, "urgent", "E-201 process outlet temperature high-high"),
    Rule("CW-TK301-LT-301", "HI", "high", 80, "medium", "Basin level high"),
    Rule("CW-E201-ZT-201", "HI", "high", 25, "low", "E-201 bypass valve open"),
    Rule("CW-HDR-PT-110", "LO", "low", 200, "high", "Cooling water header pressure low"),
    Rule("CW-P101-IT-102", "HI", "high", 190, "high", "P-101B motor overload"),
)


def _noise(rng: random.Random) -> float:
    u1 = max(rng.random(), 1e-12)
    u2 = rng.random()
    return math.sqrt(-2.0 * math.log(u1)) * math.cos(2.0 * math.pi * u2)


def _hours_since_start(moment: datetime) -> float:
    return (moment - START).total_seconds() / 3600.0


def _pumps(moment: datetime) -> tuple[bool, bool]:
    if moment < TRIP_OFF:
        return True, False
    if moment < TRIP_ON:
        return False, False
    return False, True


def _bypass(moment: datetime) -> float:
    if moment < BYPASS_START:
        return 8.0
    if moment >= BYPASS_END:
        return 45.0
    frac = (moment - BYPASS_START) / (BYPASS_END - BYPASS_START)
    return 8.0 + (45.0 - 8.0) * frac


def _bearing(moment: datetime, previous: float | None, noise: float) -> float:
    if moment < RAMP_START:
        return 63.5 + 0.25 * noise
    if moment <= TRIP_OFF:
        frac = (moment - RAMP_START) / (TRIP_OFF - RAMP_START)
        return 64.0 + 35.0 * frac
    assert previous is not None
    return previous + 0.08 * (38.0 - previous) + 0.15 * noise


def _level(moment: datetime, noise: float) -> float:
    if CHATTER_START <= moment <= CHATTER_END:
        steps = int((moment - CHATTER_START).total_seconds() // STEP.total_seconds())
        return 83.5 if steps % 2 == 0 else 76.2
    hour = moment.hour + moment.minute / 60.0
    return 57.0 + 2.2 * math.sin(2.0 * math.pi * hour / 24.0) + 0.25 * noise


def _grid(seed: int) -> list[tuple[datetime, dict[str, float]]]:
    rng = random.Random(seed)
    moment = START
    outlet = 42.0
    bearing: float | None = None
    prev_b = False
    rows: list[tuple[datetime, dict[str, float]]] = []
    while moment < END:
        draw = _noise(rng)
        running_a, running_b = _pumps(moment)
        bypass = _bypass(moment)
        hours = _hours_since_start(moment)
        fouling = 0.03 * hours
        inlet = 86.2 + 0.15 * math.sin(2.0 * math.pi * hours / 24.0) + 0.08 * draw
        if running_a or running_b:
            penalty = 0.08 * max(0.0, bypass - 8.0)
            target = 41.5 + fouling + penalty
        else:
            target = inlet - 1.5
        outlet = outlet + 0.55 * (target - outlet)
        flow = 0.0 if not (running_a or running_b) else 448.0 + 2.0 * draw
        discharge = 95.0 if not (running_a or running_b) else 515.0 + 1.5 * draw
        suction = 135.0 if not (running_a or running_b) else 105.0 + 0.8 * draw
        header = 40.0 if not (running_a or running_b) else 470.0 + 1.5 * draw
        bearing = _bearing(moment, bearing, draw)
        amps_a = 148.0 + 1.2 * draw if running_a else 0.0
        if running_b and not prev_b:
            amps_b = 210.0
        elif running_b:
            amps_b = 156.0 + 1.0 * draw
        else:
            amps_b = 0.0
        cw_in = 27.6 + 0.25 * math.sin(2.0 * math.pi * hours / 24.0)
        eff_cw = 0.0
        if running_a or running_b:
            eff_cw = 450.0 * (1.0 - 0.65 * bypass / 100.0)
        if eff_cw < 30.0:
            cw_out = cw_in + 0.4
        else:
            cw_out = cw_in + max(0.0, inlet - outlet) * (182.0 / eff_cw) * 0.55
        values = {
            "CW-P101-FT-101": flow,
            "CW-P101-PT-101": discharge,
            "CW-P101-PT-100": suction,
            "CW-P101-TT-101": bearing,
            "CW-P101-IT-101": amps_a,
            "CW-P101-ZT-101": 72.0 + 0.15 * draw,
            "CW-P101-ST-101": 1.0 if running_a else 0.0,
            "CW-P101-ST-102": 1.0 if running_b else 0.0,
            "CW-P101-IT-102": amps_b,
            "CW-HDR-PT-110": header,
            "CW-E201-FT-201": 182.0 + 0.3 * draw,
            "CW-E201-TT-201": inlet,
            "CW-E201-TT-202": outlet,
            "CW-E201-TT-203": cw_in,
            "CW-E201-TT-204": cw_out,
            "CW-E201-PT-201": 650.0 + 0.8 * draw,
            "CW-E201-PT-202": 615.0 + 0.8 * draw,
            "CW-E201-ZT-201": bypass,
            "CW-TK301-LT-301": _level(moment, draw),
            "CW-TK301-TT-301": cw_in - 0.4,
        }
        rows.append((moment, values))
        prev_b = running_b
        moment += STEP
    return rows


def _alarm_limits(tag: str) -> str:
    unit = TAG_BY_NAME[tag].eng_unit
    parts = [
        f"{rule.condition} {rule.setpoint:g} {unit}"
        for rule in RULES
        if rule.tag == tag
    ]
    return "; ".join(parts)


def _build_alarms(grid: list[tuple[datetime, dict[str, float]]]) -> list[dict]:
    active: dict[tuple[str, str], dict] = {}
    done: list[dict] = []
    for moment, values in grid:
        for rule in RULES:
            key = (rule.tag, rule.condition)
            if rule.tripped(values[rule.tag]):
                if key not in active:
                    active[key] = {
                        "timestamp": moment,
                        "tag": rule.tag,
                        "equipment": TAG_BY_NAME[rule.tag].equipment,
                        "priority": rule.priority,
                        "condition": rule.condition,
                        "setpoint": rule.setpoint,
                        "value": round(values[rule.tag], 4),
                        "message": rule.message,
                        "return_timestamp": None,
                    }
            elif key in active:
                record = active.pop(key)
                record["return_timestamp"] = moment
                done.append(record)
    done.extend(active.values())
    done.sort(key=lambda record: (record["timestamp"], record["tag"], record["condition"]))
    _assign_acks(done)
    for index, record in enumerate(done, start=1):
        record["alarm_id"] = f"ALM-{index:04d}"
    return done


def _ack(record: dict, minutes: int, user: str) -> None:
    record["acknowledged"] = True
    record["ack_timestamp"] = record["timestamp"] + timedelta(minutes=minutes)
    record["ack_user"] = user


def _assign_acks(records: list[dict]) -> None:
    first_level = True
    for record in records:
        record["acknowledged"] = False
        record["ack_timestamp"] = None
        record["ack_user"] = ""
        moment = record["timestamp"]
        in_flood = FLOOD_START <= moment < FLOOD_END
        if record["tag"] == "CW-TK301-LT-301" and record["condition"] == "HI":
            if first_level:
                _ack(record, 4, "m.chen")
                first_level = False
            continue
        if record["priority"] == "low":
            _ack(record, 12, "m.chen")
            continue
        if in_flood:
            bearing_high = record["tag"] == "CW-P101-TT-101" and record["condition"] == "HI"
            if record["priority"] == "urgent" or bearing_high:
                _ack(record, 18, "j.okonkwo")
            continue
        _ack(record, 8, "m.chen")


def _events() -> list[dict]:
    def row(moment: datetime, equipment: str, event_type: str, message: str, actor: str) -> dict:
        return {
            "timestamp": moment,
            "equipment": equipment,
            "event_type": event_type,
            "message": message,
            "actor": actor,
        }

    return [
        row(datetime(2026, 3, 1, 6, 0, tzinfo=timezone.utc), "CW", "SHIFT", "Day shift started. Plant steady.", "m.chen"),
        row(datetime(2026, 3, 1, 18, 0, tzinfo=timezone.utc), "CW", "SHIFT", "Night shift started. No transfers planned.", "j.okonkwo"),
        row(datetime(2026, 3, 2, 6, 0, tzinfo=timezone.utc), "CW", "SHIFT", "Day shift started.", "m.chen"),
        row(TRIP_OFF, "P-101A", "TRIP", "P-101A tripped. Discharge flow lost.", "system"),
        row(TRIP_ON, "P-101B", "START", "P-101B started from the standby selector.", "j.okonkwo"),
        row(
            datetime(2026, 3, 2, 6, 45, tzinfo=timezone.utc),
            "P-101A",
            "NOTE",
            "P-101A left out of service after high bearing temperature. Maintenance called.",
            "j.okonkwo",
        ),
        row(
            datetime(2026, 3, 3, 8, 30, tzinfo=timezone.utc),
            "E-201",
            "NOTE",
            "Bypass valve CW-E201-ZT-201 drifting open. Instrument technician notified.",
            "m.chen",
        ),
        row(datetime(2026, 3, 3, 18, 0, tzinfo=timezone.utc), "CW", "SHIFT", "Night shift started.", "j.okonkwo"),
    ]


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def write_plant(data_dir: Path, seed: int = SEED) -> dict:
    data_dir.mkdir(parents=True, exist_ok=True)
    grid = _grid(seed)
    history_rows = []
    for moment, values in grid:
        for spec in TAGS:
            history_rows.append(
                {
                    "timestamp": format_time(moment),
                    "tag": spec.tag,
                    "value": f"{values[spec.tag]:.4f}",
                }
            )
    tag_rows = [
        {
            "tag": spec.tag,
            "description": spec.description,
            "equipment": spec.equipment,
            "area": AREA,
            "eng_unit": spec.eng_unit,
            "data_type": spec.data_type,
            "range_min": f"{spec.range_min:g}",
            "range_max": f"{spec.range_max:g}",
            "alarm_limits": _alarm_limits(spec.tag),
        }
        for spec in TAGS
    ]
    alarms = _build_alarms(grid)
    alarm_rows = []
    for record in alarms:
        alarm_rows.append(
            {
                "alarm_id": record["alarm_id"],
                "timestamp": format_time(record["timestamp"]),
                "tag": record["tag"],
                "equipment": record["equipment"],
                "priority": record["priority"],
                "condition": record["condition"],
                "setpoint": f"{record['setpoint']:g}",
                "value": f"{record['value']:.4f}",
                "message": record["message"],
                "return_timestamp": format_time(record["return_timestamp"])
                if record["return_timestamp"]
                else "",
                "acknowledged": "true" if record["acknowledged"] else "false",
                "ack_timestamp": format_time(record["ack_timestamp"])
                if record["ack_timestamp"]
                else "",
                "ack_user": record["ack_user"],
            }
        )
    events = _events()
    event_rows = [
        {
            "timestamp": format_time(item["timestamp"]),
            "equipment": item["equipment"],
            "event_type": item["event_type"],
            "message": item["message"],
            "actor": item["actor"],
        }
        for item in events
    ]
    _write_csv(
        data_dir / "tags.csv",
        ["tag", "description", "equipment", "area", "eng_unit", "data_type", "range_min", "range_max", "alarm_limits"],
        tag_rows,
    )
    _write_csv(data_dir / "history.csv", ["timestamp", "tag", "value"], history_rows)
    _write_csv(
        data_dir / "alarms.csv",
        [
            "alarm_id",
            "timestamp",
            "tag",
            "equipment",
            "priority",
            "condition",
            "setpoint",
            "value",
            "message",
            "return_timestamp",
            "acknowledged",
            "ack_timestamp",
            "ack_user",
        ],
        alarm_rows,
    )
    _write_csv(
        data_dir / "events.csv",
        ["timestamp", "equipment", "event_type", "message", "actor"],
        event_rows,
    )
    return {
        "tags": len(tag_rows),
        "history_rows": len(history_rows),
        "alarms": len(alarm_rows),
        "events": len(event_rows),
        "seed": seed,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Write the seeded plant CSVs.")
    parser.add_argument("--out", type=Path, default=Path("data"), help="Output directory")
    parser.add_argument("--seed", type=int, default=SEED)
    args = parser.parse_args()
    summary = write_plant(args.out, seed=args.seed)
    print(
        f"Wrote {args.out}: {summary['tags']} tags, {summary['history_rows']} history rows, "
        f"{summary['alarms']} alarms, {summary['events']} events (seed {summary['seed']})."
    )


if __name__ == "__main__":
    main()
