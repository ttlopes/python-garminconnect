"""Request-shape tests for the endpoints mapped from the Garmin Connect web app.

These tests stub the client so no network access happens; they pin the exact
path and query parameters each method sends, which is what matters for
compatibility with Garmin's undocumented API.
"""

from datetime import date, datetime

import pytest

from garminconnect import Garmin, GarminConnectConnectionError


class _Recorder:
    def __init__(self, result=None):
        self.calls: list[tuple[str, str, dict]] = []
        self.result = result if result is not None else {}

    def connectapi(self, path, **kwargs):
        self.calls.append(("GET", path, kwargs))
        return self.result


@pytest.fixture
def api(monkeypatch) -> tuple[Garmin, _Recorder]:
    g = Garmin()
    g.display_name = "user-uuid"
    g.time_zone = "Europe/Amsterdam"
    rec = _Recorder()
    monkeypatch.setattr(g, "connectapi", rec.connectapi)
    monkeypatch.setattr(g, "today", lambda: date(2026, 10, 16))
    return g, rec


def _last(rec: _Recorder):
    return rec.calls[-1]


def test_daily_stats_sends_type_and_current_date(api):
    g, rec = api
    g.get_daily_stats("2026-10-01", "2026-10-16", "calories")
    _, path, kw = _last(rec)
    assert path == "/usersummary-service/stats/daily/2026-10-01/2026-10-16"
    assert kw["params"] == {"statsType": "CALORIES", "currentDate": "2026-10-16"}


def test_daily_stats_rejects_unknown_type(api):
    g, _ = api
    with pytest.raises(ValueError):
        g.get_daily_stats("2026-10-01", "2026-10-16", "FLOORS")


def test_weekly_weight_uses_364_day_inclusive_window(api):
    g, rec = api
    g.get_weekly_weight("2026-10-16")
    assert _last(rec)[1] == "/weight-service/weight/weeklyRange/2025-10-18/2026-10-16"


def test_weekly_blood_pressure_defaults_to_today(api):
    g, rec = api
    g.get_weekly_blood_pressure()
    assert (
        _last(rec)[1]
        == "/bloodpressure-service/bloodpressure/weeklyRange/2025-10-18/2026-10-16"
    )


def test_calendar_week_uses_zero_based_month(api):
    g, rec = api
    g.get_calendar_week("2026-10-16", first_day_of_week=1)
    assert _last(rec)[1] == "/calendar-service/year/2026/month/9/day/16/start/1"


def test_calendar_week_rejects_bad_first_day(api):
    g, _ = api
    with pytest.raises(ValueError):
        g.get_calendar_week("2026-10-16", first_day_of_week=7)


def test_search_activities_lowercases_bools_and_validates_dates(api):
    g, rec = api
    g.search_activities(
        0, 5, activityType="running", favorite=True, startDate="2026-01-01"
    )
    _, path, kw = _last(rec)
    assert path == "/activitylist-service/activities/search/activities"
    assert kw["params"] == {
        "start": 0,
        "limit": 5,
        "activityType": "running",
        "favorite": "true",
        "startDate": "2026-01-01",
    }
    with pytest.raises(ValueError):
        g.search_activities(startDate="01-01-2026")
    with pytest.raises(ValueError):
        g.search_activities(**{"bad key": 1})


def test_max_metrics_range_aggregation_and_sport(api):
    g, rec = api
    g.get_max_metrics_range(
        "2026-09-01", "2026-10-16", aggregation="weekly", sport="Running"
    )
    _, path, kw = _last(rec)
    assert path == "/metrics-service/metrics/maxmet/weekly/2026-09-01/2026-10-16"
    assert kw["params"] == {"sport": "running"}
    g.get_max_metrics_range("2026-09-01", "2026-10-16")
    assert (
        _last(rec)[1] == "/metrics-service/metrics/maxmet/daily/2026-09-01/2026-10-16"
    )


