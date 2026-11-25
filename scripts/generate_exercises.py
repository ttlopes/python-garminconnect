#!/usr/bin/env python3
"""Regenerate ``garminconnect/exercises.py``, the strength-exercise catalog.

There are two ways to refresh the catalog.

**From Garmin's public translations, verified against an account**
(recommended)::

    python scripts/generate_exercises.py --fetch
    python scripts/generate_exercises.py --verify --tokens ~/.garminconnect --i-understand-this-writes --cache verify-cache.json [--metadata DIR]

``--fetch`` downloads the exercise translations Garmin Connect publishes
without a login (``exercise_types.properties``), extracts every candidate
category and exercise name, and prints the counts.  Nothing is written.

``--verify`` does the same, then checks every candidate against Garmin:
Garmin rejects a strength workout whose step has an unknown ``category``
with HTTP 400, but silently stores an unknown (or wrongly categorized)
exercise name as ``""``.  So each category is uploaded alone in a temporary
workout, and the names of the accepted categories are uploaded in batches;
a name is kept only if it reads back unchanged.  Every workout the script
creates is deleted again, even when the run fails.  This creates and deletes
many workouts on the account, so it needs ``--i-understand-this-writes``;
use a throwaway test account.  ``--cache`` saves progress so an interrupted
run resumes where it stopped.

``--from-cache`` writes the catalog again from a finished run's cache (for
example to add metadata later) without logging in or uploading anything.
Every accepted category also gets a category-only row (``exercise`` ``""``).

``--metadata DIR`` optionally adds muscles and equipment.  The Garmin Connect
web app loads them from ``Exercises.json`` and ``exerciseToEquipments.json``,
which need a browser login, so the script never downloads them: save either
file from the browser's network tab into DIR.

**From the exercise picker HTML** (the original method)::

    python scripts/generate_exercises.py path/to/picker.html

Open the workout editor at https://connect.garmin.com, add a strength
exercise, open the exercise picker and copy only the picker's ``<ul>`` HTML
into a file.  Do not paste the whole page: it comes from an authenticated
session and may contain account data, and the generated file is published
to a public repository.  Each selectable exercise is a
``<li role="option" ...>`` carrying ``data-category-key`` (movement group)
and ``data-exercise-key`` (specific variant) plus a display label.

Both methods refuse to write labels that look like session data (e-mail
addresses, tokens, URLs, GUIDs).  Run ``pdm run ruff format
garminconnect/exercises.py`` after regenerating.
"""

from __future__ import annotations

import argparse
import ast
import datetime as dt
import html
import json
import re
import sys
import time
from html.parser import HTMLParser
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator

OUT = Path(__file__).resolve().parent.parent / "garminconnect" / "exercises.py"

TRANSLATIONS_BASE = "https://connect.garmin.com/web-translations"
EXERCISE_TYPES_URL = f"{TRANSLATIONS_BASE}/exercise_types/exercise_types.properties"
EQUIPMENTS_URL = (
    f"{TRANSLATIONS_BASE}/exercise_equipments/exercise_equipments.properties"
)

# Categories worth testing beyond the ``category_type_`` keys of the
# translations file: key prefixes without a ``category_type_`` line (yoga
# poses, pilates/mobility moves, indoor rowing) and the cardio-style
# categories of the FIT exercise_category enum.  Verification drops any
# that Garmin rejects.
EXTRA_CATEGORIES: tuple[str, ...] = (
    "POSE",
    "MOVE",
    "INDOOR_ROW",
    "BIKE",
    "STRETCH",
    "UNKNOWN",
    "BIKE_OUTDOOR",
    "INDOOR_BIKE",
    "RUN_INDOOR",
    "ELLIPTICAL",
    "STAIR_STEPPER",
    "FLOOR_CLIMB",
    "RUN",
)

CATEGORY_PREFIX = "category_type_"
GENERIC_PREFIX = "exercise_type_"
_UPPER_KEY = re.compile(r"[A-Z0-9_]+")

STRENGTH_SPORT = {"sportTypeId": 5, "sportTypeKey": "strength_training"}
VERIFY_WORKOUT_NAME = "TEMP exercise catalog check (safe to delete)"
METADATA_FILES = ("Exercises.json", "exerciseToEquipments.json")


class _PickerParser(HTMLParser):
    """Collect (category, exercise, name) rows, each scoped to its own <li>.

    Regex extraction with DOTALL can reach past an element boundary and
    capture a <span> from unrelated page chrome; a real parser cannot, and
    it also keeps items separate when a closing </li> is omitted.
    """

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[tuple[str, str, str]] = []
        self._cur: list[str | None] | None = None
        # Nesting depth of <span> while inside the selected span; 0 means
        # not in one. A plain in/out flag would drop back out on an inner
        # </span>, losing any text after it (e.g. "Squat" in
        # "<span><span>Back</span> Squat</span>").
        self._span_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag == "li":
            # An unclosed previous <li> ends here, not at its </li>.
            self._flush()
            if "data-category-key" in a and "data-exercise-key" in a:
                self._cur = [a["data-category-key"], a["data-exercise-key"], None]
        elif tag == "span" and self._cur is not None:
            if self._span_depth:
                self._span_depth += 1
            elif self._cur[2] is None:
                self._span_depth = 1

    def handle_data(self, data: str) -> None:
        # A nested tag inside the span (e.g. <b>) triggers another
        # handle_data call for its own text; accumulate rather than
        # overwrite, or a fragment like the "https://" prefix of a stray
        # URL is dropped and the SUSPECT filter never sees it.
        if self._span_depth and self._cur is not None:
            self._cur[2] = (self._cur[2] or "") + data

    def handle_endtag(self, tag: str) -> None:
        if tag == "span" and self._span_depth:
            self._span_depth -= 1
        elif tag == "li" and self._cur is not None:
            self._flush()

    def _flush(self) -> None:
        # Attribute-only element (no span of its own): skip rather than
        # borrowing text from elsewhere in the document.
        if self._cur is not None and self._cur[2]:
            self.rows.append((self._cur[0] or "", self._cur[1] or "", self._cur[2]))
        self._cur = None
        self._span_depth = 0


