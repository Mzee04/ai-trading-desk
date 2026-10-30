"""External analyst queue.

When the desk is not calling Claude, each scanner + chain survivor is appended
here. A human (or another model, outside this process) writes a TokenThesis
later. The file is JSONL so a crash keeps every row already written.

One record::

    {"token": "<address>", "symbol": "DOGE", "scanner": {...}, "chain": {...},
     "links": {...}, "queued_at": "2026-10-30T13:47:00+00:00", "status": "pending"}

status is pending, done, or expired.
"""
from __future__ import annotations

import fcntl
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config

QUEUE_PATH = Path("analyst_queue.jsonl")


class QueueError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def resolve_analyst_mode() -> str:
    """Which seat scores survivors.

    auto: Claude when ANTHROPIC_API_KEY is set, otherwise this queue.
    claude / external: force that mode. ANALYST_MODE in the environment wins
    over the config default so a .env can force a seat without editing thresholds.
    """
    raw = (os.getenv("ANALYST_MODE") or config.ANALYST_MODE or "auto").strip().lower()
    if raw == "claude":
        return "claude"
    if raw == "external":
        return "external"
    if raw != "auto":
        raise ValueError(
            f"ANALYST_MODE must be auto, claude, or external (got {raw!r})")
    return "claude" if os.getenv("ANTHROPIC_API_KEY") else "external"


def _path(path: Path | str | None) -> Path:
    return QUEUE_PATH if path is None else Path(path)


def _parse_time(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _utcnow(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def _expire(records: list[dict], now: datetime | None = None) -> int:
    """Mark pending rows older than the configured TTL. Returns how many flipped."""
    moment = _utcnow(now)
    ttl = timedelta(minutes=config.ANALYST_QUEUE_TTL_MINUTES)
    flipped = 0
    for rec in records:
        if rec.get("status") != "pending":
            continue
        if moment - _parse_time(rec["queued_at"]) >= ttl:
            rec["status"] = "expired"
            rec["expired_at"] = moment.isoformat()
            flipped += 1
    return flipped


def _locked(path: Path | str | None, fn) -> None:
    target = _path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.touch(exist_ok=True)
    with target.open("r+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            records: list[dict] = []
            for lineno, line in enumerate(handle.read().splitlines(), 1):
                if not line.strip():
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    raise ValueError(f"{target}:{lineno}: invalid queue line") from exc
            fn(records)
            handle.seek(0)
            handle.truncate()
            for rec in records:
                handle.write(json.dumps(rec, default=str) + "\n")
            handle.flush()
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def load(path: Path | str | None = None) -> list[dict]:
    target = _path(path)
    if not target.exists():
        return []
    return [json.loads(line) for line in target.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def enqueue(scanner: dict, chain: dict, links: dict | None = None, *,
            path: Path | str | None = None, now: datetime | None = None) -> dict:
    """Append one chain-check survivor. `token` is the mint address."""
    moment = _utcnow(now)
    rec = {
        "token": scanner["mint"],
        "symbol": scanner.get("symbol"),
        "scanner": scanner,
        "chain": chain,
        "links": links or {},
        "queued_at": moment.isoformat(),
        "status": "pending",
    }
    def add(records: list[dict]) -> None:
        records.append(rec)
    _locked(path, add)
    return rec


def list_pending(path: Path | str | None = None, now: datetime | None = None) -> list[dict]:
    """Expire stale rows, then return what is still pending, oldest first."""
    found: dict = {"rows": []}
    def collect(records: list[dict]) -> None:
        _expire(records, now)
        rows = [r for r in records if r.get("status") == "pending"]
        rows.sort(key=lambda r: r["queued_at"])
        found["rows"] = rows
    _locked(path, collect)
    return found["rows"]


def format_pending(rows: list[dict], now: datetime | None = None) -> str:
    if not rows:
        return "no pending tokens"
    moment = _utcnow(now)
    lines = [f"pending {len(rows)}", "TOKEN  SYMBOL  QUEUED_AT  AGE_MIN"]
    for rec in rows:
        age = int((moment - _parse_time(rec["queued_at"])).total_seconds() // 60)
        lines.append(f"{rec['token']}  {rec['symbol']}  {rec['queued_at']}  {age}")
    return "\n".join(lines)


def claim(token: str, path: Path | str | None = None, now: datetime | None = None) -> dict:
    """Oldest fresh pending row for this address.

    Expired rows are marked and then refused. Nothing here calls the risk engine.
    """
    found: dict = {}
    def take(records: list[dict]) -> None:
        _expire(records, now)
        pending = [r for r in records if r.get("token") == token and r.get("status") == "pending"]
        pending.sort(key=lambda r: r["queued_at"])
        if pending:
            found["rec"] = pending[0]
            return
        expired = [r for r in records if r.get("token") == token and r.get("status") == "expired"]
        if expired:
            found["error"] = QueueError(
                "expired",
                f"{token} queued at {expired[-1]['queued_at']} has expired; skipped")
            return
        found["error"] = QueueError("missing", f"no pending queue item for {token}")
    _locked(path, take)
    if "error" in found:
        raise found["error"]
    return found["rec"]


def mark_done(token: str, queued_at: str, path: Path | str | None = None,
              now: datetime | None = None) -> None:
    moment = _utcnow(now)
    def finish(records: list[dict]) -> None:
        for rec in records:
            if (rec.get("token") == token and rec.get("queued_at") == queued_at
                    and rec.get("status") == "pending"):
                rec["status"] = "done"
                rec["done_at"] = moment.isoformat()
    _locked(path, finish)
