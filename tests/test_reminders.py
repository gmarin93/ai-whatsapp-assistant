"""Tests for the schedule_reminder in-process tool.

These pin the time validation and that a valid request enqueues a deferred
``send_reminder`` ARQ job with the right args — without touching real Redis.
"""
import asyncio
import types
from datetime import datetime, timedelta

import pytest

from app.agent import reminders
from app.agent.reminders import TZ, schedule_reminder

# The @tool decorator wraps the coroutine; call the raw function directly.
_fn = schedule_reminder.coroutine


def _run(**kwargs):
    return asyncio.run(_fn(**kwargs))


def _stub_settings(monkeypatch, *, redis_url="redis://localhost:6379"):
    monkeypatch.setattr(
        reminders, "get_settings",
        lambda: types.SimpleNamespace(redis_url=redis_url, arq_queue_name="whatsapp:jobs"),
    )


class _FakePool:
    def __init__(self):
        self.jobs = []
        self.closed = False

    async def enqueue_job(self, *args, **kwargs):
        self.jobs.append((args, kwargs))

    async def close(self):
        self.closed = True


def _patch_arq(monkeypatch, pool):
    """Patch arq.connections.create_pool (imported inside the tool) to return *pool*."""
    import arq.connections

    async def _create_pool(_settings):
        return pool

    monkeypatch.setattr(arq.connections, "create_pool", _create_pool)


def test_missing_wa_id_errors():
    out = _run(when="2099-01-01T09:00:00", message="hi", state={})
    assert out.startswith("error:")


def test_invalid_datetime_errors(monkeypatch):
    _stub_settings(monkeypatch)
    out = _run(when="not-a-date", message="hi", state={"wa_id": "123"})
    assert "not a valid ISO-8601" in out


def test_past_time_errors(monkeypatch):
    _stub_settings(monkeypatch)
    past = (datetime.now(TZ) - timedelta(hours=1)).replace(microsecond=0).isoformat()
    out = _run(when=past, message="hi", state={"wa_id": "123"})
    assert "in the past" in out


def test_too_far_out_errors(monkeypatch):
    _stub_settings(monkeypatch)
    far = (datetime.now(TZ) + timedelta(days=60)).replace(microsecond=0).isoformat()
    out = _run(when=far, message="hi", state={"wa_id": "123"})
    assert "30 days" in out


def test_no_redis_errors(monkeypatch):
    _stub_settings(monkeypatch, redis_url="")
    future = (datetime.now(TZ) + timedelta(hours=1)).replace(microsecond=0).isoformat()
    out = _run(when=future, message="hi", state={"wa_id": "123"})
    assert "Redis" in out


def test_valid_reminder_enqueues_deferred_job(monkeypatch):
    _stub_settings(monkeypatch)
    pool = _FakePool()
    _patch_arq(monkeypatch, pool)

    future = (datetime.now(TZ) + timedelta(hours=2)).replace(microsecond=0).isoformat()
    out = _run(when=future, message="call the dentist", state={"wa_id": "50688887777"})

    assert out.startswith("Reminder set for")
    assert pool.closed is True
    assert len(pool.jobs) == 1
    args, kwargs = pool.jobs[0]
    assert args[0] == "send_reminder"
    assert args[1] == "50688887777"
    assert args[2] == "call the dentist"
    assert kwargs["_queue_name"] == "whatsapp:jobs"
    # Deferred ~2h out (allow slack for test execution time).
    assert 7100 <= kwargs["_defer_by"] <= 7200
