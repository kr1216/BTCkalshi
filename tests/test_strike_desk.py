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


if __name__ == "__main__":
    unittest.main()