# Names are display labels; anything shaped like session data means the
# extraction went out of scope (or the wrong HTML was pasted).
SUSPECT = re.compile(
    r"[\w.+-]+@[\w-]+\.[\w.]+"  # e-mail address
    r"|eyJ[\w-]{10,}"  # JWT
    r"|https?://"  # URL
    r"|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}",  # GUID
    re.IGNORECASE,
)


def parse(text: str) -> list[dict[str, str]]:
    """Extract unique (name, category, exercise) rows from picker HTML."""
    parser = _PickerParser()
    parser.feed(text)
    parser.close()
    # HTMLParser.close() doesn't synthesize a missing </li>, so the last
    # item (if the source HTML omits its closing tag) needs an explicit
    # flush here.
    parser._flush()
    seen: set[tuple[str, str]] = set()
    out: list[dict[str, str]] = []
    for category, exercise, name in parser.rows:
        key = (category, exercise)
        if key in seen:
            continue  # the "Recent" block repeats items listed below
        seen.add(key)
        out.append(
            {
                "name": html.unescape(name).strip(),
                "category": category,
                "exercise": exercise,
            }
        )
    out.sort(key=lambda e: (e["category"], e["name"].lower()))
    return out


# --------------------------------------------------------------------------
# Public translations (--fetch)
# --------------------------------------------------------------------------

_ESCAPES = {"t": "\t", "n": "\n", "r": "\r", "f": "\f"}


def _unescape_properties(text: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text):
            nxt = text[i + 1]
            if nxt == "u" and re.fullmatch(r"[0-9a-fA-F]{4}", text[i + 2 : i + 6]):
                out.append(chr(int(text[i + 2 : i + 6], 16)))
                i += 6
                continue
            out.append(_ESCAPES.get(nxt, nxt))
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _logical_lines(text: str) -> Iterator[str]:
    """Yield .properties logical lines, joining backslash continuations."""
    buf = ""
    for raw in text.splitlines():
        line = raw.lstrip() if buf else raw
        if not buf and (not line.strip() or line.lstrip()[:1] in "#!"):
            continue
        trailing = len(line) - len(line.rstrip("\\"))
        if trailing % 2:  # an odd number of backslashes continues the line
            buf += line[:-1]
            continue
        yield buf + line
        buf = ""
    if buf:
        yield buf


def _split_key_value(line: str) -> tuple[str, str]:
    # The key ends at the first unescaped "=", ":" or whitespace; the
    # value starts after optional whitespace and at most one "=" or ":".
    i = 0
    while i < len(line):
        ch = line[i]
        if ch == "\\":
            i += 2
            continue
        if ch in "=:" or ch.isspace():
            break
        i += 1
    rest = line[i:].lstrip(" \t\f")
    if rest and rest[0] in "=:":
        rest = rest[1:].lstrip(" \t\f")
    return line[:i], rest


def parse_properties(text: str) -> dict[str, str]:
    """Parse Java ``.properties`` text into a dict (later keys win)."""
    props: dict[str, str] = {}
    for line in _logical_lines(text.lstrip("﻿")):
        key, value = _split_key_value(line.lstrip())
        props[_unescape_properties(key)] = _unescape_properties(value)
    return props


def file_categories(props: dict[str, str]) -> dict[str, str]:
    """Return ``{CATEGORY: display name}`` from the ``category_type_`` keys."""
    return {
        key[len(CATEGORY_PREFIX) :]: value
        for key, value in props.items()
        if key.startswith(CATEGORY_PREFIX) and key[len(CATEGORY_PREFIX) :]
    }


def candidate_categories(props: dict[str, str]) -> list[str]:
    """Categories to test: the file's ``category_type_`` list plus extras."""
    return sorted(set(file_categories(props)) | set(EXTRA_CATEGORIES))


def split_key(key: str, categories: Iterable[str]) -> tuple[str, str] | None:
    """Split ``<CATEGORY>_<NAME>`` using the longest matching category.

    Names may themselves start with an underscore (``CALF_RAISE__3_WAY_...``
    is category ``CALF_RAISE``, name ``_3_WAY_...``).
    """
    best = ""
    for cat in categories:
        if (
            len(cat) > len(best)
            and len(key) > len(cat) + 1
            and key.startswith(cat + "_")
        ):
            best = cat
    if not best:
        return None
    return best, key[len(best) + 1 :]


def exercise_candidates(
    props: dict[str, str], categories: Iterable[str]
) -> list[dict[str, str]]:
    """Return ``{category, exercise, label}`` candidates from the translations.

    Only all-upper-case keys are exercise keys; lower-case keys
    (``category_type_``, ``exercise_type_``, ``muscle_``, ...) are labels for
    other things.  Keys that do not start with a known category are dropped.
    """
    cats = list(categories)
    out: list[dict[str, str]] = []
    for key, label in props.items():
        if not _UPPER_KEY.fullmatch(key):
            continue
        split = split_key(key, cats)
        if split is None:
            continue
        out.append({"category": split[0], "exercise": split[1], "label": label})
    out.sort(key=lambda e: (e["category"], e["exercise"]))
    return out


