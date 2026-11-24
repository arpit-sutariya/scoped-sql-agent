"""Small, testable functions that turn messy source values into clean ones.
Each returns None when it can't parse, so the caller can log a data quality issue instead of guessing."""
from __future__ import annotations

import re
from datetime import date, datetime, timezone

REGION_ALIASES = {
    "west": "West", "west coast": "West",
    "central": "Central", "midwest": "Central",
    "east": "East", "northeast": "East", "east coast": "East",
    "south": "South", "southeast": "South",
}
ERP_REGION_CODES = {"WST": "West", "CEN": "Central", "EST": "East", "STH": "South"}

_LEGAL = re.compile(r"\b(inc|llc|co|corp|corporation|company|ltd)\b\.?", re.IGNORECASE)


def region(raw: str | None) -> str | None:
    return REGION_ALIASES.get((raw or "").strip().lower())


def name_key(name: str | None) -> str:
    """'Blue Water Logistics LLC ' and 'BLUEWATER LOGISTICS' -> 'bluewaterlogistics'."""
    s = _LEGAL.sub(" ", (name or "").lower())
    return re.sub(r"[^a-z0-9]", "", s)


def revenue(raw: str | None) -> tuple[float | None, bool]:
    """Returns (USD, is_estimate). '$1.2M' -> 1200000; 'approx 4M' -> (4000000, True); '' -> (None, False)."""
    s = (raw or "").strip().lower()
    if not s:
        return None, False
    estimate = s.startswith("approx") or s.startswith("~")
    m = re.search(r"([\d.,]+)\s*([mk])?", s)
    if not m:
        return None, False
    try:
        value = float(m.group(1).replace(",", ""))
    except ValueError:
        return None, False
    mult = {"m": 1e6, "k": 1e3}.get(m.group(2) or "", 1)
    return value * mult, estimate


def crm_date(raw: str | None) -> date | None:
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d-%b-%Y"):
        try:
            return datetime.strptime((raw or "").strip(), fmt).date()
        except ValueError:
            continue
    return None


def timestamp_utc(raw) -> datetime | None:
    """Epoch milliseconds, '2026-03-04 22:22:00Z' or ISO with an offset -> aware UTC datetime."""
    if raw is None or raw == "":
        return None
    if isinstance(raw, (int, float)):
        return datetime.fromtimestamp(raw / 1000, tz=timezone.utc)
    s = str(raw).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None  # ambiguous: refuse to guess the time zone
    return dt.astimezone(timezone.utc)


def domain_of(email_or_url: str | None) -> str | None:
    s = (email_or_url or "").strip().lower()
    if not s:
        return None
    s = s.split("@")[-1]
    s = re.sub(r"^https?://", "", s)
    s = s.removeprefix("www.")
    return s.split("/")[0] or None


PERSONAL_DOMAINS = {"gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "icloud.com"}
