from datetime import datetime, timezone, timedelta

FMT = "%Y-%m-%dT%H:%M:%S.%fZ"

T0 = datetime(2026, 10, 12, 9, 0, 0, tzinfo=timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime(FMT)


def parse_iso(s: str) -> datetime:
    return datetime.strptime(s, FMT).replace(tzinfo=timezone.utc)


def to_wat(dt: datetime) -> datetime:
    return dt.astimezone(timezone(timedelta(hours=1)))