def summarize(props: dict[str, str]) -> dict[str, int]:
    """Count what the translations file contains (for ``--fetch``)."""
    cats = candidate_categories(props)
    upper = [k for k in props if _UPPER_KEY.fullmatch(k)]
    candidates = exercise_candidates(props, cats)
    return {
        "keys": len(props),
        "file_categories": len(file_categories(props)),
        "candidate_categories": len(cats),
        "generic_exercise_types": sum(k.startswith(GENERIC_PREFIX) for k in props),
        "exercise_keys": len(upper),
        "candidates": len(candidates),
        "unmatched_exercise_keys": len(upper) - len(candidates),
    }


def download(url: str, timeout: float = 30.0) -> str:
    """Download one of Garmin's public translation files as text."""
    import requests

    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.content.decode("utf-8")


def load_properties(path: str | None, url: str) -> dict[str, str]:
    """Read a saved ``.properties`` file, or download it when no path is given."""
    if path:
        return parse_properties(Path(path).read_text(encoding="utf-8"))
    return parse_properties(download(url))


# --------------------------------------------------------------------------
# Live verification (--verify)
# --------------------------------------------------------------------------


def _status(exc: BaseException) -> int | None:
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if isinstance(status, int):
        return status
    m = re.search(r"\((\d{3})\)", str(exc))
    return int(m.group(1)) if m else None


def _is_rate_limited(exc: BaseException) -> bool:
    from garminconnect.exceptions import GarminConnectTooManyRequestsError

    return isinstance(exc, GarminConnectTooManyRequestsError) or _status(exc) == 429


def _is_rejected(exc: BaseException) -> bool:
    from garminconnect.exceptions import GarminConnectConnectionError

    return isinstance(exc, GarminConnectConnectionError) and _status(exc) == 400


def _step(order: int, category: str, exercise: str) -> dict[str, Any]:
    return {
        "type": "ExecutableStepDTO",
        "stepOrder": order,
        "stepType": {"stepTypeId": 3, "stepTypeKey": "interval"},
        "endCondition": {"conditionTypeId": 10, "conditionTypeKey": "reps"},
        "endConditionValue": 10,
        "category": category,
        "exerciseName": exercise,
    }


def _workout(steps: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "workoutName": VERIFY_WORKOUT_NAME,
        "sportType": dict(STRENGTH_SPORT),
        "workoutSegments": [
            {
                "segmentOrder": 1,
                "sportType": dict(STRENGTH_SPORT),
                "workoutSteps": steps,
            }
        ],
    }


def _read_steps(workout: Any) -> dict[int, dict[str, Any]]:
    """Map stepOrder to step for every step of a read-back workout."""
    found: dict[int, dict[str, Any]] = {}

    def walk(steps: Any) -> None:
        for step in steps or []:
            if not isinstance(step, dict):
                continue
            if isinstance(step.get("stepOrder"), int):
                found[step["stepOrder"]] = step
            walk(step.get("workoutSteps"))

    if isinstance(workout, dict):
        for segment in workout.get("workoutSegments") or []:
            if isinstance(segment, dict):
                walk(segment.get("workoutSteps"))
    return found