def test_weekly_training_status_uses_display_name(api):
    g, rec = api
    g.get_weekly_training_status("2026-09-01", "2026-10-16")
    _, path, kw = _last(rec)
    assert path == "/metrics-service/metrics/trainingstatus/weekly/user-uuid"
    assert kw["params"] == {
        "fromCalendarDate": "2026-09-01",
        "toCalendarDate": "2026-10-16",
    }


def test_personal_record_optional_params(api):
    g, rec = api
    g.get_personal_record()
    assert _last(rec)[2]["params"] is None
    g.get_personal_record(include_history=True, months=6)
    assert _last(rec)[2]["params"] == {"includeHistory": "true", "months": "6"}


def test_get_workouts_filters(api):
    g, rec = api
    g.get_workouts(
        0,
        10,
        my_workouts_only=True,
        order_by="update_date",
        order_seq="desc",
        sport_type_keys=["Running", "cycling"],
    )
    assert _last(rec)[2]["params"] == {
        "start": 0,
        "limit": 10,
        "myWorkoutsOnly": "true",
        "orderBy": "UPDATE_DATE",
        "orderSeq": "DESC",
        "sportTypeKey": ["running", "cycling"],
    }
    with pytest.raises(ValueError):
        g.get_workouts(order_by="RANDOM")


def test_training_plans_params_only_when_given(api):
    g, rec = api
    g.get_training_plans()
    assert _last(rec)[2]["params"] is None
    g.get_training_plans(limit=50, start=0, status="Scheduled")
    assert _last(rec)[2]["params"] == {
        "limit": "50",
        "start": "0",
        "status": "Scheduled",
    }


def test_menstrual_dayview_sends_today_date(api):
    g, rec = api
    g.get_menstrual_data_for_date("2026-10-10")
    _, path, kw = _last(rec)
    assert path.endswith("/menstrualcycle/dayview/2026-10-10")
    assert kw["params"] == {"todayDate": "2026-10-16"}


def test_adjacent_activities_combines_prev_and_next(api, monkeypatch):
    g, _ = api
    answers = {"prev": {"activityId": 1}, "next": {"activityId": None}}
    monkeypatch.setattr(
        g, "connectapi", lambda path, **kw: answers[path.rsplit("/", 1)[1]]
    )
    assert g.get_adjacent_activities(42) == {"previous": 1, "next": None}


def test_ecg_uses_ghs_host(api):
    g, rec = api
    g.get_ecg_daily("2026-10-01", "2026-10-16")
    _, path, kw = _last(rec)
    assert path == "https://ghs.garmin.com/ghs/ecg/v1/ecg/daily"
    assert kw["params"] == {"firstDay": "2026-10-01", "lastDay": "2026-10-16"}
    with pytest.raises(ValueError):
        g.get_ecg("../etc")


def test_client_accepts_only_garmin_hosts_for_absolute_urls():
    from garminconnect.client import Client

    c = Client(domain="garmin.com")
    seen = {}

    class _Sess:
        def request(self, method, url, headers=None, **kw):
            seen["url"] = url

            class R:
                status_code = 200
                content = b"{}"

                def json(self):
                    return {}

            return R()

    c._api_session = _Sess()
    c.get_api_headers = dict  # type: ignore[assignment]
    c._run_request("GET", "https://ghs.garmin.com/ghs/ecg/v1/ecg/daily")
    assert seen["url"] == "https://ghs.garmin.com/ghs/ecg/v1/ecg/daily"
    with pytest.raises(ValueError):
        c._run_request("GET", "https://evil.example.com/x")
    with pytest.raises(ValueError):
        c._run_request("GET", "https://ghs.garmin.com@evil.example.com/x")


# Synthetic gear object in the shape gear-service v2 returns.
_GEAR_UUID = "11111111-2222-4333-8444-555555555555"
_GEAR = {
    "uuid": _GEAR_UUID,
    "gearType": "SHOES",
    "status": "ACTIVE",
    "name": "Trail shoes",
    "brand": "Example",
    "model": "Runner 1",
    "usageType": "DISTANCE",
    "maxUsageDistanceMeters": 500000.0,
    "maxUsageDurationSeconds": 0,
    "firstUseDate": "2026-01-15",
    "numActivitiesLinked": 0,
    "notes": "Old note",
    "associatedActivityTypes": [
        {"activityTypeKey": "running", "defaultGear": True, "preferredGear": False}
    ],
    "images": [],
}


