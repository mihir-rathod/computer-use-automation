from __future__ import annotations

from datetime import date, datetime


def money(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    return f"{sign}${abs(cents) / 100:,.2f}"


def fmt_date(iso: str) -> str:
    return date.fromisoformat(iso[:10]).strftime("%a %d %b %Y")


def fmt_dt(iso: str) -> str:
    return datetime.fromisoformat(iso).strftime("%a %d %b %Y, %I:%M %p")


def fmt_time(iso: str) -> str:
    return datetime.fromisoformat(iso).strftime("%I:%M %p")
