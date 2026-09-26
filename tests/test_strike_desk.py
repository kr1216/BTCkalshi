import importlib
import math
import os
import tempfile
import unittest

from strike_desk import model


class ModelTest(unittest.TestCase):
    def test_fee_matches_kalshi_schedule(self):
        self.assertEqual(model.fee(0.50), 0.02)   # 1.75¢ rounds up
        self.assertEqual(model.fee(0.97), 0.01)
        self.assertEqual(model.fee(0.10), 0.01)   # 0.63¢ rounds up

    def test_at_the_money_is_a_coin_flip(self):
        self.assertAlmostEqual(model.fair_up(100, 100, 600, 0.05).p, 0.5)

    def test_variance_includes_settlement_average(self):
        f = model.fair_up(100, 100, 300, 0.1)
        self.assertAlmostEqual(f.sd, 0.1 * math.sqrt(240 + 20))

    def test_final_minute_uses_observed_average(self):
        # 30 s left, first half averaged 101: the mean is pulled halfway to it
        f = model.fair_up(100, 100.4, 30, 0.1, obs_avg=101)
        self.assertAlmostEqual(f.mean, 100.5)
        self.assertGreater(f.p, 0.5)

    def test_zero_time_is_decided(self):
        self.assertEqual(model.fair_up(100, 99, 0, 0.1, obs_avg=99.5).p, 1.0)

    def test_blend_of_equal_inputs_with_unit_weights(self):
        self.assertAlmostEqual(model.blend(0.7, 0.7, (0.5, 0.5, 0.0)), 0.7)

    def test_vol_needs_eleven_closes(self):
        self.assertIsNone(model.vol_per_sec([1.0] * 10))
        self.assertAlmostEqual(model.vol_per_sec([0.0, 60 ** 0.5] * 6), 1.0)


class JournalTest(unittest.TestCase):
    def test_add_settle_delete(self):
        with tempfile.TemporaryDirectory() as d:
            os.environ["STRIKE_DESK_DB"] = os.path.join(d, "j.sqlite3")
            from strike_desk import journal
            importlib.reload(journal)
            journal.add("BTC", "YES", 0.4, 0.46, 84000.0, 84010.0, 1790439300.0, "KXBTC15M-X")
            (r,) = journal.open_rows()
            journal.settle(r["id"], 1, "kalshi", 84020.5)
            self.assertEqual(journal.open_rows(), [])
            self.assertEqual(journal.rows()[0]["settle_value"], 84020.5)
            journal.delete(r["id"])
            self.assertEqual(journal.rows(), [])



class SignalTest(unittest.TestCase):
    CLOSE = 1_790_439_300.0

    def market(self, now, spot, yes_ask, no_ask, target=100.0, fetched=None):
        from strike_desk.feeds import Quote
        q = Quote("KXSOL15M-T", self.CLOSE - 900, self.CLOSE, target, yes_ask - .01, yes_ask, no_ask - .01, no_ask,
                  fetched if fetched is not None else now)
        # 35 closed 1-minute candles wobbling by 0.05 around 100
        candles = [(now - 60 * (36 - i), 100.0, 100.0 + (0.05 if i % 2 else 0.0)) for i in range(36)]
        return q, (spot, now), candles

    def call(self, s_left, spot, yes_ask, no_ask, **kw):
        from strike_desk.signal import Settings, evaluate
        now = self.CLOSE - s_left
        q, sp, cs = self.market(now, spot, yes_ask, no_ask, **kw)
        return evaluate("SOL", now, q, sp, cs, [], Settings.default("SOL"))

    def test_takes_up_when_yes_is_cheap(self):
        c = self.call(300, 100.3, 0.40, 0.62)
        self.assertEqual((c.action, c.side, c.price), ("UP", "YES", 0.40))
        self.assertGreaterEqual(c.edge, 0.03)

    def test_takes_down_when_no_is_cheap(self):
        c = self.call(300, 99.7, 0.62, 0.40)
        self.assertEqual((c.action, c.side), ("DOWN", "NO"))

    def test_waits_when_priced_fairly(self):
        c = self.call(300, 100.0, 0.51, 0.51)
        self.assertEqual(c.action, "WAIT")
        self.assertFalse(c.is_signal)

    def test_stale_kalshi_quote_is_not_used(self):
        c = self.call(300, 100.3, 0.40, 0.62, fetched=self.CLOSE - 300 - 60)
        self.assertEqual(c.action, "LEAN UP")

    def test_no_calls_in_first_two_minutes(self):
        self.assertEqual(self.call(850, 100.3, 0.40, 0.62).action, "WAIT")

    def test_decided_market_waits(self):
        c = self.call(200, 101.0, 0.999, 1.0)
        self.assertEqual((c.action, c.why), ("WAIT", "Market is all but decided"))

    def test_last_seconds_wait(self):
        self.assertEqual(self.call(10, 100.3, 0.40, 0.62).why, "Too late: last 20 s")

    def test_signal_recorded_once_and_settled(self):
        with tempfile.TemporaryDirectory() as d:
            os.environ["STRIKE_DESK_DB"] = os.path.join(d, "j.sqlite3")
            from strike_desk import journal
            importlib.reload(journal)
            self.assertTrue(journal.record_signal("SOL", self.CLOSE, "YES", 0.4, 0.5, 0.08, 300, "T"))
            self.assertFalse(journal.record_signal("SOL", self.CLOSE, "YES", 0.38, 0.5, 0.1, 250, "T"))
            (r,) = journal.open_signals()
            journal.settle_signal(r["id"], 1, 100.4)
            self.assertEqual(journal.signals()[0]["outcome"], 1)


if __name__ == "__main__":
    unittest.main()