def test_update_gear_puts_full_object_with_changes(monkeypatch):
    g = Garmin()
    g.display_name = "user-uuid"
    g.time_zone = "Europe/Amsterdam"
    sent = {}
    monkeypatch.setattr(g, "connectapi", lambda path, **kw: dict(_GEAR))

    def fake_put(kind, url, **kw):
        sent["url"] = url
        sent["json"] = kw["json"]

        class R:
            status_code = 204
            content = b""

        return R()

    monkeypatch.setattr(g.client, "put", fake_put)
    result = g.update_gear(_GEAR_UUID, notes="New note", max_usage_distance_km=700)
    assert sent["url"] == f"/gear-service/gear/v2/{_GEAR_UUID}"
    # Garmin requires the whole object: untouched fields are sent back as-is.
    assert sent["json"]["name"] == "Trail shoes"
    assert sent["json"]["numActivitiesLinked"] == 0
    assert sent["json"]["notes"] == "New note"
    assert sent["json"]["maxUsageDistanceMeters"] == 700000
    assert sent["json"]["associatedActivityTypes"] == _GEAR["associatedActivityTypes"]
    assert result["uuid"] == _GEAR_UUID  # re-read after the PUT
    with pytest.raises(ValueError):
        g.update_gear(_GEAR_UUID, name="   ")
    with pytest.raises(ValueError):
        g.update_gear("not-a-uuid", notes="x")


def test_delete_gear_uses_v2_endpoint(monkeypatch):
    g = Garmin()
    seen = {}

    def fake_delete(kind, url, **kw):
        seen["url"] = url

        class R:
            status_code = 204
            content = b""

        return R()

    monkeypatch.setattr(g.client, "delete", fake_delete)
    assert g.delete_gear(_GEAR_UUID) is None
    assert seen["url"] == f"/gear-service/gear/v2/{_GEAR_UUID}"


class _Resp:
    def __init__(self, body=None, status=200):
        self.status_code = status
        self.content = b"{}" if body is not None else b""
        self._body = body

    def json(self):
        return self._body


@pytest.fixture
def writer(monkeypatch):
    """Garmin instance whose client records PUT/POST/DELETE calls."""
    g = Garmin()
    g.display_name = "user-uuid"
    g.time_zone = "Europe/Amsterdam"
    g.profile_id = 4242
    calls: list[tuple[str, str, dict]] = []

    def rec(method):
        def fn(kind, url, **kw):
            calls.append((method, url, kw))
            return _Resp(kw.get("json"))

        return fn

    monkeypatch.setattr(g.client, "put", rec("PUT"))
    monkeypatch.setattr(g.client, "post", rec("POST"))
    monkeypatch.setattr(g.client, "delete", rec("DELETE"))
    monkeypatch.setattr(g, "today", lambda: date(2026, 10, 16))
    return g, calls


def test_update_blood_pressure_only_changes_notes(writer):
    g, calls = writer
    measurement = {"version": 17, "systolic": 120, "diastolic": 80, "notes": "old"}
    out = g.update_blood_pressure(measurement, notes="new")
    method, url, kw = calls[-1]
    assert (method, url) == ("PUT", "/bloodpressure-service/bloodpressure")
    assert kw["json"] == {
        "version": 17,
        "systolic": 120,
        "diastolic": 80,
        "notes": "new",
    }
    assert out["notes"] == "new"
    with pytest.raises(ValueError):
        g.update_blood_pressure({"systolic": 120}, notes="x")


