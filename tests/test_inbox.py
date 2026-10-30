"""External analyst queue, inbox validation, expiry, and the Lagos desk clock."""
import io
import json
import os
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config                                                    # noqa: E402
import run_crypto                                                # noqa: E402
from analyst_inbox import InboxError, main, parse_thesis, submit_thesis  # noqa: E402
from desk import queue                                           # noqa: E402
from desk.broker import PaperBroker                              # noqa: E402
from desk.ops import Bench                                       # noqa: E402
from desk.queue import enqueue, list_pending, load, resolve_analyst_mode  # noqa: E402
from desk.risk import Account, check_token_trade, in_quiet_hours  # noqa: E402
from desk import analyst                                         # noqa: E402

NOON = datetime(2026, 10, 26, 12, 0)
UTC = timezone.utc
FACTS = {"mint_authority": None, "freeze_authority": None, "supply": 1_000_000_000,
         "top_wallet_pct": 0.02, "top_10_pct": 0.1, "program_held_pct": 0.7,
         "wallets_counted": 4}
LINKS = {"x": None, "telegram": "https://t.me/test", "website": None}


def scanner(**kw):
    t = {"mint": "MINT", "symbol": "TEST", "age_minutes": 45, "liquidity_usd": 30_000,
         "volume_h1": 60_000, "mcap_usd": 200_000, "buys_h1": 400, "sells_h1": 250,
         "price_usd": 0.0002, "dex": "pump-fun"}
    t.update(kw)
    return t


def thesis(**kw):
    base = dict(verdict="valid_setup", score=8, red_flags=["thin socials"],
                green_flags=["crowd"], shape="crowd", invalidation="buyers stall",
                sources=["buyers_h1"])
    base.update(kw)
    return base


