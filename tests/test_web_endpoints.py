"""Request-shape tests for the endpoints mapped from the Garmin Connect web app.

These tests stub the client so no network access happens; they pin the exact
path and query parameters each method sends, which is what matters for
compatibility with Garmin's undocumented API.
"""

from datetime import date

import pytest

from garminconnect import Garmin


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


def test_consent_feature_path_and_rejected_types(api):
    g, rec = api
    g.get_consent_feature("insights")
    assert _last(rec)[1] == "/gdprconsent-service/feature/INSIGHTS"
    g.get_upload_consent()
    assert _last(rec)[1] == "/gdprconsent-service/feature/UPLOAD"
    for bad in ("DEVICE_UPLOAD", "../UPLOAD", "UPLOAD/x", "", None):
        with pytest.raises(ValueError):
            g.get_consent_feature(bad)


@pytest.mark.parametrize(
    ("option", "source", "granted"),
    [("opt-in", "CUSTOMER", True), ("opt-out", "DEFAULT", False)],
)
def test_has_upload_consent_needs_opt_in(api, option, source, granted):
    g, rec = api
    rec.result = {
        "featureType": "UPLOAD",
        "userOption": option,
        "sourceType": source,
        "consentTypeId": "DI_CONNECT_UPLOAD",
        "consentEnforcementState": "HARD",
        "deleteStatus": "NOT_APPLICABLE",
    }
    assert g.has_upload_consent() is granted


def test_consent_text_url_params_and_rejected_inputs(api):
    g, rec = api
    g.get_consent_text()
    _, path, kw = _last(rec)
    assert path == "https://connect.garmin.com/consentTextServices/consentText"
    assert kw["params"] == {"consentTypeId": "DI_CONNECT_UPLOAD", "locale": "en-US"}
    g.get_consent_text("di_connect_upload", "nl-NL")
    assert _last(rec)[2]["params"] == {
        "consentTypeId": "DI_CONNECT_UPLOAD",
        "locale": "nl-NL",
    }
    sent = len(rec.calls)
    for bad_type in ("UPLOAD", "DI_CONNECT_UPLOAD/x", "", None):
        with pytest.raises(ValueError):
            g.get_consent_text(bad_type)
    for bad_locale in ("en", "en_US", "EN-us", "en-US&x=1", "../en-US", "", None):
        with pytest.raises(ValueError):
            g.get_consent_text(locale=bad_locale)
    assert len(rec.calls) == sent


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


@pytest.fixture
def consent_writer(writer, monkeypatch):
    """``writer`` whose consent text read returns version 73."""
    g, calls = writer
    reads: list[tuple[str, dict]] = []

    def fake_read(path, **kw):
        reads.append((path, kw))
        return [{"consentTypeId": "DI_CONNECT_UPLOAD", "version": "73"}]

    monkeypatch.setattr(g, "connectapi", fake_read)
    return g, calls, reads


def test_grant_upload_consent_posts_exact_body(consent_writer):
    g, calls, reads = consent_writer
    g.grant_upload_consent()
    method, url, kw = calls[-1]
    assert (method, url) == ("POST", "/gdprconsent-service/consent/grant")
    assert kw["json"] == {
        "consentTypeId": "DI_CONNECT_UPLOAD",
        "consentLocale": "en-US",
        "consentVersion": "73",
    }
    assert reads[-1][1]["params"]["locale"] == "en-US"
    reads.clear()
    g.grant_upload_consent(74, locale="nl-NL")
    assert calls[-1][2]["json"] == {
        "consentTypeId": "DI_CONNECT_UPLOAD",
        "consentLocale": "nl-NL",
        "consentVersion": "74",
    }
    assert reads == []  # an explicit version skips the consent text read


def test_decline_upload_consent_posts_exact_body_and_never_deletes(consent_writer):
    g, calls, _ = consent_writer
    g.decline_upload_consent()
    method, url, kw = calls[-1]
    assert (method, url) == (
        "POST",
        "/gdprconsent-service/consent/revoke/DI_CONNECT_UPLOAD/false",
    )
    assert kw["json"] == {
        "consentTypeId": "DI_CONNECT_UPLOAD",
        "clientIndicator": False,
        "consentLocale": "en-US",
        "consentVersion": "73",
    }
    g.decline_upload_consent("75", locale="de-DE")
    assert calls[-1][1].endswith("/DI_CONNECT_UPLOAD/false")
    assert calls[-1][2]["json"]["consentVersion"] == "75"
    assert calls[-1][2]["json"]["clientIndicator"] is False


@pytest.mark.parametrize("method", ["grant_upload_consent", "decline_upload_consent"])
@pytest.mark.parametrize(
    "texts", [[], [{}], [{"version": ""}], [{"version": None}], {"version": "73"}]
)
def test_upload_consent_needs_a_version_from_the_text(
    writer, monkeypatch, method, texts
):
    from garminconnect.exceptions import GarminConnectConnectionError

    g, calls = writer
    monkeypatch.setattr(g, "connectapi", lambda path, **kw: texts)
    with pytest.raises(GarminConnectConnectionError):
        getattr(g, method)()
    assert calls == []


@pytest.mark.parametrize("method", ["grant_upload_consent", "decline_upload_consent"])
def test_upload_consent_rejects_bad_inputs_without_sending(consent_writer, method):
    g, calls, reads = consent_writer
    for bad_version in (0, -1, "", "  ", "7a", "1.5", 1.5, True):
        with pytest.raises(ValueError):
            getattr(g, method)(bad_version)
    for bad_locale in ("en", "en_US", "en-US/x", "", None):
        with pytest.raises(ValueError):
            getattr(g, method)("73", locale=bad_locale)
    assert calls == [] and reads == []
