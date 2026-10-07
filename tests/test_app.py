import json

import pytest

from krl_sync import app
from krl_sync.config import Config
from krl_sync.ingest import IngestClient, IngestError
from tests.test_runner import ROUTES, FakeIngest, FakeKci


@pytest.fixture
def cfg():
    return Config(request_delay_ms=0, ingest_token="t", cron_secret="s", sync_stations=["ENV"])


@pytest.fixture
def fakes(monkeypatch):
    ingest = FakeIngest()
    monkeypatch.setattr(app, "KciClient", lambda config, log: FakeKci(ROUTES))
    monkeypatch.setattr(app, "ingest_for", lambda config, state: ingest)
    return ingest


def settings(monkeypatch, value):
    def fake(self):
        if isinstance(value, Exception):
            raise value
        return value
    monkeypatch.setattr(IngestClient, "config", fake)


def test_stations_from_krl_app(cfg, monkeypatch):
    settings(monkeypatch, {"stations": ["thb", "SUD"], "auto_sync": False})
    s = app.remote_settings(cfg)
    assert s == {"stations": ["THB", "SUD"], "auto_sync": False, "source": "krl-app", "error": None}


def test_falls_back_to_env(cfg, monkeypatch):
    settings(monkeypatch, IngestError("ingest/config: HTTP 404: Not Found", 404))
    s = app.remote_settings(cfg)
    assert s["stations"] == ["ENV"] and s["source"] == "env" and "404" in s["error"]


def test_cron_skipped_when_auto_sync_off(cfg, monkeypatch, fakes):
    settings(monkeypatch, {"stations": ["THB"], "auto_sync": False})
    status, body = app.start(cfg, {}, cron=True)
    assert body["status"] == "skipped" and fakes.calls == []


def test_manual_run_still_allowed_when_auto_sync_off(cfg, monkeypatch, fakes):
    settings(monkeypatch, {"stations": ["THB"], "auto_sync": False})
    status, body = app.start(cfg, {"types": ["schedules"]})
    assert status == 200 and body["status"] == "success" and body["stations_source"] == "krl-app"


def test_manual_steps_return_state_until_done(cfg, monkeypatch, fakes):
    settings(monkeypatch, {"stations": ["THB", "SUD"]})
    clock = iter(range(0, 100000, 10))
    monkeypatch.setattr(app.time, "monotonic", lambda: next(clock))
    import krl_sync.runner as runner_module
    monkeypatch.setattr(runner_module.time, "monotonic", lambda: next(clock))

    status, body = app.start(cfg, {"chain": ["0"], "budget": ["5"]})
    steps = 1
    while "state" in body:
        assert "continued" not in body
        status, body = app.resume(cfg, json.dumps({"state": body["state"], "chain": False, "budget": 5}).encode())
        steps += 1

    assert steps > 1
    assert body["status"] == "success" and body["percent"] == 100
    assert fakes.calls[-1][0] == "finish"


def test_abort_closes_the_run(cfg, monkeypatch, fakes):
    settings(monkeypatch, {"stations": ["THB"]})
    monkeypatch.setattr(app, "Runner", __import__("krl_sync.runner", fromlist=["Runner"]).Runner)
    from krl_sync.runner import new_state
    state = new_state(cfg, stations=["THB"])
    state.update(phase="stops", run_id=7)

    status, body = app.resume(cfg, json.dumps({"state": state, "abort": True}).encode())

    assert body["status"] == "failed"
    assert fakes.calls == [("finish", "failed", ["THB"], "Dihentikan manual.")]


def test_authorized(cfg):
    assert app.authorized(cfg, "Bearer s")
    assert not app.authorized(cfg, "Bearer x")
    assert not app.authorized(Config(), "Bearer ")


def test_background_returns_once_the_run_is_open_and_hands_over(cfg, monkeypatch, fakes):
    settings(monkeypatch, {"stations": ["THB", "SUD"]})
    handed = []
    monkeypatch.setattr(app, "continue_run", lambda config, state: handed.append(state) or "next invocation started")

    status, body = app.start(cfg, {"background": ["1"]})

    assert status == 200 and body["run_id"] == 42 and body["continued"] == "next invocation started"
    assert handed and handed[0]["phase"] == "schedules", "nothing synced yet: the chain does the work"
    assert [c[0] for c in fakes.calls] == ["stations", "start"]


def test_stop_closes_the_run_on_the_receiver(cfg, monkeypatch):
    closed = []
    monkeypatch.setattr(IngestClient, "finish", lambda self, run_id, date, status, stations, error=None: closed.append((run_id, status, error)))
    monkeypatch.setattr(IngestClient, "latest", lambda self: {"run_id": 7, "status": "failed"})

    status, body = app.resume(cfg, json.dumps({"stop_run": 7}).encode())

    assert status == 200 and body["run"]["status"] == "failed"
    assert closed == [(7, "failed", "Dihentikan dari halaman web.")]
