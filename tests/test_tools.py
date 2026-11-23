from datetime import datetime, timedelta, timezone

import pytest

from plant_evals.store import PlantStore
from plant_evals.tools import Toolset


def test_search_history_stats_and_alarms(store):
    tools = Toolset(store)
    found = tools.search_tags("discharge flow")
    tags = [row["tag"] for row in found["tags"]]
    assert tags == ["CW-P101-FT-101"]
    assert found["tags"][0]["eng_unit"] == "m3/h"

    window = tools.read_history(
        "CW-P101-FT-101",
        "2026-03-02T06:00:00Z",
        "2026-03-02T07:00:00Z",
        max_points=48,
    )
    assert window["count"] == 12
    by_time = {point["timestamp"]: point["value"] for point in window["points"]}
    assert by_time["2026-03-02T06:05:00Z"] > 50
    assert by_time["2026-03-02T06:10:00Z"] == 0
    assert by_time["2026-03-02T06:20:00Z"] > 50

    # End is exclusive: the 07:00 sample is not included.
    assert "2026-03-02T07:00:00Z" not in by_time

    bearing = tools.tag_stats(
        "CW-P101-TT-101",
        "2026-03-02T04:00:00Z",
        "2026-03-02T06:10:00Z",
    )
    assert bearing["last"] > bearing["first"] + 20
    assert bearing["slope_per_hour"] > 0

    outlet = tools.tag_stats(
        "CW-E201-TT-202",
        "2026-03-01T00:00:00Z",
        "2026-03-04T00:00:00Z",
    )
    flow = tools.tag_stats(
        "CW-E201-FT-201",
        "2026-03-01T00:00:00Z",
        "2026-03-04T00:00:00Z",
    )
    assert outlet["slope_per_hour"] > 0.01
    assert abs(flow["slope_per_hour"]) < 0.05

    flood = tools.query_alarms(
        "2026-03-02T06:00:00Z",
        "2026-03-02T07:00:00Z",
        onset_only=True,
    )
    assert flood["matched"] >= 6
    assert all(
        "2026-03-02T06:00:00Z" <= alarm["timestamp"] < "2026-03-02T07:00:00Z"
        for alarm in flood["alarms"]
    )

    with_events = tools.query_alarms(
        "2026-03-02T06:00:00Z",
        "2026-03-02T07:00:00Z",
        include_events=True,
    )
    assert any(event["event_type"] == "TRIP" for event in with_events["events"])


def test_unknown_tag_and_bad_window(store):
    tools = Toolset(store)
    missing = tools.call("read_history", {"tag": "NO-SUCH", "start": "2026-03-01T00:00:00Z", "end": "2026-03-01T01:00:00Z"})
    assert "error" in missing
    backwards = tools.call(
        "tag_stats",
        {"tag": "CW-P101-FT-101", "start": "2026-03-02T00:00:00Z", "end": "2026-03-01T00:00:00Z"},
    )
    assert "error" in backwards


def test_stats_on_a_known_line(tmp_path):
    start = datetime(2026, 3, 1, tzinfo=timezone.utc)
    _write_tiny(tmp_path, start)
    tools = Toolset(PlantStore.load(tmp_path))
    stats = tools.tag_stats("TT-1", "2026-03-01T00:00:00Z", "2026-03-01T00:15:00Z")
    # Samples at 0, 5, 10 minutes with values 0, 1, 2. End at 15 is excluded.
    assert stats["count"] == 3
    assert stats["mean"] == 1
    assert stats["slope_per_hour"] == pytest.approx(12)


def test_alarm_overlap_versus_onset(tmp_path):
    start = datetime(2026, 3, 1, tzinfo=timezone.utc)
    _write_tiny(tmp_path, start)
    tools = Toolset(PlantStore.load(tmp_path))
    overlap = tools.query_alarms("2026-03-01T00:07:00Z", "2026-03-01T00:12:00Z")
    assert [row["alarm_id"] for row in overlap["alarms"]] == ["ALM-1"]
    onset = tools.query_alarms("2026-03-01T00:07:00Z", "2026-03-01T00:12:00Z", onset_only=True)
    assert onset["matched"] == 0
    later = tools.query_alarms("2026-03-01T00:20:00Z", "2026-03-01T00:30:00Z")
    assert later["matched"] == 0


def _write_tiny(directory, start: datetime) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "tags.csv").write_text(
        "tag,description,equipment,area,eng_unit,data_type,range_min,range_max,alarm_limits\n"
        "TT-1,Tiny temperature,E-1,AREA,degC,analog,0,100,HI 5 degC\n",
        encoding="utf-8",
    )
    rows = ["timestamp,tag,value"]
    for step in range(4):
        moment = start + timedelta(minutes=5 * step)
        rows.append(f"{moment.strftime('%Y-%m-%dT%H:%M:%SZ')},TT-1,{step}")
    (directory / "history.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
    (directory / "alarms.csv").write_text(
        "alarm_id,timestamp,tag,equipment,priority,condition,setpoint,value,message,"
        "return_timestamp,acknowledged,ack_timestamp,ack_user\n"
        "ALM-1,2026-03-01T00:05:00Z,TT-1,E-1,high,HI,5,6,high,2026-03-01T00:20:00Z,false,,\n",
        encoding="utf-8",
    )
    (directory / "events.csv").write_text(
        "timestamp,equipment,event_type,message,actor\n",
        encoding="utf-8",
    )