def test_set_steps_goal_manual_and_auto(writer, monkeypatch):
    g, calls = writer
    current = {
        "deviceGoal": 7000,
        "userGoal": 0,
        "userGoalCategory": "my-auto",
        "goalType": "STEPS",
    }
    monkeypatch.setattr(g, "connectapi", lambda path, **kw: dict(current))
    g.set_steps_goal(9000)
    _method, url, kw = calls[-1]
    assert (
        url == "/wellness-service/wellness/wellness-goals/consolidated/steps/2026-10-16"
    )
    assert kw["json"]["userGoal"] == 9000 and kw["json"]["userGoalCategory"] == "manual"
    assert kw["json"]["deviceGoal"] == 7000
    g.set_steps_goal(None)
    assert calls[-1][2]["json"]["userGoal"] == 0
    assert calls[-1][2]["json"]["userGoalCategory"] == "my-auto"


def test_set_daily_goal_builds_dashed_path_except_weight(writer):
    g, calls = writer
    g.set_daily_goal("floors_climbed", 12)
    assert calls[-1][1] == "/goal-service/goal/user/effective/floors-climbed/2026-10-16"
    assert calls[-1][2]["json"] == {"goalValue": 12, "goalType": "FLOORS_CLIMBED"}
    g.set_daily_goal("WEIGHT_GRAMS", 80000, "2026-10-01")
    assert calls[-1][1] == "/goal-service/goal/user/effective/WEIGHT_GRAMS/2026-10-01"
    with pytest.raises(ValueError):
        g.set_daily_goal("FLOORS_CLIMBED", 0)


def test_zone_updates_send_lists(writer):
    g, calls = writer
    zones = [{"sport": "RUNNING", "trainingMethod": "HR_MAX", "zone1Floor": 100}]
    g.update_heart_rate_zones(zones)
    assert calls[-1][:2] == ("PUT", "/biometric-service/heartRateZones/")
    assert calls[-1][2]["json"] == zones
    g.update_power_zones([{"sport": "CYCLING", "functionalThresholdPower": 250}])
    assert calls[-1][1] == "/biometric-service/powerZones/all"
    with pytest.raises(ValueError):
        g.update_heart_rate_zones([])


def test_user_settings_merge_and_primary_device(writer, monkeypatch):
    g, calls = writer
    current = {
        "id": 1,
        "userData": {"measurementSystem": "statute_us", "handedness": "RIGHT"},
    }
    monkeypatch.setattr(g, "connectapi", lambda path, **kw: dict(current))
    g.update_user_settings({"userData": {"measurementSystem": "metric"}})
    sent = calls[-1][2]["json"]
    assert sent["userData"] == {"measurementSystem": "metric", "handedness": "RIGHT"}
    assert sent["id"] == 1
    g.set_primary_training_device("123")
    assert calls[-1][1].endswith("/user-settings/primary-training-device")
    assert calls[-1][2]["json"] == {"deviceId": 123}


def test_calendar_notes(writer):
    g, calls = writer
    g.create_calendar_note("2026-10-20", "Race day", "Easy jog in the morning")
    assert calls[-1][:2] == ("POST", "/calendar-service/note")
    assert calls[-1][2]["json"] == {
        "noteName": "Race day",
        "content": "Easy jog in the morning",
        "date": "2026-10-20",
    }
    g.update_calendar_note(55, "2026-10-21", "Race day")
    assert calls[-1][:2] == ("PUT", "/calendar-service/note/55")
    assert calls[-1][2]["json"]["id"] == 55
    g.delete_calendar_note(55)
    assert calls[-1][:2] == ("DELETE", "/calendar-service/note/55")
    with pytest.raises(ValueError):
        g.create_calendar_note("2026-10-20", "")


def test_update_activity_partial_put(writer):
    g, calls = writer
    g.update_activity(
        987654, {"activityName": "Evening run", "metadataDTO": {"favorite": True}}
    )
    method, url, kw = calls[-1]
    assert (method, url) == ("PUT", "/activity-service/activity/987654")
    assert kw["json"] == {
        "activityName": "Evening run",
        "metadataDTO": {"favorite": True},
        "activityId": "987654",
    }
    with pytest.raises(ValueError):
        g.update_activity(987654, {})


