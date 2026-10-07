"""
One sync run as a resumable state machine. A Vercel invocation works until its
time budget is used up, then hands the (JSON) state to the next invocation:

    stations   fetch the KCI station list, send it (/ingest/stations), open the run (/ingest/start)
    schedules  one station per unit: fetch its timetable from KCI, send it (/ingest/schedules)
    stops      one chunk of trains per unit: fetch their stops from KCI, send them (/ingest/stops)
    finish     close the run (/ingest/finish: success | partial | failed)
    done

The order, retries and failure rules follow krl-app's ScheduleSyncService and
TrainStopSyncService.
"""

import time

from . import config as cfg_module
from .ingest import IngestConflict, IngestError
from .kci import KciBlocked, KciError
from .parse import parse_schedules, parse_stations, parse_stops, url_for

# Stop early when nothing works (e.g. the train stops API is down).
EARLY_STOP_FAILURES = 10


def new_state(config, date=None, types=None, stations=None, dry=False, limit_trains=None) -> dict:
    types = list(types if types is not None else config.sync_types)
    # Train numbers come from the timetable, and closing a run prunes the
    # receiver's schedules: a train stop sync always includes the schedules.
    if "trains" in types and "schedules" not in types:
        types.append("schedules")

    return {
        "date": date or cfg_module.today(),
        "types": [t for t in cfg_module.ALL_TYPES if t in types],
        "stations": list(stations if stations is not None else config.sync_stations),
        "dry": dry,
        "limit_trains": limit_trains,
        "phase": "stations",
        "run_id": None,
        "si": 0,
        "trains": [],
        "ti": 0,
        "stats": {"stations": 0, "stations_ok": 0, "schedules": 0, "trains": 0, "trains_ok": 0, "stops": 0, "kci_requests": 0, "invocations": 0},
        "failed_stations": {},
        "failed_trains": {},
        "status": None,
        "error": None,
    }


