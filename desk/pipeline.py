"""After a thesis exists, the path is the same whoever wrote it.

Claude and the external inbox both land here. Sizing stays in the risk engine.
This module does not decide whether a trade is allowed; it only records the
decision, alerts, and (unless shadowing) paper-fills.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from desk.ops import alert, journal
from desk.risk import Account, check_token_trade


@dataclass
class Decision:
    bucket: str
    approved: bool
    ticket: float
    reason: str
    shadow: bool
    symbol: str
    verdict: str
    score: int


def apply_thesis(broker, bench, token: dict, facts: dict, links: dict, thesis,
                 shadow: bool, now: datetime | None = None) -> Decision:
    """Risk, journal, alert, paper fill. Same steps the Claude cycle uses."""
    acct = Account(equity=broker.equity(), day_pnl=broker.day_pnl(),
                   open_positions=broker.open_count())
    ok, ticket, reason = check_token_trade(thesis, acct, token["liquidity_usd"], now=now)
    journal("decision", mint=token["mint"], symbol=token["symbol"], token=token, facts=facts,
            links=links, thesis=thesis.model_dump(), approved=ok, ticket_usd=ticket,
            reason=reason, shadow=shadow)
    if not ok:
        bench.sit(token["mint"], thesis.verdict if thesis.verdict != "valid_setup" else "score")
        bucket = "analyst" if thesis.verdict != "valid_setup" else "risk"
    else:
        flags = "; ".join(thesis.red_flags[:2]) or "none"
        alert(f"VALID SETUP ${token['symbol']} score {thesis.score}/10 | ticket ${ticket}\n"
              f"mcap ${token['mcap_usd']:,.0f} liq ${token['liquidity_usd']:,.0f} "
              f"top10 {facts['top_10_pct']:.1%}\nred flags: {flags}\n"
              f"invalidation: {thesis.invalidation}\n"
              f"https://pump.fun/coin/{token['mint']}\n"
              f"https://dexscreener.com/solana/{token['mint']}")
        if not shadow:
            fill = broker.buy(token["mint"], token["symbol"], ticket, token["price_usd"])
            journal("entry", **fill)
        bucket = "entries"
    return Decision(bucket=bucket, approved=ok, ticket=ticket, reason=reason, shadow=shadow,
                    symbol=token.get("symbol") or "", verdict=thesis.verdict, score=thesis.score)