class Sandbox(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._journal = queue_journal()
        self._queue = queue.QUEUE_PATH
        import desk.ops as ops
        self._ops = ops
        ops.JOURNAL = self.root / "journal.jsonl"
        queue.QUEUE_PATH = self.root / "analyst_queue.jsonl"
        self._env = {
            key: os.environ.pop(key, None)
            for key in ("ANTHROPIC_API_KEY", "ANALYST_MODE",
                        "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")
        }

    def tearDown(self):
        self._ops.JOURNAL = self._journal
        queue.QUEUE_PATH = self._queue
        for key, val in self._env.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val
        self.tmp.cleanup()

    def broker(self):
        return PaperBroker(self.root / "ledger.json")

    def bench(self):
        return Bench(str(self.root / "desk.db"))

    def kinds(self):
        path = self._ops.JOURNAL
        if not path.exists():
            return []
        return [json.loads(line)["kind"] for line in path.read_text().splitlines() if line.strip()]


def queue_journal():
    import desk.ops as ops
    return ops.JOURNAL


class QueueAndInbox(Sandbox):
    def test_enqueue_shape(self):
        when = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
        rec = enqueue(scanner(), FACTS, LINKS, now=when)
        self.assertEqual(rec["token"], "MINT")
        self.assertEqual(rec["symbol"], "TEST")
        self.assertEqual(rec["scanner"]["liquidity_usd"], 30_000)
        self.assertEqual(rec["chain"]["top_wallet_pct"], 0.02)
        self.assertEqual(rec["links"]["telegram"], LINKS["telegram"])
        self.assertEqual(rec["queued_at"], "2026-10-30T12:00:00+00:00")
        self.assertEqual(rec["status"], "pending")
        self.assertEqual(load()[0]["token"], "MINT")

    def test_list_is_oldest_pending_only(self):
        base = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
        enqueue(scanner(mint="OLD", symbol="OLD"), FACTS, LINKS, now=base)
        enqueue(scanner(mint="NEW", symbol="NEW"), FACTS, LINKS, now=base + timedelta(minutes=1))
        rows = list_pending(now=base + timedelta(minutes=2))
        self.assertEqual([r["token"] for r in rows], ["OLD", "NEW"])

    def test_ttl_boundary_and_config(self):
        base = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
        enqueue(scanner(), FACTS, LINKS, now=base)
        still = list_pending(now=base + timedelta(minutes=29, seconds=59))
        self.assertEqual(len(still), 1)
        self.assertEqual(list_pending(now=base + timedelta(minutes=30)), [])
        self.assertEqual(load()[0]["status"], "expired")
        self.assertIn("expired_at", load()[0])

        enqueue(scanner(mint="SHORT"), FACTS, LINKS, now=base)
        with patch.object(config, "ANALYST_QUEUE_TTL_MINUTES", 5):
            gone = list_pending(now=base + timedelta(minutes=5))
        self.assertNotIn("SHORT", [r["token"] for r in gone])
        short = next(r for r in load() if r["token"] == "SHORT")
        self.assertEqual(short["status"], "expired")

    def test_submit_paper_fill_and_done(self):
        enqueue(scanner(), FACTS, LINKS, now=NOON)
        broker, bench = self.broker(), self.bench()
        decision = submit_thesis("MINT", thesis(), now=NOON, broker=broker, bench=bench)
        self.assertTrue(decision.approved)
        self.assertEqual(decision.ticket, 20.0)
        self.assertEqual(broker.open_count(), 1)
        self.assertEqual(load()[0]["status"], "done")
        self.assertIn("done_at", load()[0])
        self.assertEqual(self.kinds(), ["decision", "entry"])

    def test_submit_clamps_score_like_claude(self):
        enqueue(scanner(), FACTS, LINKS, now=NOON)
        decision = submit_thesis("MINT", thesis(score=14), now=NOON,
                                 broker=self.broker(), bench=self.bench())
        self.assertEqual(decision.score, 10)

    def test_submit_skip_and_watch_do_not_trade(self):
        for verdict in ("skip", "watch"):
            with self.subTest(verdict=verdict):
                enqueue(scanner(mint=verdict), FACTS, LINKS, now=NOON)
                broker = self.broker()
                decision = submit_thesis(verdict, thesis(verdict=verdict, score=3), now=NOON,
                                         broker=broker, bench=self.bench())
                self.assertFalse(decision.approved)
                self.assertEqual(broker.open_count(), 0)
                self.assertEqual(decision.bucket, "analyst")
        done = {r["token"]: r["status"] for r in load()}
        self.assertEqual(done["skip"], "done")
        self.assertEqual(done["watch"], "done")

    def test_shadow_does_not_fill(self):
        enqueue(scanner(), FACTS, LINKS, now=NOON)
        broker = self.broker()
        decision = submit_thesis("MINT", thesis(), shadow=True, now=NOON,
                                 broker=broker, bench=self.bench())
        self.assertTrue(decision.approved)
        self.assertEqual(broker.open_count(), 0)
        self.assertNotIn("entry", self.kinds())
        self.assertEqual(load()[0]["status"], "done")

    def test_expired_is_skipped_and_not_traded(self):
        base = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
        enqueue(scanner(), FACTS, LINKS, now=base)
        broker = self.broker()
        with self.assertRaises(InboxError) as caught:
            submit_thesis("MINT", thesis(), now=base + timedelta(minutes=31),
                          broker=broker, bench=self.bench())
        self.assertIn("expired", str(caught.exception))
        self.assertIn("skipped", str(caught.exception))
        self.assertEqual(broker.open_count(), 0)
        self.assertEqual(load()[0]["status"], "expired")
        self.assertNotIn("decision", self.kinds())

    def test_buy_and_bad_shape_rejected_without_consuming_the_row(self):
        enqueue(scanner(), FACTS, LINKS, now=NOON)
        with self.assertRaises(InboxError) as bought:
            parse_thesis(thesis(verdict="buy"))
        self.assertIn("buy", str(bought.exception))
        with self.assertRaises(InboxError):
            submit_thesis("MINT", thesis(verdict="Buy"), now=NOON,
                          broker=self.broker(), bench=self.bench())
        with self.assertRaises(InboxError):
            submit_thesis("MINT", thesis(shape="moon"), now=NOON,
                          broker=self.broker(), bench=self.bench())
        with self.assertRaises(InboxError):
            submit_thesis("MINT", {"verdict": "skip"}, now=NOON,
                          broker=self.broker(), bench=self.bench())
        self.assertEqual(load()[0]["status"], "pending")

    def test_missing_token(self):
        with self.assertRaises(InboxError) as caught:
            submit_thesis("NOPE", thesis(), now=NOON, broker=self.broker(), bench=self.bench())
        self.assertIn("no pending", str(caught.exception))

    def test_queued_bench_follows_ttl(self):
        bench = self.bench()
        start = time.time()
        bench.sit("MINT", "queued")
        until = bench.db.execute("SELECT until FROM bench WHERE mint=?", ("MINT",)).fetchone()[0]
        self.assertAlmostEqual(until - start, config.ANALYST_QUEUE_TTL_MINUTES * 60, delta=2)
        with patch.object(config, "ANALYST_QUEUE_TTL_MINUTES", 5):
            bench.sit("SHORT", "queued")
            until = bench.db.execute("SELECT until FROM bench WHERE mint=?", ("SHORT",)).fetchone()[0]
        self.assertAlmostEqual(until - time.time(), 5 * 60, delta=2)

    def test_cli_list_and_submit(self):
        enqueue(scanner(), FACTS, LINKS, now=datetime.now(UTC))
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(main(["list"]), 0)
        self.assertIn("MINT", out.getvalue())
        self.assertIn("TEST", out.getvalue())
        self.assertIn("pending 1", out.getvalue())

        raw = io.StringIO()
        with redirect_stdout(raw):
            self.assertEqual(main(["list", "--json"]), 0)
        payload = json.loads(raw.getvalue())
        self.assertEqual(payload[0]["token"], "MINT")
        self.assertIn("scanner", payload[0])
        self.assertIn("chain", payload[0])

        path = self.root / "thesis.json"
        path.write_text(json.dumps(thesis(verdict="skip", score=2)))
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err), \
                patch("analyst_inbox.PaperBroker", lambda *a, **k: self.broker()), \
                patch("analyst_inbox.Bench", lambda *a, **k: self.bench()):
            rc = main(["submit", "MINT", str(path), "--shadow"])
        self.assertEqual(rc, 0, err.getvalue())
        self.assertIn("skip", out.getvalue())
        self.assertIn("shadow", out.getvalue())
        self.assertEqual(load()[0]["status"], "done")

    def test_cli_stdin_and_errors(self):
        enqueue(scanner(), FACTS, LINKS, now=datetime.now(UTC))
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(main(["submit", "MINT", str(self.root / "missing.json")]), 1)
        path = self.root / "bad.json"
        path.write_text("{not json")
        with redirect_stderr(err):
            self.assertEqual(main(["submit", "MINT", str(path)]), 1)
        self.assertIn("not valid JSON", err.getvalue())
        self.assertEqual(load()[0]["status"], "pending")

        out, err = io.StringIO(), io.StringIO()
        stdin = io.StringIO(json.dumps(thesis(verdict="watch", score=4)))
        with redirect_stdout(out), redirect_stderr(err), patch("sys.stdin", stdin), \
                patch("analyst_inbox.PaperBroker", lambda *a, **k: self.broker()), \
                patch("analyst_inbox.Bench", lambda *a, **k: self.bench()):
            rc = main(["submit", "MINT", "-"])
        self.assertEqual(rc, 0, err.getvalue())
        self.assertIn("watch", out.getvalue())
        self.assertEqual(load()[0]["status"], "done")

        nope = self.root / "nope.json"
        nope.write_text(json.dumps(thesis(verdict="skip", score=1)))
        with redirect_stderr(err):
            self.assertEqual(main(["submit", "NONE", str(nope)]), 1)
        self.assertIn("no pending", err.getvalue())


