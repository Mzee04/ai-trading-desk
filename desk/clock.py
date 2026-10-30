"""Desk clock. Quiet hours and the daily-loss day use config.TIMEZONE.

Africa/Lagos is the default. A naive datetime is treated as a wall clock in that
zone (so a test that passes 02:30 means 02:30 in Lagos). An aware datetime is
converted into the zone.
"""
from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import config


def desk_zone() -> ZoneInfo:
    return ZoneInfo(config.TIMEZONE)


def desk_now(now: datetime | None = None) -> datetime:
    zone = desk_zone()
    if now is None:
        return datetime.now(zone)
    if now.tzinfo is None:
        return now.replace(tzinfo=zone)
    return now.astimezone(zone)


def desk_today(now: datetime | None = None) -> date:
    return desk_now(now).date()
