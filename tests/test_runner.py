import json
from types import SimpleNamespace

from krl_sync.config import Config
from krl_sync.ingest import IngestConflict, IngestError
from krl_sync.kci import KciBlocked, KciClient, KciError, Pacer
from krl_sync.runner import Runner, new_state

STATIONS = {"status": 200, "data": [
    {"sta_id": "WIL0", "sta_name": "AREA JABODETABEK", "group_wil": 0, "fg_enable": 1},
    {"sta_id": "THB", "sta_name": "TANAH ABANG", "group_wil": 0, "fg_enable": 1},
    {"sta_id": "SUD", "sta_name": "SUDIRMAN", "group_wil": 0, "fg_enable": 1},
    {"sta_id": "OFF", "sta_name": "DISABLED", "group_wil": 0, "fg_enable": 0},
]}


def schedule(train):
    return {"train_id": train, "ka_name": "COMMUTER LINE BOGOR", "dest": "BOGOR", "time_est": "05:00:00", "color": "#E30A16"}


class FakeKci:
    """Answers by URL; a value that is an exception is raised."""

    def __init__(self, routes):
        self.routes = routes
        self.requests = 0
        self.calls = []
        self.pacer = SimpleNamespace(not_after=None)

    def answer(self, url):
        self.requests += 1
        self.calls.append(url)
        for part, value in self.routes.items():
            if part in url:
                value = value(url) if callable(value) else value
                if isinstance(value, Exception):
                    raise value
                return value
        raise KciError(f"HTTP 404 from {url}")

    def fetch(self, url, token=""):
        return self.answer(url)

    def fetch_many(self, urls, token="", concurrency=2):
        results = {}
        for key, url in urls.items():
            try:
                results[key] = self.answer(url)
            except KciBlocked:
                raise
            except KciError as e:
                results[key] = e
        return results


class FakeIngest:
    def __init__(self, conflict=False, reject=()):
        self.calls = []
        self.conflict = conflict
        self.reject = reject

    def stations(self, rows):
        self.calls.append(("stations", [r["code"] for r in rows]))
        return len(rows)

    def start(self, date, stations):
        if self.conflict:
            raise IngestConflict("busy", 409)
        self.calls.append(("start", date, stations))
        return 42

    def schedules(self, run_id, date, station, rows):
        if station in self.reject:
            raise IngestError("unknown station", 422)
        self.calls.append(("schedules", station, [r["train_number"] for r in rows]))
        return len(rows)

    def stops(self, run_id, date, trains):
        self.calls.append(("stops", [t["train_number"] for t in trains]))
        return sum(len(t["stops"]) for t in trains)

    def finish(self, run_id, date, status, stations, error=None):
        self.calls.append(("finish", status, stations, error))


def config(**overrides):
    return Config(**{"request_delay_ms": 0, **overrides})


def stops_for(url):
    return {"status": 200, "data": [{"station_id": "THB", "time_est": "05:00:00"}, {"station_id": "BOO", "time_est": "06:00:00"}]}


ROUTES = {
    "/stations": STATIONS,
    "stationid=THB": {"status": 200, "data": [schedule("1001"), schedule("1002")]},
    "stationid=SUD": {"status": 200, "data": [schedule("1002"), schedule("1003")]},
    "trainid=": stops_for,
}


def run(routes=ROUTES, ingest=None, cfg=None, **state_args):
    ingest = ingest or FakeIngest()
    cfg = cfg or config()
    runner = Runner(cfg, FakeKci(routes), ingest, log=lambda m: None, sleep=lambda s: None)
    state = runner.run(new_state(cfg, date="2026-10-06", **state_args))
    return state, ingest.calls


def test_full_run_all_enabled_stations():
    state, calls = run()

    assert state["status"] == "success"
    assert calls == [
        ("stations", ["THB", "SUD", "OFF"]),
        ("start", "2026-10-06", ["SUD", "THB"]),
        ("schedules", "SUD", ["1002", "1003"]),
        ("schedules", "THB", ["1001", "1002"]),
        ("stops", ["1001", "1002", "1003"]),
        ("finish", "success", ["SUD", "THB"], None),
    ]
    assert state["stats"]["stops"] == 6


def test_configured_stations_and_types():
    state, calls = run(stations=["THB"], types=["schedules"])

    assert state["status"] == "success"
    assert [c[0] for c in calls] == ["start", "schedules", "finish"]


def test_trains_always_include_schedules():
    state = new_state(config(), types=["trains"])
    assert state["types"] == ["schedules", "trains"]


def test_failed_station_and_trains_are_partial():
    routes = {"trainid=1002": KciError("HTTP 500"), **ROUTES, "stationid=SUD": KciError("HTTP 500 from sud")}
    state, calls = run(routes)

    assert state["status"] == "partial"
    assert calls[-1][0] == "finish" and calls[-1][1] == "partial"
    assert "1 station(s) failed" in calls[-1][3] and "1 train(s) without stop data" in calls[-1][3]


def test_rejected_station_is_skipped():
    state, calls = run(ingest=FakeIngest(reject=("SUD",)))

    assert state["status"] == "partial"
    assert state["failed_stations"] == {"SUD": "unknown station"}