class AnalystMode(Sandbox):
    def test_auto_follows_the_key(self):
        self.assertEqual(resolve_analyst_mode(), "external")
        os.environ["ANTHROPIC_API_KEY"] = ""
        self.assertEqual(resolve_analyst_mode(), "external")
        os.environ["ANTHROPIC_API_KEY"] = "sk-test"
        self.assertEqual(resolve_analyst_mode(), "claude")

    def test_flag_forces_either_mode(self):
        os.environ["ANTHROPIC_API_KEY"] = "sk-test"
        os.environ["ANALYST_MODE"] = "external"
        self.assertEqual(resolve_analyst_mode(), "external")
        os.environ.pop("ANTHROPIC_API_KEY")
        os.environ["ANALYST_MODE"] = "claude"
        self.assertEqual(resolve_analyst_mode(), "claude")
        os.environ.pop("ANALYST_MODE")
        with patch.object(config, "ANALYST_MODE", "external"):
            os.environ["ANTHROPIC_API_KEY"] = "sk-test"
            self.assertEqual(resolve_analyst_mode(), "external")
        os.environ["ANALYST_MODE"] = "grok"
        with self.assertRaises(ValueError):
            resolve_analyst_mode()

    def _cycle(self, tokens, analyze):
        with patch.object(run_crypto.scanner, "new_tokens", return_value=tokens), \
                patch.object(run_crypto.scanner, "socials", return_value=LINKS), \
                patch.object(run_crypto.chain, "facts", return_value=FACTS), \
                patch.object(run_crypto.analyst, "analyze_token", side_effect=analyze) as mocked:
            stats = run_crypto.cycle(self.broker(), self.bench(), object(), shadow=True)
        return stats, mocked

    def test_external_cycle_queues_survivors_and_skips_claude(self):
        called = []

        def analyze(*args, **kwargs):
            called.append(args)
            raise RuntimeError("claude should not be called")

        stats, _ = self._cycle([scanner()], analyze)
        self.assertEqual(called, [])
        self.assertEqual(stats["queued"], 1)
        self.assertEqual(stats["analyst"], 0)
        row = load()[0]
        self.assertEqual(row["token"], "MINT")
        self.assertEqual(row["symbol"], "TEST")
        self.assertEqual(row["chain"]["top_10_pct"], 0.1)
        self.assertEqual(row["links"], LINKS)
        self.assertEqual(row["status"], "pending")
        self.assertIn("queued", self.kinds())
        self.assertNotIn("error", self.kinds())

    def test_free_kill_and_chain_kill_never_queue(self):
        called = []

        def analyze(*args, **kwargs):
            called.append(1)
            return analyst.TokenThesis(**thesis())

        stats, _ = self._cycle([scanner(age_minutes=3)], analyze)
        self.assertEqual(stats["free"], 1)
        self.assertEqual(stats["queued"], 0)
        self.assertEqual(load(), [])
        self.assertEqual(called, [])

        bad = dict(FACTS, mint_authority="printer")
        with patch.object(run_crypto.scanner, "new_tokens", return_value=[scanner(mint="RUGGED")]), \
                patch.object(run_crypto.scanner, "socials", return_value=LINKS), \
                patch.object(run_crypto.chain, "facts", return_value=bad), \
                patch.object(run_crypto.analyst, "analyze_token", side_effect=analyze):
            stats = run_crypto.cycle(self.broker(), self.bench(), object(), shadow=True)
        self.assertEqual(stats["chain"], 1)
        self.assertEqual(stats["queued"], 0)
        self.assertEqual(called, [])

    def test_claude_mode_still_calls_analyze(self):
        os.environ["ANTHROPIC_API_KEY"] = "sk-test"
        os.environ["ANALYST_MODE"] = "claude"
        produced = analyst.TokenThesis(**thesis())
        stats, mocked = self._cycle([scanner()], lambda *a, **k: produced)
        self.assertEqual(mocked.call_count, 1)
        self.assertEqual(stats["queued"], 0)
        self.assertEqual(load(), [])
        self.assertIn("decision", self.kinds())


