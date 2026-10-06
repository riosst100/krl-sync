import pytest

from krl_sync.kci import KciError
from krl_sync.parse import parse_schedules, parse_stations, parse_stops, url_for


def test_stations_kci_shape_drops_area_headers():
    payload = {"status": 200, "data": [
        {"sta_id": "WIL0", "sta_name": "AREA JABODETABEK", "group_wil": 0, "fg_enable": 1},
        {"sta_id": "thb", "sta_name": "TANAH ABANG", "group_wil": 0, "fg_enable": 1},
        {"sta_id": "JAKK", "sta_name": "JAKARTA KOTA", "group_wil": "0", "fg_enable": 0},
        {"sta_id": "THB", "sta_name": "DUPLICATE", "fg_enable": 1},
        {"sta_id": "TOOLONGCODE1", "sta_name": "X"},
    ]}

    assert parse_stations(payload) == [
        {"code": "THB", "name": "Tanah Abang", "operational_area": 0, "kci_enabled": True, "is_active": True},
        {"code": "JAKK", "name": "Jakarta Kota", "operational_area": 0, "kci_enabled": False, "is_active": False},
    ]


def test_stations_alternative_shapes():
    assert parse_stations([{"code": "SUD", "name": "Sudirman"}])[0]["code"] == "SUD"
    assert parse_stations({"stations": [{"code": "SUD", "name": "Sudirman"}]})[0]["kci_enabled"] is True


def test_stations_errors():
    with pytest.raises(KciError, match="status 500"):
        parse_stations({"status": 500, "data": []})
    with pytest.raises(KciError, match="no station list"):
        parse_stations({"foo": 1})
    with pytest.raises(KciError, match="recognisable"):
        parse_stations([{"x": 1}])


def test_schedules_valid_rows_only():
    payload = {"status": 200, "data": [
        {"train_id": "5701C", "ka_name": "COMMUTER LINE CIKARANG", "route_name": "KAMPUNGBANDAN-CIKARANG",
         "dest": "CIKARANG", "time_est": "04:31:00", "color": "#0084D8", "dest_time": "05:40:00"},
        {"train_id": "5701C", "ka_name": "DUP", "dest": "X", "time_est": "05:00:00"},
        {"train_id": 1234, "ka_name": "COMMUTER LINE BOGOR", "dest": "BOGOR", "time_est": "06:00:00", "color": "", "dest_time": None},
        {"train_id": "BAD1", "ka_name": "X", "dest": "Y", "time_est": "25:00:00"},
        {"train_id": "BAD2", "ka_name": "X", "dest": "Y", "time_est": "06:00:00", "color": "blue"},
        {"ka_name": "no train id", "dest": "Y", "time_est": "06:00:00"},
    ]}

    rows = parse_schedules(payload)

    assert [r["train_number"] for r in rows] == ["5701C", "1234"]
    assert rows[0] == {
        "train_number": "5701C", "line_name": "COMMUTER LINE CIKARANG", "line_color": "#0084D8",
        "route_name": "KAMPUNGBANDAN-CIKARANG", "destination": "CIKARANG",
        "departure_time": "04:31:00", "destination_arrival_time": "05:40:00",
    }
    assert rows[1]["line_color"] is None and rows[1]["route_name"] is None


def test_schedules_errors():
    with pytest.raises(KciError, match="status 404 for schedules of THB"):
        parse_schedules({"status": 404, "message": "not found"}, "schedules of THB")
    with pytest.raises(KciError, match="missing data"):
        parse_schedules({"status": 200})


def test_stops():
    payload = {"status": 200, "data": [
        {"station_id": "thb", "time_est": "04:31:00", "transit_station": True},
        {"station_id": "SUD", "time_est": "04:35:00", "transit_station": "false"},
        {"station_id": "", "time_est": "04:40:00"},
        {"station_id": "MRI", "time_est": "bad"},
    ]}

    assert parse_stops(payload, "5701C") == [
        {"station_code": "THB", "time": "04:31:00", "is_transit": True},
        {"station_code": "SUD", "time": "04:35:00", "is_transit": False},
    ]

    with pytest.raises(KciError, match="no stops for 5701C"):
        parse_stops({"status": 200, "data": []}, "5701C")


def test_url_for():
    url = "https://www.kci.id/api/krl/schedules?stationid=THB&timefrom=00%3A00"
    assert url_for(url, "stationid", "{station}", "SUD") == "https://www.kci.id/api/krl/schedules?stationid=SUD&timefrom=00%3A00"
    assert url_for("https://x/{train}", "trainid", "{train}", "57 01") == "https://x/57%2001"
    with pytest.raises(KciError):
        url_for("https://x/schedules", "stationid", "{station}", "THB")
