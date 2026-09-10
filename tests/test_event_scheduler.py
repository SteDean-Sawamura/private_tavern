"""Tests for engine.event_scheduler module."""

import pytest
from engine.event_scheduler import EventScheduler, parse_time


class TestParseTime:
    def test_valid_iso(self):
        t = parse_time("2024-01-15T10:30:00")
        assert t is not None
        assert t.hour == 10 and t.minute == 30

    def test_none_input(self):
        assert parse_time(None) is None

    def test_empty_string(self):
        assert parse_time("") is None

    def test_invalid_string(self):
        assert parse_time("not-a-date") is None


class TestEventScheduler:
    def _make_script(self, cyclic=None, one_time=None):
        return {
            "cyclic_events": cyclic or [],
            "one_time_events": one_time or [],
        }

    def test_one_time_event_fires_in_window(self):
        script = self._make_script(one_time=[{
            "id": "bell",
            "trigger_time": "2024-01-15T12:00:00",
            "description": "午间铃声",
        }])
        scheduler = EventScheduler(script)
        state = {"fired_one_time_events": []}

        events = scheduler.check_events(
            state, "2024-01-15T11:00:00", "2024-01-15T13:00:00"
        )
        assert len(events) == 1
        assert events[0]["event_id"] == "bell"
        assert events[0]["type"] == "one_time"

    def test_one_time_event_not_fired_outside_window(self):
        script = self._make_script(one_time=[{
            "id": "bell",
            "trigger_time": "2024-01-15T12:00:00",
            "description": "午间铃声",
        }])
        scheduler = EventScheduler(script)
        state = {"fired_one_time_events": []}

        events = scheduler.check_events(
            state, "2024-01-15T13:00:00", "2024-01-15T14:00:00"
        )
        assert len(events) == 0

    def test_one_time_event_not_repeated(self):
        script = self._make_script(one_time=[{
            "id": "bell",
            "trigger_time": "2024-01-15T12:00:00",
            "description": "午间铃声",
        }])
        scheduler = EventScheduler(script)
        state = {"fired_one_time_events": ["bell"]}

        events = scheduler.check_events(
            state, "2024-01-15T11:00:00", "2024-01-15T13:00:00"
        )
        assert len(events) == 0

    def test_cyclic_event_fires(self):
        script = self._make_script(cyclic=[{
            "id": "patrol",
            "frequency_value": 1,
            "frequency_unit": "hour",
            "description": "巡逻",
        }])
        scheduler = EventScheduler(script)
        state = {
            "cyclic_event_trackers": {
                "patrol": {"next_fire": "2024-01-15T10:00:00"},
            },
        }

        events = scheduler.check_events(
            state, "2024-01-15T09:30:00", "2024-01-15T10:30:00"
        )
        assert len(events) == 1
        assert events[0]["event_id"] == "patrol"

    def test_cyclic_event_multiple_fires_in_window(self):
        script = self._make_script(cyclic=[{
            "id": "tick",
            "frequency_value": 1,
            "frequency_unit": "hour",
            "description": "每小时",
        }])
        scheduler = EventScheduler(script)
        state = {
            "cyclic_event_trackers": {
                "tick": {"next_fire": "2024-01-15T10:00:00"},
            },
        }

        events = scheduler.check_events(
            state, "2024-01-15T09:00:00", "2024-01-15T12:30:00"
        )
        assert len(events) == 3  # 10:00, 11:00, 12:00

    def test_cyclic_event_zero_frequency_skipped(self):
        script = self._make_script(cyclic=[{
            "id": "bad",
            "frequency_value": 0,
            "frequency_unit": "hour",
            "description": "坏事件",
        }])
        scheduler = EventScheduler(script)
        state = {
            "cyclic_event_trackers": {
                "bad": {"next_fire": "2024-01-15T10:00:00"},
            },
        }

        events = scheduler.check_events(
            state, "2024-01-15T09:00:00", "2024-01-15T12:00:00"
        )
        assert len(events) == 0

    def test_cyclic_event_expired(self):
        script = self._make_script(cyclic=[{
            "id": "temp",
            "frequency_value": 1,
            "frequency_unit": "day",
            "expires_at": "2024-01-14T00:00:00",
            "description": "过期事件",
        }])
        scheduler = EventScheduler(script)
        state = {
            "cyclic_event_trackers": {
                "temp": {"next_fire": "2024-01-15T10:00:00"},
            },
        }

        events = scheduler.check_events(
            state, "2024-01-15T09:00:00", "2024-01-15T11:00:00"
        )
        assert len(events) == 0

    def test_update_trackers_one_time(self):
        script = self._make_script(one_time=[{
            "id": "bell", "trigger_time": "2024-01-15T12:00:00", "description": "铃",
        }])
        scheduler = EventScheduler(script)
        state = {"fired_one_time_events": []}

        triggered = [{"type": "one_time", "event_id": "bell", "fire_time": "2024-01-15T12:00:00"}]
        new_state = scheduler.update_trackers(state, triggered, "2024-01-15T13:00:00")
        assert "bell" in new_state["fired_one_time_events"]

    def test_update_trackers_cyclic(self):
        script = self._make_script(cyclic=[{
            "id": "patrol",
            "frequency_value": 1,
            "frequency_unit": "hour",
            "description": "巡逻",
        }])
        scheduler = EventScheduler(script)
        state = {
            "cyclic_event_trackers": {
                "patrol": {"next_fire": "2024-01-15T10:00:00"},
            },
        }

        triggered = [{"type": "cyclic", "event_id": "patrol", "fire_time": "2024-01-15T10:00:00"}]
        new_state = scheduler.update_trackers(state, triggered, "2024-01-15T10:30:00")
        tracker = new_state["cyclic_event_trackers"]["patrol"]
        assert tracker["last_fired"] == "2024-01-15T10:00:00"
        assert tracker["next_fire"] == "2024-01-15T11:00:00"

    def test_events_sorted_by_fire_time(self):
        script = self._make_script(one_time=[
            {"id": "late", "trigger_time": "2024-01-15T14:00:00", "description": "晚"},
            {"id": "early", "trigger_time": "2024-01-15T10:00:00", "description": "早"},
        ])
        scheduler = EventScheduler(script)
        state = {"fired_one_time_events": []}

        events = scheduler.check_events(
            state, "2024-01-15T09:00:00", "2024-01-15T15:00:00"
        )
        assert len(events) == 2
        assert events[0]["event_id"] == "early"
        assert events[1]["event_id"] == "late"

    def test_no_events_when_time_not_advanced(self):
        script = self._make_script(one_time=[{
            "id": "x", "trigger_time": "2024-01-15T12:00:00", "description": "x",
        }])
        scheduler = EventScheduler(script)
        state = {"fired_one_time_events": []}

        # old_time == new_time
        events = scheduler.check_events(
            state, "2024-01-15T11:00:00", "2024-01-15T11:00:00"
        )
        assert len(events) == 0

        # new_time < old_time
        events = scheduler.check_events(
            state, "2024-01-15T12:00:00", "2024-01-15T11:00:00"
        )
        assert len(events) == 0