class Timezone(Sandbox):
    def test_default_is_lagos(self):
        self.assertEqual(config.TIMEZONE, "Africa/Lagos")
        lagos = datetime(2026, 10, 26, 23, 30, tzinfo=UTC).astimezone(ZoneInfo("Africa/Lagos"))
        self.assertEqual((lagos.date().isoformat(), lagos.hour), ("2026-10-27", 0))

    def test_quiet_hours_use_the_desk_zone(self):
        just_after_midnight_utc = datetime(2026, 10, 26, 0, 30, tzinfo=UTC)   # 01:30 Lagos
        still_quiet_in_utc = datetime(2026, 10, 26, 6, 30, tzinfo=UTC)        # 07:30 Lagos
        self.assertTrue(in_quiet_hours(just_after_midnight_utc))
        self.assertFalse(in_quiet_hours(still_quiet_in_utc))
        self.assertTrue(in_quiet_hours(datetime(2026, 10, 26, 2, 30)))
        self.assertFalse(in_quiet_hours(datetime(2026, 10, 26, 12, 0)))
        with patch.object(config, "TIMEZONE", "UTC"):
            self.assertFalse(in_quiet_hours(just_after_midnight_utc))
            self.assertTrue(in_quiet_hours(still_quiet_in_utc))

    def test_daily_loss_day_rolls_on_lagos_midnight(self):
        broker = self.broker()
        broker.s["day"] = "2026-10-26"
        broker.s["day_start"] = 1_000
        broker.s["cash"] = 960
        broker._roll_day(datetime(2026, 10, 26, 22, 30, tzinfo=UTC))  # 23:30 Lagos, same day
        self.assertEqual(broker.s["day"], "2026-10-26")
        self.assertEqual(broker.day_pnl(), -40)
        halted = Account(broker.equity(), broker.day_pnl(), 0)
        reason = check_token_trade(analyst.TokenThesis(**thesis()), halted, 30_000, now=NOON)[2]
        self.assertIn("daily loss", reason)

        broker._roll_day(datetime(2026, 10, 26, 23, 30, tzinfo=UTC))  # 00:30 Lagos, next day
        self.assertEqual(broker.s["day"], "2026-10-27")
        self.assertEqual(broker.day_pnl(), 0)

        fresh = self.broker()
        fresh.s["day"] = "2026-10-26"
        fresh.s["day_start"] = fresh.equity()
        with patch.object(config, "TIMEZONE", "UTC"):
            fresh._roll_day(datetime(2026, 10, 26, 23, 30, tzinfo=UTC))
        self.assertEqual(fresh.s["day"], "2026-10-26")


if __name__ == "__main__":
    unittest.main()