def test_lifestyle_reads(api):
    g, rec = api
    g.get_lifestyle_behaviors("2026-10-16")
    assert _last(rec)[1] == "/lifestylelogging-service/behaviours/2026-10-16"
    g.get_lifestyle_auto_logging()
    assert _last(rec)[1] == "/userpreference-service/lifestyle.auto-logging.enabled"
    with pytest.raises(ValueError):
        g.get_lifestyle_behaviors("16-10-2026")


def test_set_tracked_lifestyle_behaviors(writer, monkeypatch):
    g, calls = writer
    g.set_tracked_lifestyle_behaviors([3, "7"])
    assert calls[-1][:2] == ("PUT", "/lifestylelogging-service/trackedBehaviours")
    assert calls[-1][2]["json"] == {
        "trackedBehaviours": [3, 7],
        "calendarDate": "2026-10-16",
        "userProfilePk": 4242,
    }
    g.set_tracked_lifestyle_behaviors([], "2026-10-01")
    assert calls[-1][2]["json"]["trackedBehaviours"] == []
    assert calls[-1][2]["json"]["calendarDate"] == "2026-10-01"
    sent = len(calls)
    for bad in ("3", [0], [1.5], [True]):
        with pytest.raises(ValueError):
            g.set_tracked_lifestyle_behaviors(bad)
    g.profile_id = None
    monkeypatch.setattr(g, "_load_social_profile", lambda: None)
    with pytest.raises(GarminConnectConnectionError):
        g.set_tracked_lifestyle_behaviors([3])
    assert len(calls) == sent


def test_log_lifestyle_behavior(writer, monkeypatch):
    g, calls = writer
    monkeypatch.setattr(g, "now", lambda: datetime(2026, 10, 16, 8, 5, 9, 678900))
    g.log_lifestyle_behavior(12, "2026-10-16")
    assert calls[-1][:2] == ("PUT", "/lifestylelogging-service/dailyLog/12/2026-10-16")
    assert calls[-1][2]["json"] == {
        "logStatus": "YES",
        "updateTimestamp": "2026-10-16T08:05:09.6",
    }
    g.log_lifestyle_behavior(12, "2026-10-16", logged=False)
    assert calls[-1][2]["json"]["logStatus"] == "NO"
    assert "details" not in calls[-1][2]["json"]
    g.log_lifestyle_behavior(12, "2026-10-16", amount=3)
    assert calls[-1][2]["json"]["details"] == [{"amount": 3}]
    g.log_lifestyle_behavior(1, "2026-10-16", sub_type_amounts={1: 2, 3: 25})
    assert calls[-1][2]["json"]["details"] == [
        {"subTypeId": 1, "subTypeName": "", "amount": 2},
        {"subTypeId": 3, "subTypeName": "", "amount": 25},
    ]
    sent = len(calls)
    bad_calls = [
        {"amount": 0},
        {"amount": 26},
        {"amount": 2.0},
        {"amount": True},
        {"sub_type_amounts": {1: 100}},
        {"sub_type_amounts": {}},
        {"sub_type_amounts": {0: 1}},
        {"amount": 1, "sub_type_amounts": {1: 1}},
        {"logged": False, "amount": 1},
        {"logged": "yes"},
    ]
    for kwargs in bad_calls:
        with pytest.raises(ValueError):
            g.log_lifestyle_behavior(12, "2026-10-16", **kwargs)
    with pytest.raises(ValueError):
        g.log_lifestyle_behavior(12, "2026/10/16")
    assert len(calls) == sent


def test_clear_lifestyle_behavior_log(writer):
    g, calls = writer
    assert g.clear_lifestyle_behavior_log(12, "2026-10-16") is None
    assert calls[-1][:2] == (
        "DELETE",
        "/lifestylelogging-service/dailyLog/12/2026-10-16",
    )
    with pytest.raises(ValueError):
        g.clear_lifestyle_behavior_log(-1, "2026-10-16")
    assert len(calls) == 1


