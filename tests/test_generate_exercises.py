"""Regression tests for scripts/generate_exercises.py."""

import importlib.util
import json
import types
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def gen():
    spec = importlib.util.spec_from_file_location(
        "generate_exercises",
        Path(__file__).parent.parent / "scripts" / "generate_exercises.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PAGE = """<html><body>
  <div class="user-header">
    <span>user@example.com</span>
  </div>
  <ul id="picker">
    <li role="option" data-category-key="SQUAT" data-exercise-key="BACK_SQUAT"><span>Back Squat</span></li>
    <li role="option" data-category-key="LEAK" data-exercise-key="NO_SPAN_HERE"></li>
  </ul>
  <footer><span>JWT_WEB=eyJhbGciOi.FAKE.TOKEN</span></footer>
</body></html>"""


def test_parse_does_not_capture_span_outside_element(gen):
    rows = gen.parse(PAGE)
    assert rows == [
        {"name": "Back Squat", "category": "SQUAT", "exercise": "BACK_SQUAT"}
    ]


def test_parse_keeps_unclosed_items_separate(gen):
    # No </li> after the first item: the next <li> start tag is the
    # boundary, so the attribute-only item must not merge with (or borrow
    # the span of) the following one.
    rows = gen.parse(
        '<li data-category-key="LEAK" data-exercise-key="NO_SPAN">\n'
        '<li data-category-key="X" data-exercise-key="Y">'
        "<span>user@example.com</span></li>"
    )
    assert rows == [{"name": "user@example.com", "category": "X", "exercise": "Y"}]


def test_main_refuses_suspect_names(gen, tmp_path, monkeypatch, capsys):
    html_file = tmp_path / "picker.html"
    html_file.write_text(
        '<li data-category-key="X" data-exercise-key="Y">'
        "<span>user@example.com</span></li>"
    )
    monkeypatch.setattr("sys.argv", ["generate_exercises.py", str(html_file)])
    with pytest.raises(SystemExit) as exc_info:
        gen.main()
    # The refusal must name positions only, never the suspected text.
    assert "user@example.com" not in str(exc_info.value)
    assert "session data" in str(exc_info.value)


@pytest.mark.parametrize(
    "label",
    [
        "see HTTPS://EXAMPLE.COM/x",  # uppercase URL scheme
        "id 9F8E7D6C-5B4A-4C3D-8E9F-0A1B2C3D4E5F",  # uppercase GUID
    ],
)
def test_suspect_regex_is_case_insensitive(gen, label):
    assert gen.SUSPECT.search(label)


def test_parse_accumulates_text_across_nested_tags(gen):
    # A nested <b> inside the span splits the label into two handle_data
    # calls; the "https://" prefix must not be dropped, or the SUSPECT
    # filter never sees the full (sensitive) label.
    rows = gen.parse(
        '<li data-category-key="X" data-exercise-key="Y">'
        "<span>https://host/<b>?ticket=secret</b></span></li>"
    )
    assert rows == [
        {
            "name": "https://host/?ticket=secret",
            "category": "X",
            "exercise": "Y",
        }
    ]
    assert gen.SUSPECT.search(rows[0]["name"])


def test_parse_flushes_final_item_without_closing_tag(gen):
    # HTMLParser.close() does not synthesize a missing </li>; the last
    # item in the source HTML must still be captured.
    rows = gen.parse(
        '<ul><li data-category-key="SQUAT" data-exercise-key="BACK_SQUAT">'
        "<span>Back Squat</span>"
    )
    assert rows == [
        {"name": "Back Squat", "category": "SQUAT", "exercise": "BACK_SQUAT"}
    ]


def test_parse_keeps_capturing_after_nested_span_closes(gen):
    # A plain in/out flag drops back "out of span" on the inner </span>,
    # losing text that follows it but is still inside the outer span.
    rows = gen.parse(
        '<li data-category-key="SQUAT" data-exercise-key="BACK_SQUAT">'
        "<span><span>Back</span> Squat</span></li>"
    )
    assert rows == [
        {"name": "Back Squat", "category": "SQUAT", "exercise": "BACK_SQUAT"}
    ]


# --------------------------------------------------------------------------
# --fetch: properties parsing and candidate extraction
# --------------------------------------------------------------------------

PROPS = """\ufeff# Garmin exercise translations
! another comment
category_type_BENCH_PRESS=Bench Press
category_type_CALF_RAISE = Calf Raise
category_type_HIP_RAISE:Hip Raise
category_type_HIP=Hip
exercise_type_PUSH_UP=Push-up
muscle_CHEST=Chest
primary_muscles=Primary muscles
BENCH_PRESS_BARBELL_BENCH_PRESS=Barbell bench press
HIP_RAISE_45_DEGREE_CABLE_KICKBACK=45 degree cable kickback
HIP_SOMETHING=Hip thing
CALF_RAISE__3_WAY_CALF_RAISE=3-way calf raise
POSE_DOWNWARD_DOG=Downward dog
RUN_INDOOR_TREADMILL=Treadmill \\
    run
NOT_A_CATEGORY_THING=Nope
BENCH_PRESS_ESCAPED=Pli\\u00e9 press
"""


def test_parse_properties_handles_separators_escapes_and_continuations(gen):
    props = gen.parse_properties(PROPS)
    assert props["category_type_CALF_RAISE"] == "Calf Raise"
    assert props["category_type_HIP_RAISE"] == "Hip Raise"
    assert props["RUN_INDOOR_TREADMILL"] == "Treadmill run"
    assert props["BENCH_PRESS_ESCAPED"] == "Pli\u00e9 press"
    assert not any(k.startswith(("#", "!", "\ufeff")) for k in props)


def test_candidate_categories_merge_file_and_extras(gen):
    cats = gen.candidate_categories(gen.parse_properties(PROPS))
    assert {"BENCH_PRESS", "CALF_RAISE", "HIP_RAISE", "HIP"} <= set(cats)
    assert {"POSE", "MOVE", "INDOOR_ROW", "RUN", "RUN_INDOOR"} <= set(cats)
    assert len(cats) == len(set(cats))


def test_split_key_uses_longest_prefix(gen):
    cats = ["HIP", "HIP_RAISE", "RUN", "RUN_INDOOR"]
    assert gen.split_key("HIP_RAISE_45_DEGREE_CABLE_KICKBACK", cats) == (
        "HIP_RAISE",
        "45_DEGREE_CABLE_KICKBACK",
    )
    assert gen.split_key("HIP_SOMETHING", cats) == ("HIP", "SOMETHING")
    assert gen.split_key("RUN_INDOOR_TREADMILL", cats) == ("RUN_INDOOR", "TREADMILL")
    # The bare category (no name after it) is not an exercise key.
    assert gen.split_key("HIP_RAISE", ["HIP_RAISE"]) is None
    assert gen.split_key("SQUAT_BACK_SQUAT", cats) is None


def test_exercise_candidates(gen):
    props = gen.parse_properties(PROPS)
    rows = gen.exercise_candidates(props, gen.candidate_categories(props))
    pairs = {(r["category"], r["exercise"]): r["label"] for r in rows}
    assert pairs[("CALF_RAISE", "_3_WAY_CALF_RAISE")] == "3-way calf raise"
    assert ("HIP_RAISE", "45_DEGREE_CABLE_KICKBACK") in pairs
    assert ("HIP", "SOMETHING") in pairs
    assert ("POSE", "DOWNWARD_DOG") in pairs
    assert ("RUN_INDOOR", "TREADMILL") in pairs
    # Lower-case keys and unknown prefixes are not exercises.
    assert all(r["category"] != "NOT" for r in rows)
    assert not any("CHEST" in r["exercise"] for r in rows)
    assert not any(r["exercise"] == "PUSH_UP" for r in rows)
    summary = gen.summarize(props)
    assert summary["candidates"] == len(rows)
    assert summary["unmatched_exercise_keys"] == 1
    assert summary["generic_exercise_types"] == 1


def test_fetch_prints_counts_without_writing(gen, tmp_path, capsys):
    src = tmp_path / "exercise_types.properties"
    src.write_text(PROPS, encoding="utf-8")
    out = tmp_path / "exercises.py"
    gen.main(["--fetch", "--properties", str(src), "--out", str(out)])
    assert "candidates: 7" in capsys.readouterr().out
    assert not out.exists()


def test_fetch_downloads_the_public_file(gen, monkeypatch, capsys):
    seen = []

    def fake_download(url, timeout=30.0):
        seen.append(url)
        return PROPS

    monkeypatch.setattr(gen, "download", fake_download)
    gen.main(["--fetch"])
    assert seen == [gen.EXERCISE_TYPES_URL]
    assert "candidate categories" in capsys.readouterr().out


# --------------------------------------------------------------------------
# --verify: live checks against a fake API
# --------------------------------------------------------------------------


class _Resp:
    def __init__(self, status_code):
        self.status_code = status_code


def _http_error(status):
    from garminconnect.exceptions import GarminConnectConnectionError

    exc = GarminConnectConnectionError(f"API client error ({status}): boom")
    exc.response = _Resp(status)
    return exc


class FakeApi:
    """Mimics Garmin: bad categories -> 400, unknown names stored as ""."""

    def __init__(self, categories, names, rate_limit=0, fail_on_upload=None):
        self.categories = set(categories)
        self.names = names  # {category: {valid names}}
        self.workouts = {}
        self.next_id = 100
        self.uploads = []
        self.deleted = []
        self.rate_limit = rate_limit
        self.fail_on_upload = fail_on_upload

    def upload_workout(self, payload):
        if self.rate_limit:
            from garminconnect.exceptions import GarminConnectTooManyRequestsError

            self.rate_limit -= 1
            raise GarminConnectTooManyRequestsError("Rate limit exceeded")
        self.uploads.append(payload)
        if self.fail_on_upload is not None and len(self.uploads) == self.fail_on_upload:
            raise RuntimeError("network down")
        steps = payload["workoutSegments"][0]["workoutSteps"]
        if any(s["category"] not in self.categories for s in steps):
            raise _http_error(400)
        stored = []
        for s in steps:
            ok = s["exerciseName"] in self.names.get(s["category"], set())
            stored.append({**s, "exerciseName": s["exerciseName"] if ok else ""})
        self.next_id += 1
        self.workouts[self.next_id] = {
            "workoutId": self.next_id,
            "workoutSegments": [{"workoutSteps": stored}],
        }
        return {"workoutId": self.next_id}

    def get_workout_by_id(self, workout_id):
        return self.workouts[workout_id]

    def delete_workout(self, workout_id):
        self.deleted.append(workout_id)
        del self.workouts[workout_id]


def _verifier(gen, api, **kw):
    kw.setdefault("sleep", lambda s: None)
    kw.setdefault("log", lambda m: None)
    return gen.Verifier(api, **kw)


def _cands(*pairs):
    return [{"category": c, "exercise": e, "label": e.lower()} for c, e in pairs]


def test_verify_drops_rejected_categories_and_blanked_names(gen):
    api = FakeApi({"SQUAT", "PLANK"}, {"SQUAT": {"BACK_SQUAT"}})
    v = _verifier(gen, api)
    valid, rows = v.run(
        ["SQUAT", "PLANK", "BOGUS"],
        _cands(("SQUAT", "BACK_SQUAT"), ("SQUAT", "FAKE"), ("BOGUS", "THING")),
    )
    assert valid == ["SQUAT", "PLANK"]
    assert rows == [
        {"category": "SQUAT", "exercise": "BACK_SQUAT", "label": "back_squat"}
    ]
    # Every created workout was deleted again.
    assert api.workouts == {}
    assert v.created == v.deleted == len(api.deleted)


def test_verify_resplits_keys_of_rejected_longer_category(gen):
    api = FakeApi({"HIP"}, {"HIP": {"RAISE_KICK"}})
    _, rows = _verifier(gen, api).run(
        ["HIP", "HIP_RAISE"], _cands(("HIP_RAISE", "KICK"))
    )
    assert [(r["category"], r["exercise"]) for r in rows] == [("HIP", "RAISE_KICK")]


def test_verify_uploads_names_in_batches(gen):
    names = [f"N{i:03d}" for i in range(120)]
    api = FakeApi({"CORE"}, {"CORE": set(names[::2])})
    _, rows = _verifier(gen, api, batch_size=50).run(
        ["CORE"], _cands(*(("CORE", n) for n in names))
    )
    name_uploads = [u["workoutSegments"][0]["workoutSteps"] for u in api.uploads[1:]]
    assert [len(s) for s in name_uploads] == [50, 50, 20]
    for steps in name_uploads:
        assert [s["stepOrder"] for s in steps] == list(range(1, len(steps) + 1))
    assert len(rows) == 60
    # The payload has the shape of a strength workout.
    first = api.uploads[0]
    assert first["sportType"]["sportTypeKey"] == "strength_training"
    step = first["workoutSegments"][0]["workoutSteps"][0]
    assert step["type"] == "ExecutableStepDTO"
    assert step["endCondition"]["conditionTypeKey"] == "reps"


def test_verify_bisects_a_rejected_batch(gen):
    class PickyApi(FakeApi):
        def upload_workout(self, payload):
            steps = payload["workoutSegments"][0]["workoutSteps"]
            if any(s["exerciseName"] == "POISON" for s in steps):
                self.uploads.append(payload)
                raise _http_error(400)
            return super().upload_workout(payload)

    api = PickyApi({"CORE"}, {"CORE": {"A", "B", "C"}})
    _, rows = _verifier(gen, api).run(
        ["CORE"],
        _cands(("CORE", "A"), ("CORE", "POISON"), ("CORE", "B"), ("CORE", "C")),
    )
    assert sorted(r["exercise"] for r in rows) == ["A", "B", "C"]
    assert api.workouts == {}


def test_verify_deletes_workouts_even_when_a_step_fails(gen):
    class BrokenReadApi(FakeApi):
        def get_workout_by_id(self, workout_id):
            raise RuntimeError("read failed")

    api = BrokenReadApi({"CORE"}, {})
    logs = []
    v = _verifier(gen, api, log=logs.append)
    with pytest.raises(RuntimeError, match="read failed"):
        v.run(["CORE"], [])
    assert api.workouts == {}
    assert any("Deletion summary" in m and "failed 0" in m for m in logs)


def test_verify_retries_failed_deletes_in_cleanup(gen):
    class FlakyDeleteApi(FakeApi):
        fails = 1

        def delete_workout(self, workout_id):
            if self.fails:
                self.fails -= 1
                raise RuntimeError("delete failed")
            super().delete_workout(workout_id)

    api = FlakyDeleteApi({"CORE"}, {})
    v = _verifier(gen, api)
    v.run(["CORE"], [])
    assert api.workouts == {}
    assert v.pending == []


def test_verify_cleans_up_after_an_aborted_upload(gen):
    api = FakeApi({"A", "B"}, {}, fail_on_upload=2)
    v = _verifier(gen, api)
    with pytest.raises(RuntimeError, match="network down"):
        v.run(["A", "B"], [])
    assert api.workouts == {}


def test_verify_waits_and_retries_on_429(gen):
    api = FakeApi({"CORE"}, {}, rate_limit=2)
    waits = []
    v = _verifier(gen, api, sleep=waits.append, delay=0.5, backoff=10)
    valid, _ = v.run(["CORE"], [])
    assert valid == ["CORE"]
    assert waits == [0.5, 10, 20]


def test_verify_gives_up_after_repeated_429(gen):
    from garminconnect.exceptions import GarminConnectTooManyRequestsError

    api = FakeApi({"CORE"}, {}, rate_limit=99)
    v = _verifier(gen, api, max_retries=2)
    with pytest.raises(GarminConnectTooManyRequestsError):
        v.run(["CORE"], [])


def test_verify_resumes_from_cache(gen, tmp_path):
    cache = tmp_path / "cache.json"
    api = FakeApi({"CORE"}, {"CORE": {"A"}})
    _verifier(gen, api, cache_path=cache).run(["CORE", "BAD"], _cands(("CORE", "A")))
    assert len(api.uploads) == 3

    # A second run re-tests nothing and returns the same result.
    api2 = FakeApi(set(), {})
    valid, rows = _verifier(gen, api2, cache_path=cache).run(
        ["CORE", "BAD"], _cands(("CORE", "A"), ("CORE", "B"))
    )
    assert valid == ["CORE"]
    # Only the new name is uploaded; CORE is rejected by api2, so it
    # fails, but the cached result for A still stands.
    assert [r["exercise"] for r in rows] == ["A"]
    assert len(api2.uploads) == 1


def test_verify_refuses_without_confirmation(gen, tmp_path, monkeypatch):
    src = tmp_path / "p.properties"
    src.write_text(PROPS, encoding="utf-8")
    import garminconnect

    def no_login(*a, **k):
        raise AssertionError("must not log in")

    monkeypatch.setattr(garminconnect, "Garmin", no_login)
    with pytest.raises(SystemExit) as exc_info:
        gen.main(["--verify", "--tokens", "x", "--properties", str(src)])
    assert "--i-understand-this-writes" in str(exc_info.value)
    with pytest.raises(SystemExit) as exc_info:
        gen.main(["--verify", "--properties", str(src), "--i-understand-this-writes"])
    assert "--tokens" in str(exc_info.value)


def test_verify_main_writes_catalog(gen, tmp_path, monkeypatch, capsys):
    src = tmp_path / "p.properties"
    src.write_text(
        "category_type_PULL_UP=Pull Up\n"
        "PULL_UP_LAT_PULLDOWN=Lat pulldown\n"
        "PULL_UP_NEW_ONE=New one\n"
        "PULL_UP_FAKE=Fake\n",
        encoding="utf-8",
    )
    out = tmp_path / "exercises.py"
    out.write_text(
        "_RAW: list[tuple[str, str, str]] = [\n"
        '    ("Lat Pull-down", "PULL_UP", "LAT_PULLDOWN"),\n'
        '    ("Gone", "PULL_UP", "GONE"),\n'
        "]\n",
        encoding="utf-8",
    )
    api = FakeApi({"PULL_UP"}, {"PULL_UP": {"LAT_PULLDOWN", "NEW_ONE"}})
    api.login = lambda tokens: None
    import garminconnect

    monkeypatch.setattr(garminconnect, "Garmin", lambda: api)
    gen.main(
        [
            "--verify",
            "--tokens",
            "tok",
            "--i-understand-this-writes",
            "--properties",
            str(src),
            "--delay",
            "0",
            "--out",
            str(out),
        ]
    )
    printed = capsys.readouterr().out
    assert "PULL_UP/GONE" in printed
    assert "Deletion summary" in printed
    mod = _load_rendered(out.read_text(encoding="utf-8"))
    # The old name is kept, so existing lookups still work.
    assert mod.resolve("Lat Pull-down")["exercise"] == "LAT_PULLDOWN"
    assert mod.resolve("Lat pulldown")["exercise"] == "LAT_PULLDOWN"
    assert mod.resolve("New one")["category"] == "PULL_UP"
    assert mod.resolve("Fake") is None
    assert "PULL_UP" in mod.CATEGORIES
    assert "--verify" in mod.__doc__
    assert api.workouts == {}


# --------------------------------------------------------------------------
# --metadata
# --------------------------------------------------------------------------


def _write_metadata(d):
    (d / "Exercises.json").write_text(
        json.dumps(
            {
                "categories": {
                    "SQUAT": {
                        "exercises": {
                            "BACK_SQUAT": {
                                "primaryMuscles": ["QUADS"],
                                "secondaryMuscles": ["GLUTES"],
                            }
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    (d / "exerciseToEquipments.json").write_text(
        json.dumps(
            [
                {
                    "exerciseCategoryKey": "SQUAT",
                    "exercisesInCategory": [
                        {
                            "exerciseKey": "BACK_SQUAT",
                            "primaryMuscles": ["OLD"],
                            "secondaryMuscles": [],
                            "equipmentKeys": ["BARBELL"],
                        },
                        {
                            "exerciseKey": "GOBLET_SQUAT",
                            "primaryMuscles": ["QUADS"],
                            "secondaryMuscles": ["CORE"],
                            "equipmentKeys": ["DUMBBELL"],
                        },
                    ],
                }
            ]
        ),
        encoding="utf-8",
    )


def test_load_and_attach_metadata(gen, tmp_path):
    _write_metadata(tmp_path)
    meta = gen.load_metadata(tmp_path)
    assert meta[("SQUAT", "BACK_SQUAT")] == {
        "primary_muscles": ["QUADS"],
        "secondary_muscles": ["GLUTES"],
        "equipment": ["BARBELL"],
    }
    rows = [
        {"name": "Back Squat", "category": "SQUAT", "exercise": "BACK_SQUAT"},
        {"name": "Air Squat", "category": "SQUAT", "exercise": "AIR_SQUAT"},
    ]
    assert gen.attach_metadata(rows, meta) == 1
    assert rows[1]["primary_muscles"] == rows[1]["equipment"] == []


def test_load_metadata_accepts_either_file(gen, tmp_path):
    _write_metadata(tmp_path)
    (tmp_path / "Exercises.json").unlink()
    meta = gen.load_metadata(tmp_path)
    assert meta[("SQUAT", "BACK_SQUAT")]["primary_muscles"] == ["OLD"]
    with pytest.raises(FileNotFoundError):
        gen.load_metadata(tmp_path / "missing")


def test_equipment_labels(gen):
    props = {"equipment_type_BARBELL": "Barbell", "DUMBBELL": "Dumbbell"}
    assert gen.equipment_labels(["BARBELL", "DUMBBELL", "NONE"], props) == {
        "BARBELL": "Barbell",
        "DUMBBELL": "Dumbbell",
    }


def test_suspect_metadata_is_refused(gen):
    rows = [
        {
            "name": "Squat",
            "category": "SQUAT",
            "exercise": "SQUAT",
            "primary_muscles": ["user@example.com"],
        }
    ]
    assert gen.suspect_rows(rows) == [0]


# --------------------------------------------------------------------------
# Rendering keeps the public API
# --------------------------------------------------------------------------


def _load_rendered(source):
    module = types.ModuleType("rendered_exercises")
    exec(compile(source, "rendered_exercises", "exec"), module.__dict__)  # noqa: S102
    return module


def test_render_keeps_public_api_and_adds_metadata(gen):
    rows = [
        {
            "name": "Back Squat",
            "category": "SQUAT",
            "exercise": "BACK_SQUAT",
            "display_name": "Barbell back squat",
            "primary_muscles": ["QUADS"],
            "secondary_muscles": ["GLUTES"],
            "equipment": ["BARBELL"],
        },
        {"name": "Plank", "category": "PLANK", "exercise": "PLANK"},
    ]
    mod = _load_rendered(
        gen.render(rows, categories=["RUN"], equipment_names={"BARBELL": "Barbell"})
    )
    assert mod.EXERCISES[0]["name"] == "Back Squat"
    assert set(mod.EXERCISES[0]) >= {"name", "category", "exercise"}
    assert mod.BY_NAME["Plank"]["category"] == "PLANK"
    assert mod.CATEGORIES == ["PLANK", "RUN", "SQUAT"]
    assert mod.resolve("Back Squat")["exercise"] == "BACK_SQUAT"
    assert mod.resolve("Barbell back squat")["exercise"] == "BACK_SQUAT"
    assert mod.resolve("nope") is None
    assert [e["name"] for e in mod.find("squat")] == ["Back Squat"]
    assert [e["name"] for e in mod.find("barbell")] == ["Back Squat"]
    plank = mod.resolve("Plank")
    assert plank["display_name"] == "Plank"
    assert plank["primary_muscles"] == plank["secondary_muscles"] == []
    assert plank["equipment"] == []
    assert [e["name"] for e in mod.by_muscle("glutes")] == ["Back Squat"]
    assert mod.by_muscle("GLUTES", primary_only=True) == []
    assert [e["name"] for e in mod.by_equipment("barbell")] == ["Back Squat"]
    assert mod.EQUIPMENT_NAMES == {"BARBELL": "Barbell"}


def test_picker_mode_still_writes_catalog(gen, tmp_path, monkeypatch):
    html_file = tmp_path / "picker.html"
    html_file.write_text(PAGE.replace("user@example.com", "x"), encoding="utf-8")
    out = tmp_path / "exercises.py"
    monkeypatch.setattr(
        "sys.argv", ["generate_exercises.py", str(html_file), "--out", str(out)]
    )
    gen.main()
    mod = _load_rendered(out.read_text(encoding="utf-8"))
    assert mod.resolve("Back Squat") == {
        "name": "Back Squat",
        "category": "SQUAT",
        "exercise": "BACK_SQUAT",
        "display_name": "Back Squat",
        "primary_muscles": [],
        "secondary_muscles": [],
        "equipment": [],
    }


def test_previous_names_read_old_and_new_format(gen, tmp_path):
    old = tmp_path / "old.py"
    old.write_text('_RAW: list = [("A b", "C", "E")]\n', encoding="utf-8")
    assert gen.load_previous_names(old) == {("C", "E"): "A b"}
    new = tmp_path / "new.py"
    new.write_text(gen.render([{"name": "X", "category": "C", "exercise": "E"}]))
    assert gen.load_previous_names(new) == {("C", "E"): "X"}
    assert gen.load_previous_names(tmp_path / "missing.py") == {}


# --------------------------------------------------------------------------
# Category-only rows and --from-cache
# --------------------------------------------------------------------------


def test_category_rows_keep_old_generic_names(gen):
    previous = {
        ("CARDIO", "CARDIO"): "Cardio",  # old generic row
        ("PLANK", ""): "Plank (any)",  # category-only row of a new catalog
        ("BANDED_EXERCISES", "BANDED_EXERCISES"): "Banded Exercises",
    }
    labels = {
        "CARDIO": "Cardio label",
        "STRETCH": "Stretch",
        "BANDED_EXERCISES": "Bands",
    }
    rows, skipped = gen.category_rows(
        ["CARDIO", "PLANK", "STRETCH", "UNKNOWN", "BANDED_EXERCISES"],
        previous,
        labels,
        taken=["Banded Exercises"],
    )
    by_cat = {r["category"]: r for r in rows}
    assert by_cat["CARDIO"] == {
        "name": "Cardio",
        "category": "CARDIO",
        "exercise": "",
        "display_name": "Cardio label",
    }
    assert by_cat["PLANK"]["name"] == "Plank (any)"
    assert by_cat["PLANK"]["display_name"] == "Plank"
    assert by_cat["STRETCH"]["name"] == "Stretch"
    assert by_cat["UNKNOWN"]["name"] == by_cat["UNKNOWN"]["display_name"] == "Unknown"
    # A name already used by a real exercise wins; the category row is skipped.
    assert skipped == ["BANDED_EXERCISES"]
    assert "BANDED_EXERCISES" not in by_cat


def test_catalog_rows_have_unique_names_and_category_rows(gen):
    verified = [
        {"category": "CARDIO", "exercise": "JOG", "label": "Jog"},
        {"category": "CORE", "exercise": "CORE", "label": "Core"},
    ]
    rows, skipped = gen.catalog_rows(
        ["CARDIO", "CORE", "STRETCH"], verified, {("CARDIO", "CARDIO"): "Cardio"}, {}
    )
    names = [r["name"] for r in rows]
    assert len(names) == len(set(names))
    assert skipped == ["CORE"]  # "Core" is the real CORE/CORE exercise
    assert [(r["category"], r["exercise"]) for r in rows] == [
        ("CARDIO", ""),
        ("CARDIO", "JOG"),
        ("CORE", "CORE"),
        ("STRETCH", ""),
    ]


CACHE_PROPS = (
    "category_type_CARDIO=Cardio\n"
    "category_type_PULL_UP=Pull Up\n"
    "CARDIO_JOG=Jog\n"
    "CARDIO_CARDIO=Cardio\n"
    "PULL_UP_LAT_PULLDOWN=Lat pulldown\n"
)


def _cache_for(props_text, gen, bad=()):
    props = gen.parse_properties(props_text)
    cats = gen.candidate_categories(props)
    names = {}
    for c in gen.exercise_candidates(props, cats):
        names.setdefault(c["category"], {})[c["exercise"]] = c["exercise"] not in bad
    return {"categories": dict.fromkeys(cats, True), "names": names}


def test_rows_from_cache(gen):
    props = gen.parse_properties(CACHE_PROPS)
    cache = _cache_for(CACHE_PROPS, gen, bad={"CARDIO"})
    cache["categories"]["UNKNOWN"] = False
    valid, verified, n = gen.rows_from_cache(cache, props)
    assert "UNKNOWN" not in valid and "CARDIO" in valid
    assert n == 3
    assert [(v["category"], v["exercise"]) for v in verified] == [
        ("CARDIO", "JOG"),
        ("PULL_UP", "LAT_PULLDOWN"),
    ]


def test_rows_from_cache_refuses_untested_candidates(gen):
    props = gen.parse_properties(CACHE_PROPS)
    cache = _cache_for(CACHE_PROPS, gen)
    del cache["names"]["CARDIO"]["JOG"]
    with pytest.raises(ValueError, match="not in the cache"):
        gen.rows_from_cache(cache, props)
    cache = _cache_for(CACHE_PROPS, gen)
    del cache["categories"]["PULL_UP"]
    with pytest.raises(ValueError, match="categories not in the cache"):
        gen.rows_from_cache(cache, props)


def test_from_cache_writes_catalog_without_logging_in(
    gen, tmp_path, monkeypatch, capsys
):
    import garminconnect

    def no_login(*a, **k):
        raise AssertionError("must not log in")

    monkeypatch.setattr(garminconnect, "Garmin", no_login)
    src = tmp_path / "p.properties"
    src.write_text(CACHE_PROPS, encoding="utf-8")
    cache = tmp_path / "cache.json"
    cache.write_text(json.dumps(_cache_for(CACHE_PROPS, gen, bad={"CARDIO"})))
    previous = tmp_path / "old.py"
    previous.write_text(
        "_RAW: list[tuple[str, str, str]] = [\n"
        '    ("Cardio", "CARDIO", "CARDIO"),\n'
        '    ("Lat Pull-down", "PULL_UP", "LAT_PULLDOWN"),\n'
        "]\n",
        encoding="utf-8",
    )
    out = tmp_path / "exercises.py"
    gen.main(
        [
            "--from-cache",
            "--cache",
            str(cache),
            "--properties",
            str(src),
            "--previous",
            str(previous),
            "--out",
            str(out),
        ]
    )
    printed = capsys.readouterr().out
    assert "not verified" not in printed  # CARDIO/CARDIO lives on as a category row
    mod = _load_rendered(out.read_text(encoding="utf-8"))
    cardio = mod.resolve("Cardio")
    assert (cardio["category"], cardio["exercise"]) == ("CARDIO", "")
    assert mod.resolve("Lat Pull-down")["exercise"] == "LAT_PULLDOWN"
    assert mod.resolve("Pull Up") == {
        "name": "Pull Up",
        "category": "PULL_UP",
        "exercise": "",
        "display_name": "Pull Up",
        "primary_muscles": [],
        "secondary_muscles": [],
        "equipment": [],
    }
    assert "category-only" in mod.__doc__
    # Every category has a row (extras too) and names stay unique.
    assert len(mod.EXERCISES) == len(mod.BY_NAME) == 2 + len(mod.CATEGORIES)
    # Rendering again from its own output keeps the category-only names.
    assert gen.load_previous_names(out)[("CARDIO", "")] == "Cardio"


def test_from_cache_needs_cache(gen, tmp_path):
    src = tmp_path / "p.properties"
    src.write_text(CACHE_PROPS, encoding="utf-8")
    with pytest.raises(SystemExit, match="--cache"):
        gen.main(["--from-cache", "--properties", str(src)])
    with pytest.raises(SystemExit, match="only one"):
        gen.main(["--from-cache", "--verify", "--properties", str(src)])


def test_render_escapes_non_ascii_labels(gen):
    label = "Hero\u2019s Pose"
    source = gen.render([{"name": label, "category": "POSE", "exercise": "HEROS"}])
    assert source.isascii()
    assert _load_rendered(source).resolve(label)["exercise"] == "HEROS"


def test_metadata_normalises_garmin_muscle_typos(gen, tmp_path):
    (tmp_path / "Exercises.json").write_text(
        '{"categories": {"LUNGE": {"exercises": {"SIDE_LUNGE": {'
        '"primaryMuscles": ["ADBUCTORS", "ADDDUCTORS", ""],'
        '"secondaryMuscles": ["SHOULDER", "shoulders"]}}}}}',
        encoding="utf-8",
    )
    meta = gen.load_metadata(tmp_path)
    assert meta[("LUNGE", "SIDE_LUNGE")]["primary_muscles"] == [
        "ABDUCTORS",
        "ADDUCTORS",
    ]
    assert meta[("LUNGE", "SIDE_LUNGE")]["secondary_muscles"] == ["SHOULDERS"]
