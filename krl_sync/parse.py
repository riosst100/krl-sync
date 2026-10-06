"""
KCI payload -> rows for the ingest API. Ports of krl-app's KciService
(parseStationList, parseSchedules) and TrainStopSyncService::parse, with the
same field names, validation rules and "skip invalid rows" behaviour.
"""

import re
from datetime import datetime
from urllib.parse import quote

from .kci import KciError

# Field names accepted per station attribute; the KCI "krl-webs" names first.
STATION_FIELDS = {
    "code": ["sta_id", "station_id", "code", "station_code", "kode", "id"],
    "name": ["sta_name", "station_name", "name", "nama"],
    "enabled": ["fg_enable", "is_active", "active", "enabled", "status"],
    "area": ["group_wil", "operational_area", "group", "wilayah", "daop"],
}

COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")


def is_area_header(code: str, name: str) -> bool:
    """KCI mixes area rows ("WIL0" / "AREA JABODETABEK") into the station list."""
    return bool(re.match(r"^WIL\d+$", code, re.I) or re.match(r"^AREA\s", name, re.I))


def is_time(value) -> bool:
    """Laravel's date_format:H:i:s."""
    if not isinstance(value, str) or not re.fullmatch(r"\d{2}:\d{2}:\d{2}", value):
        return False
    try:
        datetime.strptime(value, "%H:%M:%S")
        return True
    except ValueError:
        return False


def is_scalar(value) -> bool:
    return isinstance(value, (str, int, float, bool))


def php_string(value) -> str:
    """(string) cast as PHP does it: true -> "1", false -> ""."""
    if isinstance(value, bool):
        return "1" if value else ""
    return str(value)


def title(name: str) -> str:
    """Str::title(Str::lower($name)): "JAKARTA KOTA" -> "Jakarta Kota"."""
    return name.lower().title()


def check_status(payload, what: str) -> None:
    if isinstance(payload, dict) and isinstance(payload.get("status"), (int, float, str)):
        status = str(payload["status"]).strip()
        if status.lstrip("-").replace(".", "", 1).isdigit() and int(float(status)) != 200:
            raise KciError(f"status {status} for {what}")


def parse_stations(payload) -> list[dict]:
    """
    Station rows for /ingest/stations. Area header rows are dropped, codes are
    upper-cased and unique, names title-cased (as krl-app stores them).
    """
    check_status(payload, "stations")

    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        data = payload.get("data")
        rows = (data.get("stations") if isinstance(data, dict) else None) or data or payload.get("stations") or payload.get("result")
    else:
        rows = None

    if not isinstance(rows, list):
        raise KciError('no station list found (expected a JSON array, or one under "data"/"stations")')

    stations, seen = [], set()
    for row in rows:
        station = _station_row(row) if isinstance(row, dict) else None
        if station is None or station["code"] in seen:
            continue
        seen.add(station["code"])
        if not is_area_header(station["code"], station["raw_name"]):
            stations.append(station)

    if not seen and rows:
        first = rows[0] if isinstance(rows[0], dict) else {}
        raise KciError(f"station rows found, but none had a recognisable code and name ({', '.join(first)})")

    for station in stations:
        del station["raw_name"]

    return stations


def _station_row(row: dict) -> dict | None:
    row = {str(k).lower(): v for k, v in row.items()}

    def pick(attribute):
        for field in STATION_FIELDS[attribute]:
            if row.get(field) is not None and row.get(field) != "":
                return row[field]
        return None

    code, name = pick("code"), pick("name")
    if not is_scalar(code) or not isinstance(name, str) or len(php_string(code)) > 10 or len(name) > 100:
        return None

    enabled, area = pick("enabled"), pick("area")
    enabled = enabled is None or php_string(enabled).lower() in ("1", "true", "active", "aktif", "y", "yes")
    code = php_string(code).strip().upper()

    try:
        area = int(float(area)) if area is not None and not isinstance(area, bool) else None
    except (TypeError, ValueError):
        area = None

    return {
        "code": code,
        "name": title(name.strip()),
        "raw_name": name.strip(),
        "operational_area": area,
        "kci_enabled": enabled,
        # Only used for a station the receiver does not know yet.
        "is_active": enabled,
    }


def parse_schedules(payload, what: str = "schedules") -> list[dict]:
    """
    Schedule rows for /ingest/schedules from {"status":200,"data":[{"train_id",
    "ka_name","route_name","dest","time_est","color","dest_time"}]}.
    """
    if isinstance(payload, list):
        rows = payload
    else:
        check_status(payload, what)
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise KciError(f"missing data array for {what}")
        rows = payload["data"]

    schedules, seen = [], set()
    for row in rows:
        schedule = _schedule_row(row) if isinstance(row, dict) else None
        if schedule and schedule["train_number"] not in seen:
            seen.add(schedule["train_number"])
            schedules.append(schedule)

    return schedules


def _schedule_row(row: dict) -> dict | None:
    def optional(key):
        value = row.get(key)
        return None if value is None or value == "" else value

    train, line, dest = row.get("train_id"), row.get("ka_name"), row.get("dest")
    route, color, dest_time = optional("route_name"), optional("color"), optional("dest_time")

    valid = (
        is_scalar(train) and php_string(train).strip() != "" and len(php_string(train)) <= 20
        and isinstance(line, str) and line.strip() != "" and len(line) <= 100
        and isinstance(dest, str) and dest.strip() != "" and len(dest) <= 100
        and (route is None or (isinstance(route, str) and len(route) <= 150))
        and is_time(row.get("time_est"))
        and (dest_time is None or is_time(dest_time))
        and (color is None or (isinstance(color, str) and COLOR.match(color)))
    )
    if not valid:
        return None

    return {
        "train_number": php_string(train).strip(),
        "line_name": line.strip(),
        "line_color": color,
        "route_name": route.strip() if route else None,
        # Sent as published ("JAKARTAKOTA"): the receiver maps it to the station name.
        "destination": dest.strip(),
        "departure_time": row["time_est"],
        "destination_arrival_time": dest_time,
    }


def parse_stops(payload, train: str) -> list[dict]:
    """Stops of one train (KCI train-schedule?trainid=...), in published order."""
    check_status(payload, f"stops of {train}")

    rows = payload if isinstance(payload, list) else (payload.get("data") if isinstance(payload, dict) else None)
    if not isinstance(rows, list):
        raise KciError(f"missing data array for stops of {train}")

    stops = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        station, time = row.get("station_id"), row.get("time_est")
        if not isinstance(station, str) or station.strip() == "" or len(station) > 10 or not is_time(time):
            continue
        stops.append({
            "station_code": station.strip().upper(),
            "time": time,
            "is_transit": to_bool(row.get("transit_station", False)),
        })

    if not stops:
        raise KciError(f"no stops for {train}")

    return stops


def to_bool(value) -> bool:
    """PHP's filter_var(..., FILTER_VALIDATE_BOOL)."""
    if isinstance(value, bool):
        return value
    return php_string(value).strip().lower() in ("1", "true", "on", "yes")


def url_for(url: str, param: str, placeholder: str, value: str) -> str:
    """
    Replaces a {station}/{train} placeholder, or the value of the stationid /
    trainid query parameter, with the given code (ScheduleSyncService::urlForStation).
    """
    if placeholder in url:
        return url.replace(placeholder, quote(value, safe=""))

    replaced, count = re.subn(rf"([?&]{param}=)[^&#]*", lambda m: m.group(1) + quote(value, safe=""), url, count=1, flags=re.I)
    if count == 0:
        raise KciError(f'URL must contain a "{param}" query parameter or a {placeholder} placeholder: {url}')

    return replaced