def test_create_lifestyle_behaviors(writer):
    g, calls = writer
    g.create_lifestyle_behaviors(
        [
            {"name": "Stretching"},
            {"name": "Glasses", "measurement_type": "QUANTITY", "sleep_related": True},
        ]
    )
    assert calls[-1][:2] == ("POST", "/lifestylelogging-service/behaviours/batch")
    assert calls[-1][2]["json"] == [
        {"name": "Stretching", "measurementType": "NONE", "sleepRelated": False},
        {"name": "Glasses", "measurementType": "QUANTITY", "sleepRelated": True},
    ]
    bad_inputs = [
        [],
        {"name": "x"},
        ["x"],
        [{"name": " "}],
        [{"name": "x", "measurement_type": "COUNT"}],
        [{"name": "x", "sleep_related": "no"}],
        [{"name": "x", "measurementType": "NONE"}],
    ]
    for bad in bad_inputs:
        with pytest.raises(ValueError):
            g.create_lifestyle_behaviors(bad)
    assert len(calls) == 1


_BEHAVIORS = [
    {
        "behaviourId": 101,
        "name": "Stretching",
        "category": "CUSTOM",
        "measurementType": "NONE",
        "sleepRelated": False,
    },
    {
        "behaviourId": 1,
        "name": "Alcohol",
        "category": "LIFESTYLE",
        "measurementType": "QUANTITY",
        "sleepRelated": False,
    },
]


def test_update_lifestyle_behavior(writer, monkeypatch):
    g, calls = writer
    reads = []

    def fake_get(path, **kw):
        reads.append(path)
        return [dict(b) for b in _BEHAVIORS]

    monkeypatch.setattr(g, "connectapi", fake_get)
    g.update_lifestyle_behavior("101", name="Yoga")
    assert reads == ["/lifestylelogging-service/behaviours/2026-10-16"]
    assert calls[-1][:2] == ("PUT", "/lifestylelogging-service/behaviours/101")
    assert calls[-1][2]["json"] == {
        "name": "Yoga",
        "measurementType": "NONE",
        "sleepRelated": False,
    }
    g.update_lifestyle_behavior(101, measurement_type="QUANTITY", sleep_related=True)
    assert calls[-1][2]["json"] == {
        "name": "Stretching",
        "measurementType": "QUANTITY",
        "sleepRelated": True,
    }
    sent = len(calls)
    with pytest.raises(ValueError):
        g.update_lifestyle_behavior(101)
    with pytest.raises(ValueError, match="not found"):
        g.update_lifestyle_behavior(999, name="x")
    with pytest.raises(ValueError, match="not a custom"):
        g.update_lifestyle_behavior(1, name="x")
    with pytest.raises(ValueError):
        g.update_lifestyle_behavior(101, measurement_type="COUNT")
    assert len(calls) == sent


def test_delete_lifestyle_behaviors(writer):
    g, calls = writer
    g.delete_lifestyle_behaviors([101, "102"])
    assert calls[-1][:2] == (
        "DELETE",
        "/lifestylelogging-service/behaviours/batchDelete",
    )
    assert calls[-1][2]["json"] == {"ids": [101, 102]}
    for bad in ([], 101, ["x"]):
        with pytest.raises(ValueError):
            g.delete_lifestyle_behaviors(bad)
    assert len(calls) == 1


def test_set_lifestyle_auto_logging(writer):
    g, calls = writer
    g.set_lifestyle_auto_logging(True)
    assert calls[-1][:2] == (
        "PUT",
        "/userpreference-service/lifestyle.auto-logging.enabled",
    )
    assert calls[-1][2]["json"] == {
        "key": "lifestyle.auto-logging.enabled",
        "value": "true",
    }
    g.set_lifestyle_auto_logging(False)
    assert calls[-1][2]["json"]["value"] == "false"
    with pytest.raises(ValueError):
        g.set_lifestyle_auto_logging("true")
    assert len(calls) == 2
