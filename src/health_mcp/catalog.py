"""Allowed data types and strict civil/physical time filter construction."""

import json
import re
from datetime import UTC, date, datetime
from importlib.resources import files

from .errors import HealthError

CATALOG = {
    r["name"]: r for r in json.loads(files("health_mcp").joinpath("catalog.json").read_text())
}
SCOPES = [
    "https://www.googleapis.com/auth/googlehealth." + s + ".readonly"
    for s in (
        "activity_and_fitness",
        "health_metrics_and_measurements",
        "sleep",
        "profile",
        "settings",
        "nutrition",
        "location",
    )
]
SHORT_ROLLUP = {
    "calories-in-heart-rate-zone",
    "heart-rate",
    "active-minutes",
    "total-calories",
}


def data_type(name: str) -> dict:
    if name not in CATALOG:
        raise HealthError("UNSUPPORTED_DATA_TYPE")
    return CATALOG[name]


def parse_bound(value: str) -> date | datetime:
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return date.fromisoformat(value)
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError()
        return parsed.astimezone(UTC)
    except (ValueError, TypeError):
        raise HealthError("USE_ISO_DATE_OR_TIMESTAMP_WITH_TIMEZONE") from None


def validate_range(
    start: str | None, end: str | None
) -> tuple[date | datetime | None, date | datetime | None]:
    if (start is None) != (end is None):
        raise HealthError("BOTH_RANGE_BOUNDS_REQUIRED")
    if start is None:
        return None, None
    a, b = parse_bound(start), parse_bound(end)
    if type(a) is not type(b) or a >= b:
        raise HealthError("INVALID_CLOSED_OPEN_RANGE")
    return a, b


def build_filter(name: str, start: str | None, end: str | None) -> str | None:
    row = data_type(name)
    a, b = validate_range(start, end)
    if a is None:
        return None
    civil = type(a) is date
    kind = row["kind"]
    prefix = name.replace("-", "_")
    if kind == "Food":
        raise HealthError("FOOD_CATALOG_HAS_NO_TIME_RANGE")
    if kind == "Daily":
        if not civil:
            raise HealthError("DAILY_DATA_REQUIRES_DATE_BOUNDS")
        field = "date"
    elif name == "sleep":
        field = "interval.civil_end_time" if civil else "interval.end_time"
    elif kind == "Sample":
        field = "sample_time.civil_time" if civil else "sample_time.physical_time"
    else:
        # Exercise and nutrition sessions use civil time in the documented filter.
        if kind == "Session" and not civil:
            raise HealthError("SESSION_REQUIRES_CIVIL_DATE_BOUNDS")
        field = "interval.civil_start_time" if civil else "interval.start_time"
    return f'{prefix}.{field} >= "{a.isoformat()}" AND {prefix}.{field} < "{b.isoformat()}"'


def record_id(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", value):
        raise HealthError("INVALID_RECORD_ID")
    return value
