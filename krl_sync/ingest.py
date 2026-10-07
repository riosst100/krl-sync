"""
Sends the synced data to krl-app's ingest API (POST /api/v1/ingest/*, bearer
SYNC_INGEST_TOKEN): start -> stations -> schedules (per station) -> stops (per
train chunk) -> finish. The same contract as krl-app's former IngestController
and ProdPushService.
"""

import json
import time
import urllib.error
import urllib.request

STATIONS_CHUNK = 200


class IngestError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class IngestConflict(IngestError):
    """HTTP 409: the receiver is already running a synchronization."""


class IngestClient:
    def __init__(self, url: str, token: str, log=print, retries: int = 3, sleep=time.sleep):
        if not url or not token:
            raise IngestError("INGEST_URL and INGEST_TOKEN must be set")
        self.url = url.rstrip("/")
        self.token = token
        self.log = log
        self.retries = retries
        self.sleep = sleep

    def post(self, path: str, body: dict) -> dict:
        return self.request("POST", path, body)

    def request(self, method: str, path: str, body: dict | None = None, retries: int | None = None) -> dict:
        """JSON request; connection errors and 5xx are retried, 4xx are raised."""
        retries = retries or self.retries
        request = urllib.request.Request(
            f"{self.url}/{path}",
            data=json.dumps(body).encode() if body is not None else None,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "krl-sync/1.0",
            },
        )

        for attempt in range(1, retries + 1):
            try:
                with urllib.request.urlopen(request, timeout=120) as response:
                    return json.loads(response.read() or b"{}")
            except urllib.error.HTTPError as e:
                message = self.describe(e)
                if e.code < 500 or attempt == retries:
                    error = IngestConflict if e.code == 409 else IngestError
                    raise error(f"ingest/{path}: HTTP {e.code}: {message}", e.code) from None
                self.log(f"ingest/{path}: HTTP {e.code}, retrying ({attempt}/{retries})")
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                if attempt == retries:
                    raise IngestError(f"ingest/{path}: cannot connect: {getattr(e, 'reason', e)}") from None
                self.log(f"ingest/{path}: {getattr(e, 'reason', e)}, retrying ({attempt}/{retries})")
            self.sleep(2)

    @staticmethod
    def describe(error: urllib.error.HTTPError) -> str:
        """The API's message plus the first validation errors, else the raw body."""
        body = error.read().decode(errors="replace")
        try:
            data = json.loads(body)
        except ValueError:
            return body[:300]
        message = str(data.get("message", ""))
        errors = data.get("errors")
        if isinstance(errors, dict):
            first = [f"{k}: {v[0] if isinstance(v, list) and v else v}" for k, v in list(errors.items())[:3]]
            message = f"{message} ({'; '.join(first)})"
        return message or body[:300]

    def config(self) -> dict:
        """
        The sync settings of krl-app's admin panel: {"stations": ["THB", ...]}
        (GET /ingest/config). Not retried: the caller falls back to the env.
        """
        data = self.request("GET", "config", retries=1).get("data")
        if not isinstance(data, dict) or not isinstance(data.get("stations"), list):
            raise IngestError('ingest/config: expected {"data": {"stations": [...]}}')
        return data

    def stations(self, rows: list[dict]) -> int:
        for i in range(0, len(rows), STATIONS_CHUNK):
            self.post("stations", {"stations": rows[i:i + STATIONS_CHUNK]})
        return len(rows)

    def start(self, date: str, stations: list[str]) -> int:
        return int(self.post("start", {"date": date, "stations": stations})["data"]["run_id"])

    def schedules(self, run_id: int, date: str, station: str, rows: list[dict]) -> int:
        self.post("schedules", {"run_id": run_id, "date": date, "station": station, "schedules": rows})
        return len(rows)

    def stops(self, run_id: int, date: str, trains: list[dict]) -> int:
        self.post("stops", {"run_id": run_id, "date": date, "trains": trains})
        return sum(len(t["stops"]) for t in trains)

    def progress(self, run_id: int, progress: dict) -> str:
        """Reports progress for the page; returns the run's status ("running" = go on)."""
        return str(self.request("POST", "progress", {"run_id": run_id, "progress": progress}, retries=1)["data"]["status"])

    def latest(self) -> dict | None:
        """The most recent run on the receiver, with its last reported progress."""
        return self.request("GET", "runs/latest", retries=1).get("data")

    def finish(self, run_id: int, date: str, status: str, stations: list[str], error: str | None = None) -> None:
        self.post("finish", {
            "run_id": run_id,
            "date": date,
            "status": status,
            "stations": stations,
            "error": error[:2000] if error else None,
        })


class DryIngest:
    """Dry run: fetches from KCI as usual, only logs what would be sent."""

    def __init__(self, log=print):
        self.log = log

    def stations(self, rows):
        self.log(f"[dry] stations: {len(rows)}")
        return len(rows)

    def start(self, date, stations):
        self.log(f"[dry] start {date}: {len(stations)} station(s)")
        return 0

    def schedules(self, run_id, date, station, rows):
        self.log(f"[dry] schedules {station}: {len(rows)}")
        return len(rows)

    def stops(self, run_id, date, trains):
        count = sum(len(t["stops"]) for t in trains)
        self.log(f"[dry] stops: {len(trains)} train(s), {count} stop(s)")
        return count

    def progress(self, run_id, progress):
        return "running"

    def finish(self, run_id, date, status, stations, error=None):
        self.log(f"[dry] finish: {status}" + (f" ({error})" if error else ""))
