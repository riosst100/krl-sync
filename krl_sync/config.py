"""
Settings from environment variables. The KCI_* names and defaults are the
same as in krl-app's backend/config/kci.php.
"""

import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

# WIB (Asia/Jakarta) has no daylight saving time: a fixed offset avoids
# depending on the tz database of the runtime.
WIB = timezone(timedelta(hours=7))

ALL_TYPES = ("stations", "schedules", "trains")


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def env_int(name: str, default: int) -> int:
    value = env(name)
    return int(value) if value.lstrip("-").isdigit() else default


def codes(value: str) -> list[str]:
    """ "thb, sud,THB" -> ["THB", "SUD"] """
    seen = []
    for code in value.split(","):
        code = code.strip().upper()
        if code and code not in seen:
            seen.append(code)
    return seen


def types(value: str) -> list[str]:
    wanted = [t.strip().lower() for t in value.split(",")]
    return [t for t in ALL_TYPES if t in wanted]


def today() -> str:
    return datetime.now(WIB).date().isoformat()


@dataclass
class Config:
    # KCI web API (same defaults as krl-app)
    stations_api_url: str = "https://www.kci.id/api/krl/stations"
    stations_api_token: str = ""
    schedules_api_url: str = "https://www.kci.id/api/krl/schedules?stationid=THB&timefrom=00%3A00&timeto=23%3A59"
    schedules_api_token: str = ""
    train_stops_api_url: str = "https://www.kci.id/api/krl/train-schedule?trainid=5701C"
    train_stops_api_token: str = ""

    timeout: int = 20
    request_delay_ms: int = 1000
    train_stops_concurrency: int = 2
    rate_limit_pause_seconds: int = 30
    impersonate: str = "chrome"

    # What to sync: station codes (empty = every station KCI marks as enabled)
    # and which parts ("stations", "schedules", "trains").
    sync_stations: list[str] = field(default_factory=list)
    sync_types: list[str] = field(default_factory=lambda: list(ALL_TYPES))
    # Whether Vercel Cron runs sync. krl-app's /ingest/config "auto_sync" overrides it.
    auto_sync: bool = True

    # Receiving API (krl-app /api/v1/ingest, shared-secret bearer token)
    ingest_url: str = "https://api-krl.inovasionline.com/api/v1/ingest"
    ingest_token: str = ""

    # Vercel: shared secret for the cron / manual trigger and the chained steps,
    # this deployment's public URL, and the working time per invocation.
    cron_secret: str = ""
    self_url: str = ""
    time_budget_seconds: int = 180

    @classmethod
    def from_env(cls) -> "Config":
        d = cls()
        production = env("VERCEL_PROJECT_PRODUCTION_URL")

        return cls(
            stations_api_url=env("KCI_STATIONS_API_URL", d.stations_api_url),
            stations_api_token=env("KCI_STATIONS_API_TOKEN"),
            schedules_api_url=env("KCI_SCHEDULES_API_URL", d.schedules_api_url),
            schedules_api_token=env("KCI_SCHEDULES_API_TOKEN"),
            train_stops_api_url=env("KCI_TRAIN_STOPS_API_URL", d.train_stops_api_url),
            train_stops_api_token=env("KCI_TRAIN_STOPS_API_TOKEN"),
            timeout=env_int("KCI_TIMEOUT", d.timeout),
            request_delay_ms=env_int("KCI_REQUEST_DELAY_MS", d.request_delay_ms),
            train_stops_concurrency=max(1, env_int("KCI_TRAIN_STOPS_CONCURRENCY", d.train_stops_concurrency)),
            rate_limit_pause_seconds=env_int("KCI_RATE_LIMIT_PAUSE_SECONDS", d.rate_limit_pause_seconds),
            impersonate=env("KCI_IMPERSONATE", d.impersonate) or d.impersonate,
            sync_stations=codes(env("KCI_SYNC_STATIONS")),
            sync_types=types(env("KCI_SYNC_TYPES", ",".join(ALL_TYPES))),
            auto_sync=env("AUTO_SYNC", "true").lower() not in ("0", "false", "off", "no"),
            ingest_url=env("INGEST_URL", d.ingest_url).rstrip("/"),
            ingest_token=env("INGEST_TOKEN"),
            cron_secret=env("CRON_SECRET"),
            # The production domain, not VERCEL_URL: generated deployment URLs
            # sit behind Vercel's deployment protection.
            self_url=(env("SELF_URL") or (f"https://{production}" if production else "")).rstrip("/"),
            time_budget_seconds=env_int("SYNC_TIME_BUDGET_SECONDS", d.time_budget_seconds),
        )
