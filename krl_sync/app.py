"""
The HTTP side of a Vercel invocation: authentication, building the run from
the query string and krl-app's settings, the time budget, and continuing an
unfinished run.

Two ways a run moves on after the time budget:
- automatic (Vercel Cron, or GET without chain=0): this invocation POSTs the
  state to the next one itself;
- manual (the web page): the response carries the state, the browser POSTs it
  back for the next step and shows the progress in between.
"""

import hmac
import json
import os
import time
import urllib.error
import urllib.request

from . import config as cfg_module
from .ingest import DryIngest, IngestClient, IngestError
from .kci import KciClient, KciError
from .parse import parse_schedules, parse_stations, url_for
from .runner import Runner, new_state, summary

# How long to wait for the next invocation to accept the hand-off. It keeps
# running after this caller stops waiting.
CHAIN_TIMEOUT = 10

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def log(message: str) -> None:
    print(f"[krl-sync] {message}", flush=True)


def authorized(config, header: str | None) -> bool:
    return bool(config.cron_secret) and hmac.compare_digest(header or "", f"Bearer {config.cron_secret}")


def remote_settings(config) -> dict:
    """
    krl-app's sync settings (GET /ingest/config): which stations, and whether
    this fallback may sync automatically. On any failure the env is used.
    """
    settings = {
        "stations": config.sync_stations,
        "auto_sync": config.auto_sync,
        "source": "env",
        "error": None,
    }
    if not config.ingest_token:
        settings["error"] = "INGEST_TOKEN is not set"
        return settings

    try:
        data = IngestClient(config.ingest_url, config.ingest_token, log).config()
    except IngestError as e:
        log(f"krl-app settings unavailable, using env: {e}")
        settings["error"] = str(e)
        return settings

    return {
        "stations": cfg_module.codes(",".join(str(c) for c in data["stations"])),
        "auto_sync": bool(data.get("auto_sync", config.auto_sync)),
        "source": "krl-app",
        "error": None,
    }


def cron_schedule() -> str | None:
    try:
        with open(os.path.join(ROOT, "vercel.json")) as f:
            crons = json.load(f).get("crons") or []
        return next((c["schedule"] for c in crons if c.get("path", "").startswith("/api/sync")), None)
    except (OSError, ValueError, KeyError):
        return None


def info(config) -> dict:
    """What the web page shows: the effective settings, never the secrets."""
    settings = remote_settings(config)
    return {
        "stations": settings["stations"],
        "stations_source": settings["source"],
        "settings_error": settings["error"],
        "auto_sync": settings["auto_sync"],
        "cron_schedule_utc": cron_schedule(),
        "types": config.sync_types,
        "ingest_url": config.ingest_url,
        "ingest_ready": bool(config.ingest_token),
        "time_budget_seconds": config.time_budget_seconds,
        "kci": {
            "stations": config.stations_api_url,
            "schedules": config.schedules_api_url,
            "train_stops": config.train_stops_api_url,
        },
    }


def start(config, query: dict, cron: bool = False) -> tuple[int, dict]:
    """
    GET: a new run, or ?info=1 / ?check=1.

    ?dry=1 &types= &stations= &limit_trains=   what to sync
    ?chain=0 &budget=N                         manual: return the state instead of continuing
    """
    first = lambda name: (query.get(name) or [""])[0].strip()

    if first("info") in ("1", "true"):
        return 200, info(config)
    if first("check") in ("1", "true"):
        return 200, check(config, first("station") or (config.sync_stations or ["THB"])[0])

    if first("stations"):
        stations, source = cfg_module.codes(first("stations")), "manual"
    else:
        settings = remote_settings(config)
        if cron and not settings["auto_sync"]:
            log(f"automatic sync is switched off ({settings['source']}), skipped")
            return 200, {"status": "skipped", "error": f"automatic sync is switched off ({settings['source']})"}
        stations, source = settings["stations"], settings["source"]

    limit = first("limit_trains")
    state = new_state(
        config,
        types=cfg_module.types(first("types")) if first("types") else None,
        stations=stations,
        dry=first("dry") in ("1", "true"),
        limit_trains=int(limit) if limit.isdigit() and int(limit) > 0 else None,
    )
    state["stations_source"] = source
    state["trigger"] = "schedule" if cron else "manual"

    return work(config, state, chain=first("chain") not in ("0", "false"), budget=first("budget"))


def resume(config, body: bytes) -> tuple[int, dict]:
    """POST {"state": {...}, "chain": bool, "budget": N, "abort": bool}: the next step of a run."""
    try:
        data = json.loads(body)
        state = data["state"]
        state["phase"]
    except (ValueError, KeyError, TypeError):
        return 400, {"error": "expected {\"state\": {...}}"}

    chain = data.get("chain", True) is not False
    if data.get("abort"):
        runner = Runner(config, KciClient(config, log), ingest_for(config, state), log)
        if state["phase"] != "done":
            runner.fail(state, "Dihentikan manual.")
        return 200, {**summary(state), "state": state}

    return work(config, state, chain=chain, budget=str(data.get("budget", "")))


def ingest_for(config, state):
    return DryIngest(log) if state["dry"] else IngestClient(config.ingest_url, config.ingest_token, log)


def work(config, state: dict, chain: bool = True, budget: str = "") -> tuple[int, dict]:
    seconds = config.time_budget_seconds
    if budget.isdigit():
        seconds = max(5, min(int(budget), seconds))
    deadline = time.monotonic() + seconds

    try:
        ingest = ingest_for(config, state)
    except IngestError as e:
        return 500, {"error": str(e)}

    state = Runner(config, KciClient(config, log), ingest, log).run(state, deadline)
    result = summary(state)

    if state["phase"] != "done":
        if chain:
            result["continued"] = continue_run(config, state)
            log(f"handed over at {result['progress']}: {result['continued']}")
        else:
            result["state"] = state

    return (500 if state["status"] == "failed" else 200), result


def continue_run(config, state: dict) -> str:
    """Starts the next invocation with the state; does not wait for it to finish."""
    if not config.self_url:
        return "not continued: SELF_URL / VERCEL_PROJECT_PRODUCTION_URL is not set"

    request = urllib.request.Request(
        f"{config.self_url}/api/sync",
        data=json.dumps({"state": state}).encode(),
        method="POST",
        headers={"Authorization": f"Bearer {config.cron_secret}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=CHAIN_TIMEOUT) as response:
            return f"next invocation finished: HTTP {response.status}"
    except TimeoutError:
        return "next invocation started"
    except urllib.error.HTTPError as e:
        return f"next invocation failed: HTTP {e.code}"
    except urllib.error.URLError as e:
        if isinstance(e.reason, TimeoutError):
            return "next invocation started"
        return f"next invocation failed: {e.reason}"


def check(config, station: str) -> dict:
    """Can this deployment reach KCI? One stations + one schedules request, nothing is sent."""
    kci = KciClient(config, log)
    result = {"station": station}

    for name, fetch in (
        ("stations", lambda: len(parse_stations(kci.fetch(config.stations_api_url, config.stations_api_token)))),
        ("schedules", lambda: len(parse_schedules(kci.fetch(
            url_for(config.schedules_api_url, "stationid", "{station}", station), config.schedules_api_token)))),
    ):
        try:
            result[name] = {"ok": True, "count": fetch()}
        except KciError as e:
            result[name] = {"ok": False, "error": str(e), "blocked": type(e).__name__ == "KciBlocked"}

    result["ok"] = all(result[n]["ok"] for n in ("stations", "schedules"))
    return result