def _batches(items: list[str], size: int) -> Iterator[list[str]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


class Verifier:
    """Check candidates by uploading, reading back and deleting workouts.

    ``api`` needs ``upload_workout``, ``get_workout_by_id`` and
    ``delete_workout`` (a logged-in :class:`garminconnect.Garmin`).
    """

    def __init__(
        self,
        api: Any,
        *,
        cache_path: str | Path | None = None,
        batch_size: int = 50,
        delay: float = 1.0,
        max_retries: int = 4,
        backoff: float = 60.0,
        sleep: Callable[[float], Any] = time.sleep,
        log: Callable[[str], Any] = print,
    ) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be >= 1")
        self.api = api
        self.cache_path = Path(cache_path) if cache_path else None
        self.batch_size = batch_size
        self.delay = delay
        self.max_retries = max_retries
        self.backoff = backoff
        self.sleep = sleep
        self.log = log
        self.created = 0
        self.deleted = 0
        self.pending: list[Any] = []
        self.cache: dict[str, Any] = {"categories": {}, "names": {}}
        if self.cache_path and self.cache_path.exists():
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self.cache["categories"].update(data.get("categories") or {})
                self.cache["names"].update(data.get("names") or {})

    # -- plumbing ----------------------------------------------------------

    def _save(self) -> None:
        if not self.cache_path:
            return
        tmp = self.cache_path.with_name(self.cache_path.name + ".tmp")
        tmp.write_text(json.dumps(self.cache, indent=1, sort_keys=True), "utf-8")
        tmp.replace(self.cache_path)

    def _call(self, func: Callable[..., Any], *args: Any) -> Any:
        """Call ``func``, waiting and retrying when Garmin rate limits."""
        for attempt in range(self.max_retries + 1):
            try:
                return func(*args)
            except Exception as e:
                if not _is_rate_limited(e) or attempt >= self.max_retries:
                    raise
                wait = self.backoff * (attempt + 1)
                self.log(f"  rate limited (429), waiting {wait:.0f}s and retrying")
                self.sleep(wait)
        raise RuntimeError("retry loop exited without a result")  # unreachable

    def _delete(self, workout_id: Any) -> bool:
        from garminconnect.exceptions import GarminConnectNotFoundError

        try:
            self._call(self.api.delete_workout, workout_id)
        except GarminConnectNotFoundError:
            pass  # already gone
        except Exception as e:
            self.log(f"  could not delete workout {workout_id}: {type(e).__name__}")
            return False
        self.pending.remove(workout_id)
        self.deleted += 1
        return True

    def _try(self, steps: list[dict[str, Any]]) -> dict[int, dict[str, Any]] | None:
        """Upload ``steps``, read them back and delete the workout.

        Returns the read-back steps by ``stepOrder``, or ``None`` when
        Garmin rejected the upload with HTTP 400.
        """
        self.sleep(self.delay)
        try:
            created = self._call(self.api.upload_workout, _workout(steps))
        except Exception as e:
            if _is_rejected(e):
                return None
            raise
        workout_id = created.get("workoutId") if isinstance(created, dict) else None
        if not workout_id:
            raise RuntimeError("upload_workout returned no workoutId")
        self.pending.append(workout_id)
        self.created += 1
        try:
            return _read_steps(self._call(self.api.get_workout_by_id, workout_id))
        finally:
            self._delete(workout_id)

    def cleanup(self) -> list[Any]:
        """Delete every workout still pending; return the ids that failed."""
        for workout_id in list(self.pending):
            self._delete(workout_id)
        return list(self.pending)

    # -- checks ------------------------------------------------------------

    def verify_categories(self, categories: Iterable[str]) -> list[str]:
        """Return the categories Garmin accepts (each tested alone)."""
        known: dict[str, bool] = self.cache["categories"]
        cats = list(categories)
        for i, cat in enumerate(cats, 1):
            if cat in known:
                continue
            steps = self._try([_step(1, cat, "")])
            known[cat] = steps is not None and steps.get(1, {}).get("category") == cat
            self.log(
                f"  category {i}/{len(cats)} {cat}: "
                f"{'ok' if known[cat] else 'rejected'}"
            )
            self._save()
        return [c for c in cats if known.get(c)]

    def _check_batch(self, category: str, names: list[str]) -> None:
        done: dict[str, bool] = self.cache["names"].setdefault(category, {})
        steps = self._try([_step(i, category, name) for i, name in enumerate(names, 1)])
        if steps is None:
            # Garmin rejected the whole workout; narrow down the culprit.
            if len(names) == 1:
                done[names[0]] = False
                return
            half = len(names) // 2
            self._check_batch(category, names[:half])
            self._check_batch(category, names[half:])
            return
        for i, name in enumerate(names, 1):
            step = steps.get(i, {})
            done[name] = (
                step.get("category") == category and step.get("exerciseName") == name
            )

    def verify_names(self, names: dict[str, list[str]]) -> dict[str, list[str]]:
        """Return the names that read back unchanged, per category."""
        result: dict[str, list[str]] = {}
        for category in sorted(names):
            done: dict[str, bool] = self.cache["names"].setdefault(category, {})
            todo = [n for n in names[category] if n not in done]
            for batch in _batches(todo, self.batch_size):
                self._check_batch(category, batch)
                self._save()
            kept = [n for n in names[category] if done.get(n)]
            self.log(f"  {category}: {len(kept)}/{len(names[category])} names kept")
            result[category] = kept
        return result

    def run(
        self, categories: Iterable[str], candidates: list[dict[str, str]]
    ) -> tuple[list[str], list[dict[str, str]]]:
        """Verify categories, then names; always delete created workouts.

        Candidates are re-split against the accepted categories, so a key
        whose longest prefix was a rejected category can still fall back to
        a shorter, valid one.
        """
        try:
            valid = self.verify_categories(categories)
            labels = {
                f"{c['category']}_{c['exercise']}": c["label"] for c in candidates
            }
            names: dict[str, list[str]] = {}
            resplit: dict[tuple[str, str], str] = {}
            for key, label in labels.items():
                split = split_key(key, valid)
                if split:
                    names.setdefault(split[0], []).append(split[1])
                    resplit[split] = label
            kept = self.verify_names(names)
            rows = [
                {"category": cat, "exercise": name, "label": resplit[(cat, name)]}
                for cat in sorted(kept)
                for name in kept[cat]
            ]
            return valid, rows
        finally:
            failed = self.cleanup()
            self.log(
                f"Deletion summary: created {self.created} temporary workouts, "
                f"deleted {self.deleted}, failed {len(failed)}"
            )
            if failed:
                self.log(
                    "Delete these workouts by hand (named "
                    f"{VERIFY_WORKOUT_NAME!r}): {failed}"
                )


# --------------------------------------------------------------------------
# Metadata (--metadata)
# --------------------------------------------------------------------------


def _str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [v for v in value if isinstance(v, str)]


# Garmin's own muscle data has typos and a singular/plural pair; map them to
# one spelling so filtering by muscle finds every exercise.
MUSCLE_ALIASES = {
    "ADBUCTORS": "ABDUCTORS",
    "ADDDUCTORS": "ADDUCTORS",
    "SHOULDER": "SHOULDERS",
}


def _muscles(value: Any) -> list[str]:
    """Normalised, de-duplicated muscle keys (empty strings dropped)."""
    out: list[str] = []
    for m in _str_list(value):
        key = MUSCLE_ALIASES.get(m.strip().upper(), m.strip().upper())
        if key and key not in out:
            out.append(key)
    return out


def load_metadata(directory: str | Path) -> dict[tuple[str, str], dict[str, list[str]]]:
    """Read muscles/equipment from saved ``Exercises.json`` and/or
    ``exerciseToEquipments.json`` files in ``directory``.
    """
    d = Path(directory)
    meta: dict[tuple[str, str], dict[str, list[str]]] = {}

    def entry(cat: Any, name: Any) -> dict[str, list[str]] | None:
        if not isinstance(cat, str) or not isinstance(name, str):
            return None
        return meta.setdefault(
            (cat, name),
            {"primary_muscles": [], "secondary_muscles": [], "equipment": []},
        )

    eq_file = d / "exerciseToEquipments.json"
    if eq_file.exists():
        for group in json.loads(eq_file.read_text(encoding="utf-8")) or []:
            if not isinstance(group, dict):
                continue
            for ex in group.get("exercisesInCategory") or []:
                if not isinstance(ex, dict):
                    continue
                e = entry(group.get("exerciseCategoryKey"), ex.get("exerciseKey"))
                if e is None:
                    continue
                e["primary_muscles"] = _muscles(ex.get("primaryMuscles"))
                e["secondary_muscles"] = _muscles(ex.get("secondaryMuscles"))
                e["equipment"] = _str_list(ex.get("equipmentKeys"))

    # Exercises.json is the web app's main muscle source: it wins for muscles.
    ex_file = d / "Exercises.json"
    if ex_file.exists():
        data = json.loads(ex_file.read_text(encoding="utf-8"))
        cats = data.get("categories") if isinstance(data, dict) else None
        for cat, body in (cats or {}).items():
            exercises = body.get("exercises") if isinstance(body, dict) else None
            for name, info in (exercises or {}).items():
                e = entry(cat, name)
                if e is None or not isinstance(info, dict):
                    continue
                if "primaryMuscles" in info:
                    e["primary_muscles"] = _muscles(info["primaryMuscles"])
                if "secondaryMuscles" in info:
                    e["secondary_muscles"] = _muscles(info["secondaryMuscles"])

    if not eq_file.exists() and not ex_file.exists():
        raise FileNotFoundError(f"neither of {METADATA_FILES} found in {d}")
    return meta


def attach_metadata(
    rows: list[dict[str, Any]], meta: dict[tuple[str, str], dict[str, list[str]]]
) -> int:
    """Add metadata fields to ``rows`` in place; return how many matched."""
    matched = 0
    for row in rows:
        m = meta.get((row["category"], row["exercise"]))
        if m:
            matched += 1
        for field in ("primary_muscles", "secondary_muscles", "equipment"):
            row[field] = list(m[field]) if m else []
    return matched


def equipment_labels(keys: Iterable[str], props: dict[str, str]) -> dict[str, str]:
    """Look up display names for equipment keys in the equipment translations.

    Uses the exact key when present, else the shortest translation key that
    ends with ``_<KEY>`` (case-insensitive), e.g. ``equipment_type_BARBELL``.
    """
    upper = {k.upper(): v for k, v in props.items()}
    out: dict[str, str] = {}
    for key in sorted(set(keys)):
        if key.upper() in upper:
            out[key] = upper[key.upper()]
            continue
        matches = sorted((k for k in upper if k.endswith("_" + key.upper())), key=len)
        if matches:
            out[key] = upper[matches[0]]
    return out


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def load_previous_names(path: Path = OUT) -> dict[tuple[str, str], str]:
    """Return ``{(category, exercise): name}`` from an existing catalog file.

    Reads the ``_RAW`` literal without importing the module.
    """
    if not path.exists():
        return {}
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        target: ast.expr | None = None
        value: ast.expr | None = None
        if isinstance(node, ast.AnnAssign):
            target, value = node.target, node.value
        elif isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        if isinstance(target, ast.Name) and target.id == "_RAW" and value:
            rows = ast.literal_eval(value)
            return {(r[1], r[2]): r[0] for r in rows}
    return {}


def build_rows(
    verified: list[dict[str, str]], previous: dict[tuple[str, str], str]
) -> list[dict[str, Any]]:
    """Turn verified candidates into catalog rows.

    ``name`` keeps the previous catalog's name for the same
    category/exercise so existing ``resolve()`` / ``BY_NAME`` lookups keep
    working; new exercises use Garmin's label.  ``display_name`` is always
    Garmin's label from the translations file.
    """
    rows: list[dict[str, Any]] = []
    for v in verified:
        label = v["label"].strip()
        rows.append(
            {
                "name": previous.get((v["category"], v["exercise"]), label),
                "category": v["category"],
                "exercise": v["exercise"],
                "display_name": label,
            }
        )
    rows.sort(key=lambda e: (e["category"], e["name"].lower()))
    return rows


def _title(category: str) -> str:
    return category.replace("_", " ").title()


def category_rows(
    categories: Iterable[str],
    previous: dict[tuple[str, str], str],
    labels: dict[str, str],
    taken: Iterable[str] = (),
) -> tuple[list[dict[str, Any]], list[str]]:
    """Return one category-only row (``exercise`` ``""``) per category.

    A step with only a category sends an empty ``exerciseName``, which is
    also what Garmin stores for a category's generic name (e.g.
    ``CARDIO``/``CARDIO``), so these rows replace the old generic rows.
    ``name`` is the previous catalog's name for that category's
    category-only or generic row, else Garmin's ``category_type_`` label,
    else the title-cased category; ``display_name`` is Garmin's label when
    known.  A category whose name is already used (by a real exercise in
    ``taken``) gets no row, so ``BY_NAME`` stays unambiguous; those
    categories are returned as the second item.
    """
    used = set(taken)
    rows: list[dict[str, Any]] = []
    skipped: list[str] = []
    for cat in sorted(set(categories)):
        label = (labels.get(cat) or "").strip()
        name = (
            previous.get((cat, "")) or previous.get((cat, cat)) or label or _title(cat)
        )
        if name in used:
            skipped.append(cat)
            continue
        used.add(name)
        rows.append(
            {
                "name": name,
                "category": cat,
                "exercise": "",
                "display_name": label or _title(cat),
            }
        )
    return rows, skipped


def catalog_rows(
    valid: Iterable[str],
    verified: list[dict[str, str]],
    previous: dict[tuple[str, str], str],
    category_labels: dict[str, str],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Exercise rows plus one category-only row per valid category, sorted.

    Returns the rows and the categories whose category-only row was
    skipped because its name is used by a real exercise.
    """
    rows = build_rows(verified, previous)
    extra, skipped = category_rows(
        valid, previous, category_labels, (r["name"] for r in rows)
    )
    rows += extra
    rows.sort(key=lambda e: (e["category"], e["exercise"] != "", e["name"].lower()))
    return rows, skipped


def rows_from_cache(
    cache: dict[str, Any], props: dict[str, str]
) -> tuple[list[str], list[dict[str, str]], int]:
    """Rebuild a ``--verify`` result from its cache without contacting Garmin.

    Returns the accepted categories, the verified candidates and the
    number of candidate names.  Fails if a candidate was never tested.
    """
    tested_cats: dict[str, bool] = cache.get("categories") or {}
    tested_names: dict[str, dict[str, bool]] = cache.get("names") or {}
    missing = set(candidate_categories(props)) - set(tested_cats)
    if missing:
        raise ValueError(
            f"categories not in the cache: {sorted(missing)}; run --verify"
        )
    valid = sorted(c for c, ok in tested_cats.items() if ok)
    verified: list[dict[str, str]] = []
    untested = 0
    candidates = 0
    for key, label in props.items():
        if not _UPPER_KEY.fullmatch(key):
            continue
        split = split_key(key, valid)
        if split is None:
            continue
        candidates += 1
        result = tested_names.get(split[0], {}).get(split[1])
        if result is None:
            untested += 1
        elif result:
            verified.append(
                {"category": split[0], "exercise": split[1], "label": label}
            )
    if untested:
        raise ValueError(
            f"{untested} candidate names are not in the cache; run --verify"
        )
    verified.sort(key=lambda e: (e["category"], e["exercise"]))
    return valid, verified, candidates


def suspect_rows(rows: list[dict[str, Any]]) -> list[int]:
    """Return positions of rows with a label that looks like session data."""
    bad = []
    for i, row in enumerate(rows):
        texts = [row.get("name", ""), row.get("display_name", "")]
        for field in ("primary_muscles", "secondary_muscles", "equipment"):
            texts += row.get(field) or []
        if any(SUSPECT.search(t) for t in texts):
            bad.append(i)
    return bad


_PICKER_HEADER = [
    "Every selectable strength exercise from the Garmin Connect workout editor,",
    "with the ``category`` (movement/muscle group) and ``exercise`` (specific",
    "variant) enum values a strength workout step needs.",
]


def render(
    exercises: list[dict[str, Any]],
    *,
    header: list[str] | None = None,
    categories: Iterable[str] | None = None,
    equipment_names: dict[str, str] | None = None,
) -> str:
    """Render the exercises as the ``garminconnect/exercises.py`` module source.

    Rows need ``name``, ``category`` and ``exercise``; ``display_name``
    defaults to ``name`` and the muscle/equipment lists to ``[]``.
    """
    cats = sorted({e["category"] for e in exercises} | set(categories or ()))
    lines = [
        '"""Garmin Connect strength-exercise catalog.',
        "",
        *(header or _PICKER_HEADER),
        "",
        "Use the ``category`` / ``exercise`` pair with",
        "``create_strength_exercise_step`` / ``create_strength_set``.",
        "",
        f"Contains {len(exercises)} exercises across {len(cats)} categories.",
        "Regenerate with ``scripts/generate_exercises.py``.",
        '"""',
        "",
        "from __future__ import annotations",
        "",
        "from typing import TypedDict",
        "",
        "",
        "class Exercise(TypedDict):",
        '    """One catalog entry."""',
        "",
        "    name: str",
        "    category: str",
        "    exercise: str",
        "    display_name: str",
        "    primary_muscles: list[str]",
        "    secondary_muscles: list[str]",
        "    equipment: list[str]",
        "",
        "",
        "# Raw catalog rows: (name, category, exercise, display name,",
        "# primary muscles, secondary muscles, equipment).",
        '# ``exercise`` may be "" (the workout shows only the category name).',
        "_RAW: list[",
        "    tuple[str, str, str, str, tuple[str, ...], tuple[str, ...], tuple[str, ...]]",
        "] = [",
    ]
    for e in exercises:
        row = (
            e["name"],
            e["category"],
            e["exercise"],
            e.get("display_name") or e["name"],
            tuple(e.get("primary_muscles") or ()),
            tuple(e.get("secondary_muscles") or ()),
            tuple(e.get("equipment") or ()),
        )
        # ascii() escapes non-ASCII labels (e.g. the U+2019 apostrophe in
        # Garmin's "Hero's Pose" label), which ruff flags as ambiguous (RUF001).
        lines.append(f"    {row!a},")
    lines += [
        "]",
        "",
        "EXERCISES: list[Exercise] = [",
        "    {",
        '        "name": name,',
        '        "category": category,',
        '        "exercise": exercise,',
        '        "display_name": display_name,',
        '        "primary_muscles": list(primary),',
        '        "secondary_muscles": list(secondary),',
        '        "equipment": list(equipment),',
        "    }",
        "    for name, category, exercise, display_name, primary, secondary, equipment in _RAW",
        "]",
        "",
        "# Lookup by exact name.",
        'BY_NAME: dict[str, Exercise] = {e["name"]: e for e in EXERCISES}',
        "",
        "# Lookup by Garmin's display label, for names that differ from it.",
        "_BY_DISPLAY_NAME: dict[str, Exercise] = {",
        '    e["display_name"]: e for e in EXERCISES',
        "}",
        "",
        "# All exercise categories (movement/muscle groups) a step can use.",
        f"CATEGORIES: list[str] = {cats!r}",
        "",
        "# Display names of the equipment keys used in ``equipment``.",
        f"EQUIPMENT_NAMES: dict[str, str] = {dict(sorted((equipment_names or {}).items()))!a}",
        "",
        "",
        "def resolve(name: str) -> Exercise | None:",
        '    """Return the catalog entry for an exact name or display name, or None."""',
        "    return BY_NAME.get(name) or _BY_DISPLAY_NAME.get(name)",
        "",
        "",
        "def find(term: str) -> list[Exercise]:",
        '    """Return exercises whose name contains ``term`` (case-insensitive)."""',
        "    needle = term.lower()",
        "    return [",
        "        e",
        "        for e in EXERCISES",
        '        if needle in e["name"].lower() or needle in e["display_name"].lower()',
        "    ]",
        "",
        "",
        "def by_muscle(muscle: str, primary_only: bool = False) -> list[Exercise]:",
        '    """Return exercises that work ``muscle`` (e.g. ``"CHEST"``).',
        "",
        "    Matches primary muscles, and secondary ones unless ``primary_only``.",
        "    Empty when the catalog was generated without muscle metadata.",
        '    """',
        "    key = muscle.upper()",
        "    return [",
        "        e",
        "        for e in EXERCISES",
        '        if key in e["primary_muscles"]',
        '        or (not primary_only and key in e["secondary_muscles"])',
        "    ]",
        "",
        "",
        "def by_equipment(equipment: str) -> list[Exercise]:",
        '    """Return exercises that use ``equipment`` (an equipment key).',
        "",
        "    Empty when the catalog was generated without equipment metadata.",
        '    """',
        "    key = equipment.upper()",
        '    return [e for e in EXERCISES if key in e["equipment"]]',
        "",
    ]
    return "\n".join(lines)


def _refuse_suspect(rows: list[dict[str, Any]]) -> None:
    suspect = suspect_rows(rows)
    if suspect:
        # Report only row positions — the matched labels are exactly the
        # potentially sensitive text we refuse to write out.
        sys.exit(
            f"refusing to write: {len(suspect)} entr{'ies' if len(suspect) != 1 else 'y'} "
            "look like session data, not exercise names (rows "
            f"{suspect}; check the pasted HTML)"
        )


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Regenerate garminconnect/exercises.py from the exercise picker "
            "HTML, or from Garmin's public exercise translations (--fetch / "
            "--verify)."
        )
    )
    p.add_argument("picker", nargs="?", help="saved exercise picker <ul> HTML")
    p.add_argument(
        "--fetch",
        action="store_true",
        help="download the public translations and print candidate counts",
    )
    p.add_argument(
        "--verify",
        action="store_true",
        help=(
            "check every candidate against Garmin by uploading, reading back "
            "and deleting temporary strength workouts, then write the catalog. "
            "This creates and deletes many workouts: use a throwaway test "
            "account. Requires --tokens and --i-understand-this-writes"
        ),
    )
    p.add_argument(
        "--from-cache",
        action="store_true",
        help=(
            "write the catalog from a finished --verify run's --cache file, "
            "without logging in or uploading anything"
        ),
    )
    p.add_argument(
        "--previous",
        help=(
            "catalog file whose names to keep (default: the --out file); "
            "e.g. a copy of the catalog before an earlier regeneration"
        ),
    )
    p.add_argument("--tokens", help="saved Garmin token store for --verify")
    p.add_argument(
        "--i-understand-this-writes",
        dest="confirm_writes",
        action="store_true",
        help="confirm that --verify may create and delete workouts on the account",
    )
    p.add_argument("--cache", help="JSON file that saves --verify progress")
    p.add_argument(
        "--metadata",
        help=f"directory with saved {' and/or '.join(METADATA_FILES)}",
    )
    p.add_argument("--batch-size", type=int, default=50, help="names per workout")
    p.add_argument(
        "--delay", type=float, default=1.0, help="seconds to wait before each upload"
    )
    p.add_argument(
        "--properties", help="use a saved exercise_types.properties (no download)"
    )
    p.add_argument(
        "--equipment-properties",
        help="use a saved exercise_equipments.properties (no download)",
    )
    p.add_argument("--out", default=str(OUT), help="output file (default: %(default)s)")
    return p


