"""External analyst inbox. List the queue, or submit a thesis for one token.

    python analyst_inbox.py list
    python analyst_inbox.py submit <TOKEN_ADDRESS> thesis.json
    python analyst_inbox.py submit <TOKEN_ADDRESS> -     # JSON on stdin
    python analyst_inbox.py submit <TOKEN_ADDRESS> thesis.json --shadow

The thesis is a TokenThesis: verdict skip, watch, or valid_setup. Never buy.
A valid thesis takes the same risk, journal, alert, and paper path as Claude.
Expired queue rows are marked expired and skipped.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError

from desk.analyst import TokenThesis
from desk.broker import PaperBroker
from desk.ops import Bench, load_env
from desk.pipeline import Decision, apply_thesis
from desk.queue import QueueError, claim, format_pending, list_pending, mark_done


class InboxError(Exception):
    pass


def parse_thesis(payload: object) -> TokenThesis:
    """Validate against TokenThesis. Verdict buy is rejected. Score is clamped 0-10."""
    if not isinstance(payload, dict):
        raise InboxError("thesis must be a JSON object")
    verdict = payload.get("verdict")
    if isinstance(verdict, str) and verdict.strip().lower() == "buy":
        raise InboxError('verdict "buy" is not allowed; use skip, watch, or valid_setup')
    try:
        thesis = TokenThesis.model_validate(payload)
    except ValidationError as exc:
        raise InboxError(f"invalid thesis: {exc}") from exc
    thesis.score = max(0, min(10, int(thesis.score)))
    return thesis


def read_payload(file: str | None) -> object:
    try:
        if file is None or file == "-":
            raw = sys.stdin.read()
        else:
            raw = Path(file).read_text(encoding="utf-8")
    except OSError as exc:
        raise InboxError(f"could not read thesis: {exc}") from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InboxError(f"thesis is not valid JSON: {exc}") from exc


def submit_thesis(token: str, payload: object, *, shadow: bool = False,
                  now: datetime | None = None, queue_path=None,
                  broker: PaperBroker | None = None,
                  bench: Bench | None = None) -> Decision:
    thesis = parse_thesis(payload)
    try:
        record = claim(token, path=queue_path, now=now)
    except QueueError as exc:
        raise InboxError(str(exc)) from exc
    broker = broker if broker is not None else PaperBroker()
    bench = bench if bench is not None else Bench()
    decision = apply_thesis(
        broker, bench, record["scanner"], record["chain"], record.get("links") or {},
        thesis, shadow, now=now)
    mark_done(token, record["queued_at"], path=queue_path, now=now)
    return decision


def format_decision(decision: Decision) -> str:
    state = "approved" if decision.approved else "rejected"
    detail = f"ticket ${decision.ticket:.2f}" if decision.approved else decision.reason
    shadow = " | shadow" if decision.shadow else ""
    return (f"{decision.symbol} {decision.verdict} score {decision.score} "
            f"| {state} | {detail}{shadow}")


def main(argv: list[str] | None = None) -> int:
    load_env()
    ap = argparse.ArgumentParser(description="External analyst inbox")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="show pending queued tokens")
    p_list.add_argument("--json", action="store_true", help="print the pending records as JSON")

    p_sub = sub.add_parser("submit", help="submit a thesis for one queued token")
    p_sub.add_argument("token", help="token address (mint) of a pending queue row")
    p_sub.add_argument("file", nargs="?", default=None,
                       help="thesis JSON file; omit or pass - to read stdin")
    p_sub.add_argument("--shadow", action="store_true",
                       help="journal and alert only, no paper fill")

    args = ap.parse_args(argv)
    try:
        if args.cmd == "list":
            rows = list_pending()
            if args.json:
                print(json.dumps(rows, indent=1, default=str))
            else:
                print(format_pending(rows))
            return 0
        decision = submit_thesis(args.token, read_payload(args.file), shadow=args.shadow)
    except InboxError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(format_decision(decision))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