def test_block_closes_the_run_as_failed():
    routes = {**ROUTES, "trainid=": KciBlocked("HTTP 403 (blocked by Cloudflare)")}
    state, calls = run(routes)

    assert state["status"] == "failed"
    assert calls[-1] == ("finish", "failed", ["SUD", "THB"], "HTTP 403 (blocked by Cloudflare)")


def test_conflict_is_skipped():
    state, calls = run(ingest=FakeIngest(conflict=True))

    assert state["status"] == "skipped"
    assert [c[0] for c in calls] == ["stations"]


def test_station_list_failure_with_configured_stations_continues():
    routes = {**ROUTES, "/stations": KciError("HTTP 500")}
    state, calls = run(routes, stations=["THB"])

    assert state["status"] == "success"
    assert calls[0] == ("start", "2026-10-06", ["THB"])


def test_early_stop_when_no_train_works():
    trains = [schedule(str(2000 + i)) for i in range(30)]
    routes = {**ROUTES, "stationid=THB": {"status": 200, "data": trains}, "trainid=": KciError("HTTP 500")}
    state, calls = run(routes, cfg=config(train_stops_concurrency=2), stations=["THB"])

    assert state["status"] == "partial"
    # Chunks of 8 (concurrency 2 * 4): stops after the 2nd chunk (16 failures >= 10).
    assert len(state["failed_trains"]) == 16


def test_resumes_across_invocations_with_json_state():
    cfg = config()
    ingest = FakeIngest()
    kci = FakeKci(ROUTES)
    ticks = iter(range(1000))
    runner = Runner(cfg, kci, ingest, log=lambda m: None, sleep=lambda s: None, clock=lambda: next(ticks))

    state = new_state(cfg, date="2026-10-06")
    invocations = 0
    while state["phase"] != "done":
        # Each invocation gets two units of work, the state crosses as JSON.
        state = json.loads(json.dumps(runner.run(state, deadline=next(ticks) + 2)))
        invocations += 1

    assert invocations > 1
    assert state["status"] == "success"
    assert [c[0] for c in ingest.calls] == ["stations", "start", "schedules", "schedules", "stops", "finish"]


def response(status, headers=None, body=b"{}"):
    return SimpleNamespace(status_code=status, headers=headers or {}, content=body, text=body.decode())


def test_kci_fetch_retries_429_then_blocks(monkeypatch):
    client = KciClient(config(), log=lambda m: None, sleep=lambda s: None)
    answers = iter([response(429, {"retry-after": "1"}), response(200, body=b'{"status":200,"data":[]}')])
    monkeypatch.setattr(client, "get", lambda url, token="": next(answers))
    assert client.fetch("https://www.kci.id/x") == {"status": 200, "data": []}

    monkeypatch.setattr(client, "get", lambda url, token="": response(429))
    try:
        client.fetch("https://www.kci.id/x")
        raise AssertionError("expected KciBlocked")
    except KciBlocked:
        pass


def test_kci_cloudflare_detection():
    from krl_sync.kci import is_blocked

    assert is_blocked(response(403, {"content-type": "text/html"}, b"Attention Required"))
    assert is_blocked(response(200, {"cf-mitigated": "challenge"}))
    assert not is_blocked(response(403, {"content-type": "application/json"}))
    assert not is_blocked(response(404, {"content-type": "text/html"}))


class FakeTime:
    def __init__(self):
        self.now = 0.0
        self.slept = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


def pacer(clock, **overrides):
    import random

    cfg = config(**{"request_delay_ms": (3000, 8000), "break_every": (3, 3), "break_seconds": (30, 90), **overrides})
    return Pacer(cfg, log=lambda m: None, sleep=clock.sleep, clock=clock.clock, rng=random.Random(1))


def requests_through(p, clock, n, between=0.0):
    for _ in range(n):
        p.wait()
        p.done()
        clock.now += between  # other work after the request (e.g. sending to api-krl)


def test_pacer_waits_a_random_3_to_8_seconds_and_breaks_every_few_requests():
    clock = FakeTime()
    p = pacer(clock)
    requests_through(p, clock, 8)

    waits = [round(s, 3) for s in clock.slept]
    assert len(waits) == 7, "no wait before the first request"
    breaks = [w for w in waits if w >= 30]
    assert len(breaks) == 2 and all(w <= 90 for w in breaks), "a break after every 3 requests"
    assert all(3 <= w <= 8 for w in waits if w < 30)
    assert len(set(waits)) == len(waits), "the waits vary"


def test_pacer_counts_other_work_and_skips_a_break_past_the_deadline():
    clock = FakeTime()
    p = pacer(clock, request_delay_ms=(5000, 5000), break_every=(1, 1))
    p.not_after = 20  # a 30-90 s break cannot end before this
    requests_through(p, clock, 3, between=2.0)

    # 5 s from the end of the previous request, 2 s of which went to other work; no break.
    assert clock.slept == [3.0, 3.0]


def test_pacer_off():
    clock = FakeTime()
    p = pacer(clock, request_delay_ms=0, break_seconds=0)
    requests_through(p, clock, 5)
    assert clock.slept == []
