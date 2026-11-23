from datetime import datetime, timezone
from pathlib import Path

from plant_evals.generate import SEED, write_plant
from plant_evals.store import PlantStore, find_root


def _files(directory: Path) -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in sorted(directory.glob("*.csv"))}


def test_same_seed_same_files(tmp_path: Path):
    left = tmp_path / "a"
    right = tmp_path / "b"
    write_plant(left, seed=SEED)
    write_plant(right, seed=SEED)
    assert _files(left) == _files(right)


def test_different_seed_changes_history(tmp_path: Path):
    left = tmp_path / "a"
    right = tmp_path / "b"
    write_plant(left, seed=1)
    write_plant(right, seed=2)
    assert (left / "history.csv").read_bytes() != (right / "history.csv").read_bytes()


def test_shape(store):
    assert 15 <= len(store.tags) <= 25
    flow = store.history["CW-P101-FT-101"]
    assert len(flow) == 3 * 24 * 12
    span = flow[-1][0] - flow[0][0]
    assert span.days >= 2
    assert flow[0][0] == datetime(2026, 3, 1, tzinfo=timezone.utc)
    assert {tag.tag for tag in store.tags} == set(store.history)


def test_quiet_day_trip_flood_and_chatter(store):
    march_1 = _between(store, datetime(2026, 3, 1, tzinfo=timezone.utc), datetime(2026, 3, 2, tzinfo=timezone.utc))
    assert march_1 == []

    flood = _between(
        store,
        datetime(2026, 3, 2, 6, 0, tzinfo=timezone.utc),
        datetime(2026, 3, 2, 7, 0, tzinfo=timezone.utc),
    )
    serious = [alarm for alarm in flood if alarm.priority in {"urgent", "high"}]
    assert len(serious) >= 6
    assert any(alarm.acknowledged for alarm in serious)
    assert any(not alarm.acknowledged for alarm in serious)

    chatter = [
        alarm
        for alarm in _between(
            store,
            datetime(2026, 3, 3, 15, 0, tzinfo=timezone.utc),
            datetime(2026, 3, 3, 17, 0, tzinfo=timezone.utc),
        )
        if alarm.tag == "CW-TK301-LT-301"
    ]
    assert len(chatter) >= 6
    gaps = [
        (later.timestamp - earlier.timestamp).total_seconds() / 60
        for earlier, later in zip(chatter, chatter[1:])
    ]
    assert gaps and max(gaps) <= 15
    assert chatter[0].acknowledged
    assert any(not alarm.acknowledged for alarm in chatter[1:])


def test_checked_in_data_matches_seed():
    from tempfile import TemporaryDirectory

    repo = find_root() / "data"
    with TemporaryDirectory() as tmp:
        write_plant(Path(tmp), seed=SEED)
        assert _files(Path(tmp)) == _files(repo)


def _between(store, start, end):
    return [alarm for alarm in store.alarms if start <= alarm.timestamp < end]