class Runner:
    def __init__(self, config, kci, ingest, log=print, sleep=time.sleep, clock=time.monotonic):
        self.config = config
        self.kci = kci
        self.ingest = ingest
        self.log = log
        self.sleep = sleep
        self.clock = clock

    def run(self, state: dict, deadline: float | None = None) -> dict:
        """Works on the run until it is done or the deadline (clock value) has passed."""
        state["stats"]["invocations"] += 1
        before = self.kci.requests

        try:
            while state["phase"] != "done" and (deadline is None or self.clock() < deadline):
                getattr(self, f"phase_{state['phase']}")(state)
        except IngestConflict as e:
            state.update(phase="done", status="skipped", error=f"The receiver is already running a synchronization ({e})")
        except (KciError, IngestError) as e:
            self.fail(state, str(e))
        except Exception as e:  # noqa: BLE001 - always close the run on the receiver
            self.fail(state, f"Unexpected error: {type(e).__name__}: {e}")
        finally:
            state["stats"]["kci_requests"] += self.kci.requests - before

        if state["phase"] == "done":
            self.log(f"run {state['run_id']} {state['status']}" + (f": {state['error']}" if state["error"] else ""))

        return state

    def fail(self, state: dict, message: str) -> None:
        self.log(f"sync failed: {message}")
        if state["run_id"] is not None and state["phase"] not in ("finish", "done"):
            try:
                self.ingest.finish(state["run_id"], state["date"], "failed", state["stations"], message)
            except Exception as e:  # noqa: BLE001 - best effort: the receiver expires stale runs
                self.log(f"could not close run {state['run_id']}: {e}")
        state.update(phase="done", status="failed", error=message)

    def phase_stations(self, state: dict) -> None:
        stats = state["stats"]
        need_list = "stations" in state["types"] or not state["stations"]

        if need_list:
            try:
                rows = parse_stations(self.kci.fetch(self.config.stations_api_url, self.config.stations_api_token))
            except KciBlocked:
                raise
            except KciError as e:
                if not state["stations"]:
                    raise KciError(f"station list: {e}") from None
                # The configured station codes are enough for the timetable.
                self.log(f"station list failed, continuing with KCI_SYNC_STATIONS: {e}")
                rows = []

            if rows and "stations" in state["types"]:
                stats["stations"] = self.ingest.stations(rows)
            if not state["stations"]:
                state["stations"] = sorted(r["code"] for r in rows if r["kci_enabled"])

        if "schedules" not in state["types"]:
            state.update(phase="done", status="success")
            return

        if not state["stations"]:
            raise KciError("no stations to sync")

        state["run_id"] = self.ingest.start(state["date"], state["stations"])
        state["phase"] = "schedules"
        self.log(f"run {state['run_id']}: {len(state['stations'])} station(s) for {state['date']}")

    def phase_schedules(self, state: dict) -> None:
        stats = state["stats"]

        if state["si"] >= len(state["stations"]):
            if stats["stations_ok"] == 0:
                state["phase"] = "finish"
            elif "trains" in state["types"] and self.config.train_stops_api_url:
                trains = sorted(set(state["trains"]))
                state["trains"] = trains[: state["limit_trains"]] if state["limit_trains"] else trains
                stats["trains"] = len(state["trains"])
                state["phase"] = "stops"
            else:
                state["trains"] = []
                state["phase"] = "finish"
            return

        code = state["stations"][state["si"]]
        try:
            url = url_for(self.config.schedules_api_url, "stationid", "{station}", code)
            rows = parse_schedules(self.kci.fetch(url, self.config.schedules_api_token), f"schedules of {code}")
            stats["schedules"] += self.ingest.schedules(state["run_id"], state["date"], code, rows)
            stats["stations_ok"] += 1
            state["trains"].extend(r["train_number"] for r in rows)
        except KciBlocked:
            raise
        except KciError as e:
            state["failed_stations"][code] = str(e)
            self.log(f"schedules of {code} failed: {e}")
        except IngestError as e:
            if e.status != 422:
                raise
            # E.g. a station the receiver does not know: skip it, keep the run.
            state["failed_stations"][code] = str(e)
            self.log(f"schedules of {code} rejected: {e}")

        state["si"] += 1

    def phase_stops(self, state: dict) -> None:
        stats = state["stats"]
        concurrency = self.config.train_stops_concurrency
        chunk = state["trains"][state["ti"]: state["ti"] + concurrency * 4]

        if not chunk or (stats["trains_ok"] == 0 and len(state["failed_trains"]) >= EARLY_STOP_FAILURES):
            state["phase"] = "finish"
            return

        try:
            urls = {t: url_for(self.config.train_stops_api_url, "trainid", "{train}", t) for t in chunk}
        except KciError as e:
            state["failed_trains"].update({t: str(e) for t in state["trains"][state["ti"]:]})
            state["phase"] = "finish"
            return

        token = self.config.train_stops_api_token
        trains = []

        # The upstream answers slowly: small parallel batches, then each failure retried once.
        for train, payload in self.kci.fetch_many(urls, token, concurrency).items():
            try:
                if isinstance(payload, KciError):
                    payload = self.kci.fetch(urls[train], token)
                trains.append({"train_number": train, "stops": parse_stops(payload, train)})
            except KciBlocked:
                raise
            except KciError as e:
                state["failed_trains"][train] = str(e)
                self.log(f"stops of {train} failed: {e}")

        if trains:
            stats["stops"] += self.ingest.stops(state["run_id"], state["date"], trains)
            stats["trains_ok"] += len(trains)

        state["ti"] += len(chunk)

    def phase_finish(self, state: dict) -> None:
        stats = state["stats"]
        problems = []

        if state["failed_stations"]:
            first = next(iter(state["failed_stations"].values()))
            problems.append(f"{len(state['failed_stations'])} station(s) failed. First error: {first}")
        if state["failed_trains"]:
            first = next(iter(state["failed_trains"].values()))
            problems.append(f"{len(state['failed_trains'])} train(s) without stop data. First error: {first}")

        if stats["stations_ok"] == 0:
            status = "failed"
        else:
            status = "partial" if problems else "success"

        error = " ".join(problems) or None
        self.ingest.finish(state["run_id"], state["date"], status, state["stations"], error)
        state.update(phase="done", status=status, error=error)

def percent(state: dict) -> int:
    """Rough overall progress: schedules up to 30 %, train stops up to 98 %."""
    stations, trains = len(state["stations"]) or 1, len(state["trains"]) or 1
    return {
        "stations": 2,
        "schedules": 2 + round(28 * state["si"] / stations),
        "stops": 30 + round(68 * state["ti"] / trains),
        "finish": 99,
    }.get(state["phase"], 100)


def summary(state: dict) -> dict:
    """What a response / log line shows: no train lists, a few failures only."""
    return {
        "date": state["date"],
        "phase": state["phase"],
        "percent": percent(state),
        "trigger": state.get("trigger"),
        "stations": state["stations"],
        "stations_source": state.get("stations_source"),
        "status": state["status"],
        "error": state["error"],
        "dry": state["dry"],
        "types": state["types"],
        "run_id": state["run_id"],
        "progress": {
            "stations": f"{state['si']}/{len(state['stations'])}",
            "trains": f"{state['ti']}/{len(state['trains'])}",
        },
        "stats": state["stats"],
        "failed_stations": dict(list(state["failed_stations"].items())[:5]),
        "failed_trains": dict(list(state["failed_trains"].items())[:5]),
    }
