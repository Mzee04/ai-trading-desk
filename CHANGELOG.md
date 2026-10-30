# Changelog

## Unreleased

- External analyst: with no `ANTHROPIC_API_KEY`, or `ANALYST_MODE=external`, chain-check survivors are appended to `analyst_queue.jsonl` instead of calling Claude. `ANALYST_MODE=claude` forces Claude.
- `analyst_inbox.py` lists the queue and submits a `TokenThesis` (skip / watch / valid_setup, never buy) through the same risk, journal, alert, and paper path. Rows older than `ANALYST_QUEUE_TTL_MINUTES` (30) are marked expired and skipped.
- Desk clock defaults to `Africa/Lagos`. Quiet hours and the daily-loss day use that zone. Risk thresholds are unchanged.

## v1.0.0 · 2026-10-26

- Five seats: scanner, chain check, Claude analyst, risk engine, paper execution
- Real-wallet concentration: bonding curve and pool accounts excluded by owner program
- Typed `TokenThesis` and `StockThesis` via structured outputs, untrusted-input wrapping
- Rule-based exits: -35% stop, half at 2x, volume decay, 24h time out, 6h no-revenge bench
- Stocks mode on Alpaca paper with OTO stop orders
- Journal, Telegram alerts, kill switch, weekly review
- 17 offline tests, CI on Python 3.10 to 3.13
