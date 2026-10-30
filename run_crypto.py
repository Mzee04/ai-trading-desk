"""The shift: one loop that runs the five seats in order, every minute, forever.

    python run_crypto.py            paper trading (default)
    python run_crypto.py --shadow   journal + alerts only, no paper fills
    python run_crypto.py --once     one cycle, then exit (good for testing)

Each pass is more expensive than the one before it, so each one has to kill harder:
  scanner (free) -> chain facts (RPC) -> analyst -> risk engine (code) -> paper fill

The analyst seat is Claude when ANTHROPIC_API_KEY is set. Otherwise (or when
ANALYST_MODE=external) survivors are appended to analyst_queue.jsonl and scored
later with analyst_inbox.py. The risk engine does not change either way.
"""
from __future__ import annotations

import argparse
import time

import requests

from config import CYCLE_SECONDS
from desk import analyst, chain, scanner
from desk.broker import PaperBroker
from desk.ops import Bench, alert, journal, killed, load_env
from desk.pipeline import apply_thesis
from desk.queue import enqueue, resolve_analyst_mode

CHAIN_BUDGET = 8      # RPC fact checks per cycle
ANALYST_BUDGET = 3    # Claude calls per cycle: caps the bill no matter what the market does


def manage_positions(broker: PaperBroker, bench: Bench, s: requests.Session):
    for mint in list(broker.s["positions"]):
        sig = broker.exit_signal(mint, scanner.quote(mint, s))
        if sig:
            fraction, reason = sig
            price = broker.s["positions"][mint]["last"]
            fill = broker.sell(mint, fraction, price, reason)
            journal("exit", **fill)
            if "pnl_usd" in fill:
                bench.sit(mint, "exited")                     # no revenge re-entry
            alert(f"EXIT {fill['symbol']} {int(fraction * 100)}% | {reason}"
                  + (f" | pnl ${fill['pnl_usd']}" if "pnl_usd" in fill else ""))


def cycle(broker: PaperBroker, bench: Bench, s: requests.Session, shadow: bool) -> dict:
    stats = {"seen": 0, "benched": 0, "free": 0, "chain": 0, "analyst": 0, "risk": 0,
             "entries": 0, "queued": 0}
    manage_positions(broker, bench, s)

    chain_left, analyst_left = CHAIN_BUDGET, ANALYST_BUDGET
    tokens = scanner.new_tokens(s)
    tokens.sort(key=lambda t: t["volume_h1"] / max(t["mcap_usd"], 1), reverse=True)

    for t in tokens:
        stats["seen"] += 1
        if t["mint"] in broker.s["positions"] or bench.benched(t["mint"]):
            stats["benched"] += 1
            continue

        if (k := scanner.free_kill(t)):                       # pass 1: free
            bench.sit(t["mint"], k)
            stats["free"] += 1
            continue
        if chain_left <= 0 or analyst_left <= 0:
            break                                             # out of budget, not out of ideas

        try:                                                  # pass 2: chain facts
            f = chain.facts(t["mint"], s)
        except Exception as e:
            journal("error", stage="chain", mint=t["mint"], error=str(e))
            continue                                          # missing is missing, not a pass
        chain_left -= 1
        if (k := chain.chain_kill(f)):
            bench.sit(t["mint"], k)
            journal("reject", stage="chain", mint=t["mint"], symbol=t["symbol"], reason=k, facts=f)
            stats["chain"] += 1
            continue

        links = scanner.socials(t["mint"], s)                 # pass 3: analyst
        if resolve_analyst_mode() == "external":
            try:
                rec = enqueue(t, f, links)
            except Exception as e:
                journal("error", stage="analyst", mint=t["mint"], error=str(e))
                continue
            analyst_left -= 1
            bench.sit(t["mint"], "queued")
            journal("queued", mint=t["mint"], symbol=t["symbol"], queued_at=rec["queued_at"])
            stats["queued"] += 1
            continue

        try:                                                  # Claude, unchanged
            th = analyst.analyze_token(t, f, links)
        except Exception as e:
            journal("error", stage="analyst", mint=t["mint"], error=str(e))
            continue
        analyst_left -= 1
        decision = apply_thesis(broker, bench, t, f, links, th, shadow)   # pass 4 + paper
        stats[decision.bucket] += 1

    journal("cycle", **stats, equity=broker.equity(), day_pnl=broker.day_pnl())
    return stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--shadow", action="store_true")
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()
    load_env()
    broker, bench, s = PaperBroker(), Bench(), requests.Session()
    while True:
        if killed():
            for mint in list(broker.s["positions"]):
                fill = broker.sell(mint, 1.0, broker.s["positions"][mint]["last"], "kill switch")
                journal("exit", **fill)
            alert("KILL file found. All paper positions closed. Desk stopped.")
            return
        try:
            stats = cycle(broker, bench, s, args.shadow)
            print(f"cycle {stats} equity ${broker.equity()}")
        except Exception as e:
            journal("error", stage="cycle", error=str(e))
            print(f"cycle failed: {e}")
        if args.once:
            return
        time.sleep(CYCLE_SECONDS)


if __name__ == "__main__":
    main()