def _picker_main(path: str, out: Path) -> None:
    exercises = parse(Path(path).read_text(encoding="utf-8"))
    _refuse_suspect(exercises)
    out.write_text(render(exercises), encoding="utf-8")
    print(f"Wrote {len(exercises)} exercises to {out}")
    print("Run `pdm run format` to normalize quoting/formatting.")


def _verify_main(args: argparse.Namespace, props: dict[str, str]) -> None:
    if not args.tokens:
        sys.exit("--verify needs --tokens PATH (a saved Garmin token store)")
    if not args.confirm_writes:
        sys.exit(
            "--verify creates and deletes many temporary workouts on the "
            "account (use a throwaway test account); pass "
            "--i-understand-this-writes to proceed"
        )
    meta = load_metadata(args.metadata) if args.metadata else None

    from garminconnect import Garmin

    api = Garmin()
    api.login(args.tokens)
    categories = candidate_categories(props)
    candidates = exercise_candidates(props, categories)
    verifier = Verifier(
        api, cache_path=args.cache, batch_size=args.batch_size, delay=args.delay
    )
    valid, verified = verifier.run(categories, candidates)
    _write_catalog(args, props, meta, valid, verified, len(categories), len(candidates))


def _from_cache_main(args: argparse.Namespace, props: dict[str, str]) -> None:
    if not args.cache or not Path(args.cache).exists():
        sys.exit("--from-cache needs --cache PATH (the cache of a --verify run)")
    meta = load_metadata(args.metadata) if args.metadata else None
    cache = json.loads(Path(args.cache).read_text(encoding="utf-8"))
    try:
        valid, verified, n_candidates = rows_from_cache(cache, props)
    except ValueError as e:
        sys.exit(str(e))
    _write_catalog(
        args,
        props,
        meta,
        valid,
        verified,
        len(cache.get("categories") or {}),
        n_candidates,
    )


