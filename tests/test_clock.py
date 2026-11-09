"""Uhr, Tagesabschnitt, Formulierung."""

from datetime import datetime


def test_phase_boundaries_are_exact(env):
    assert env.phase_of(0) == "night"
    assert env.phase_of(5) == "night"
    assert env.phase_of(6) == "morning"
    assert env.phase_of(11) == "morning"
    assert env.phase_of(12) == "midday"
    assert env.phase_of(13) == "midday"
    assert env.phase_of(14) == "afternoon"
    assert env.phase_of(17) == "afternoon"
    assert env.phase_of(18) == "evening"
    assert env.phase_of(21) == "evening"
    assert env.phase_of(22) == "night"
    assert env.phase_of(23) == "night"


def test_phase_covers_every_hour(env):
    """Keine Stunde darf ohne Zuordnung bleiben."""
    seen = {env.phase_of(h) for h in range(24)}
    assert seen == {"night", "morning", "midday", "afternoon", "evening"}


def test_now_falls_back_to_utc_with_label(env, monkeypatch):
    def broken(zone):
        raise RuntimeError("keine Zone")

    monkeypatch.setattr(env, "ZoneInfo", broken)
    moment, zone = env.now_in()
    assert zone == "UTC"
    assert moment.tzinfo is not None


def test_duration_tiers(env):
    assert env.describe_duration(20) == "a minute"
    assert env.describe_duration(40 * 60) == "40 minutes"
    assert env.describe_duration(90 * 60) == "2 hours"
    assert env.describe_duration(5 * 3600) == "5 hours"
    assert env.describe_duration(60 * 60) == "an hour"
    assert env.describe_duration(23.6 * 3600) == "a day"
    assert env.describe_duration(86400) == "a day"
    assert env.describe_duration(3 * 86400) == "3 days"


def test_distance_labels(env, monkeypatch):
    now = datetime(2026, 7, 14, 12, 0).timestamp()
    same_day = datetime(2026, 7, 14, 8, 0).timestamp()
    yesterday = datetime(2026, 7, 13, 8, 0).timestamp()
    long_ago = datetime(2026, 7, 10, 8, 0).timestamp()

    assert env.describe_distance(same_day, now) == "today"
    assert env.describe_distance(yesterday, now) == "yesterday"
    assert env.describe_distance(long_ago, now) == "4 days ago"


def test_day_boundaries_same_day_and_midnight(env):
    before = datetime(2026, 7, 13, 10, 0).timestamp()
    later = datetime(2026, 7, 13, 23, 0).timestamp()
    assert env.day_boundaries(before, later) == 0

    pre = datetime(2026, 7, 13, 23, 55).timestamp()
    post = datetime(2026, 7, 14, 0, 5).timestamp()
    assert env.day_boundaries(pre, post) == 1