def _write_catalog(
    args: argparse.Namespace,
    props: dict[str, str],
    meta: dict[tuple[str, str], dict[str, list[str]]] | None,
    valid: list[str],
    verified: list[dict[str, str]],
    n_categories: int,
    n_candidates: int,
) -> None:
    out = Path(args.out)
    previous = load_previous_names(Path(args.previous) if args.previous else out)
    rows, skipped = catalog_rows(valid, verified, previous, file_categories(props))
    if skipped:
        print(
            "No category-only row (name used by an exercise) for: " + ", ".join(skipped)
        )
    n_category_rows = sum(1 for r in rows if r["exercise"] == "")
    equipment_names: dict[str, str] = {}
    if meta is not None:
        matched = attach_metadata(rows, meta)
        print(f"Metadata found for {matched}/{len(rows)} exercises")
        used = {k for r in rows for k in r["equipment"]}
        if used:
            eq_props = load_properties(args.equipment_properties, EQUIPMENTS_URL)
            equipment_names = equipment_labels(used, eq_props)
    _refuse_suspect(rows)

    # An old row is still covered if its key or its name survived (generic
    # rows such as CARDIO/CARDIO live on as category-only rows).
    names = {r["name"] for r in rows}
    keys = {(r["category"], r["exercise"]) for r in rows}
    gone = sorted(k for k, n in previous.items() if k not in keys and n not in names)
    if gone:
        shown = ", ".join(f"{c}/{e}" for c, e in gone[:20])
        print(f"{len(gone)} exercises of the old catalog were not verified: {shown}")

    today = dt.datetime.now(dt.UTC).date().isoformat()
    n_exercises = len(rows) - n_category_rows
    header = [
        f"Generated on {today} by ``scripts/generate_exercises.py --verify``.",
        "Candidates come from Garmin Connect's public exercise translations",
        f"({EXERCISE_TYPES_URL}).",
        f"Each of the {n_categories} candidate categories was uploaded alone in a",
        "temporary strength workout; Garmin rejects an unknown category with",
        f"HTTP 400. {len(valid)} were accepted.",
        f"The {n_candidates} candidate exercise names were uploaded in batches",
        "and kept only if they read back unchanged (Garmin stores unknown or",
        f"miscategorized names as an empty string): {n_exercises} passed.",
        f'{n_category_rows} rows are category-only (``exercise`` is ``""``): a step',
        "built from one sends an empty exerciseName, which is what Garmin",
        "stores for a category's generic name (e.g. CARDIO/CARDIO) anyway.",
        (
            "Muscles and equipment come from the Garmin Connect web app's"
            if meta is not None
            else "No muscle/equipment metadata was supplied, so those lists are empty."
        ),
    ]
    if meta is not None:
        header.append("``Exercises.json`` / ``exerciseToEquipments.json``.")
    out.write_text(
        render(rows, header=header, categories=valid, equipment_names=equipment_names),
        encoding="utf-8",
    )
    print(
        f"Wrote {len(rows)} rows ({n_exercises} exercises, {n_category_rows} "
        f"category-only) in {len(valid)} categories to {out}"
    )
    print(f"Run `pdm run ruff format {out}` to normalize formatting.")


def main(argv: list[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    modes = [args.fetch, args.verify, args.from_cache]
    if sum(modes) > 1 or (args.picker and any(modes)):
        sys.exit(
            "give only one of: a picker HTML file, --fetch, --verify, --from-cache"
        )
    if args.picker:
        _picker_main(args.picker, Path(args.out))
        return
    if not any(modes):
        _parser().print_usage()
        sys.exit("give a picker HTML file, --fetch, --verify or --from-cache")

    props = load_properties(args.properties, EXERCISE_TYPES_URL)
    for key, count in summarize(props).items():
        print(f"{key.replace('_', ' ')}: {count}")
    if args.verify:
        _verify_main(args, props)
    elif args.from_cache:
        _from_cache_main(args, props)


if __name__ == "__main__":
    main()
