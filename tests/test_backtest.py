"""The backtester has to be trustworthy before its results mean anything: no lookahead, correct fees, conservative fills."""
import unittest

from duo import backtest
from duo.backtest import BARS_PER_DAY, parse_bars, price_moves, rolling_high, run_breakout, run_dip, run_trend, sma


def bars_from(rows, start=86400 * 1000):
    return parse_bars([(start + i * 300, *row) for i, row in enumerate(rows)])


def flat(price, count):
    return [(price, price * 1.0001, price * 0.9999, price)] * count


def net(run, maker=0.4, taker=0.8):
    return sum(backtest.net_daily(run, maker, taker))


class Indicators(unittest.TestCase):
    def test_sma(self):
        self.assertEqual(sma([1, 2, 3, 4], 2), [None, 1.5, 2.5, 3.5])

    def test_rolling_high_drops_old_values(self):
        self.assertEqual(rolling_high([5, 1, 2, 1], 2), [None, 5, 2, 2])


class Dip(unittest.TestCase):
    def dip(self, rows, entry=1, target=2, stop=4):
        bars = bars_from(rows)
        return run_dip(bars, "BTC", [100.0] * len(rows), [0.0] * len(rows), entry, target, stop, 12, False)

    def test_winning_trade_pays_maker_fees_both_ways(self):
        run = self.dip(flat(100, 3) + [(100, 100, 98.9, 99.5)] + [(99.5, 101.5, 99.5, 100.9)])
        self.assertEqual(sum(run.trades), 1)
        expected_gross = 99 * 1.02 / 99 - 1
        self.assertAlmostEqual(sum(run.gross), expected_gross, places=9)
        self.assertAlmostEqual(sum(run.maker), 2.02, places=6)
        self.assertEqual(sum(run.taker), 0)
        self.assertAlmostEqual(net(run), expected_gross - 2.02 * 0.004, places=9)

    def test_merely_touching_the_bid_isnt_a_fill(self):
        run = self.dip(flat(100, 3) + [(100, 100, 99.0, 99.5)] + flat(100, 3))
        self.assertEqual(sum(run.maker), 0)

    def test_stop_loss_pays_taker_and_slippage(self):
        run = self.dip(flat(100, 3) + [(100, 100, 98.9, 99.5)] + [(99.5, 99.5, 95, 95)])
        stop_price = 99 * 0.96 * (1 - backtest.TAKER_SLIPPAGE)
        self.assertAlmostEqual(sum(run.gross), stop_price / 99 - 1, places=9)
        self.assertEqual(sum(run.trades), 1)
        self.assertGreater(sum(run.taker), 0)

    def test_bar_touching_stop_and_target_assumes_the_stop(self):
        run = self.dip(flat(100, 3) + [(100, 100, 98.9, 99.5)] + [(99.5, 110, 90, 100)])
        self.assertLess(sum(run.gross), 0)

    def test_entry_bar_cant_also_hit_the_target(self):
        run = self.dip(flat(100, 3) + [(100, 105, 98.9, 104)])
        self.assertEqual(sum(run.trades), 0)

    def test_gap_below_the_bid_isnt_a_maker_fill(self):
        run = self.dip(flat(100, 3) + [(95, 96, 94, 95)])
        self.assertEqual(sum(run.maker), 0)

    def test_gap_through_the_stop_fills_at_the_worse_open(self):
        run = self.dip(flat(100, 3) + [(100, 100, 98.9, 99.5)] + [(80, 81, 79, 80)])
        self.assertAlmostEqual(sum(run.gross), 80 * (1 - backtest.TAKER_SLIPPAGE) / 99 - 1, places=9)

    def test_future_prices_cant_change_past_decisions(self):
        day_one = flat(100, 3) + [(100, 100, 98.9, 99.5)] + [(99.5, 101.5, 99.5, 100.9)] + flat(100, BARS_PER_DAY - 5)

        def run_with(future):
            rows = day_one + future
            bars = bars_from(rows)
            return run_dip(bars, "BTC", sma(bars.close, 2), [0.0] * len(rows), 1, 2, 4, 2, False)

        calm, wild = run_with(flat(100, 50)), run_with([(500, 600, 400, 500)] * 50)
        self.assertEqual(calm.trades[0], 1)
        self.assertEqual((calm.gross[0], calm.maker[0], calm.taker[0], calm.trades[0]),
                         (wild.gross[0], wild.maker[0], wild.taker[0], wild.trades[0]))

    def test_open_position_is_marked_daily_and_totals_match(self):
        rows = flat(100, 3) + [(100, 100, 98.9, 99)] + flat(99.5, BARS_PER_DAY * 2)
        bars = bars_from(rows)
        run = run_dip(bars, "BTC", [100.0] * len(rows), [0.0] * len(rows), 1, 50, 50, 12, False)
        self.assertAlmostEqual(sum(run.gross), 99.5 / 99 - 1, places=9)
        self.assertGreater(sum(1 for g in run.gross if g != 0), 0)


class Others(unittest.TestCase):
    def test_breakout_waits_for_the_next_bar_and_pays_taker(self):
        rows = flat(100, 5) + [(100, 105, 100, 105)] + [(110, 111, 110, 110)] + [(110, 110, 90, 90)]
        bars = bars_from(rows)
        run = run_breakout(bars, "BTC", rolling_high(bars.high, 3), 3, 2, 8)
        entry = 110 * (1 + backtest.TAKER_SLIPPAGE)
        stop = entry * 0.98 * (1 - backtest.TAKER_SLIPPAGE)
        self.assertEqual(sum(run.trades), 1)
        self.assertAlmostEqual(sum(run.gross), stop / entry - 1, places=9)
        self.assertEqual(sum(run.maker), 0)

    def test_trend_enters_and_exits_on_the_following_open(self):
        rows = flat(100, 5) + flat(110, 5) + flat(90, 5)
        bars = bars_from(rows)
        run = run_trend(bars, "BTC", sma(bars.close, 2), sma(bars.close, 4), 2, 4)
        self.assertGreaterEqual(sum(run.trades), 1)
        self.assertLess(sum(run.gross), 0)


class Years(unittest.TestCase):
    def test_assets_share_a_calendar_when_given_a_first_day(self):
        start = 86400 * 1000
        late = parse_bars([(start + 86400 * 5, 1, 1, 1, 1)], first_day=1000)
        self.assertEqual(late.day[0], 5)
        self.assertEqual(late.days, 6)

    def test_a_rule_on_an_asset_that_lists_late_keeps_its_days_on_the_calendar(self):
        rows = [(86400 * 1005 + i * 300, *row) for i, row in enumerate(flat(100, 3) + [(100, 100, 98.9, 99)] + [(99, 99, 90, 90)])]
        bars = parse_bars(rows, first_day=1000)
        run = run_dip(bars, "SOL", [100.0] * len(rows), [0.0] * len(rows), 1, 2, 4, 12, False)
        self.assertEqual(len(run.gross), 6)
        self.assertEqual(sum(run.gross[:5]), 0)
        self.assertLess(run.gross[5], 0)

    def test_max_hold_closes_a_position_as_a_taker(self):
        rows = flat(100, 3) + [(100, 100, 98.9, 99)] + flat(99.5, 10)
        bars = bars_from(rows)
        run = run_dip(bars, "BTC", [100.0] * len(rows), [0.0] * len(rows), 1, 50, 50, 12, False, 3)
        self.assertEqual(sum(run.trades), 1)
        self.assertGreater(sum(run.taker), 0)

    def test_year_spans_follow_the_calendar(self):
        first_day = 18628  # 2021-01-01
        spans = backtest.year_spans(first_day, 360, 370)
        self.assertEqual(spans, [(2021, 360, 365), (2022, 365, 370)])

    def test_yearly_rows_add_up_and_compare_against_holding(self):
        from array import array
        first_day = 18628
        days = backtest.TRAIN_DAYS + 30
        rows = [(86400 * (first_day + d), 100.0, 100.0, 100.0, 100.0 + (d >= backtest.TRAIN_DAYS) * 10) for d in range(days)]
        bars = parse_bars(rows, first_day)
        daily = [0.0] * days
        daily[backtest.TRAIN_DAYS + 1] = 0.02
        result = {"daily": daily, "fees_daily": [0.001 if d == backtest.TRAIN_DAYS + 1 else 0.0 for d in range(days)],
                  "trades_daily": [1.0 if d == backtest.TRAIN_DAYS + 1 else 0.0 for d in range(days)]}
        zeros = array("d", [0.0] * days)
        gross = array("d", [0.01 if d == backtest.TRAIN_DAYS + 2 else 0.0 for d in range(days)])
        trades = array("d", [1.0 if d == backtest.TRAIN_DAYS + 2 else 0.0 for d in range(days)])
        run = backtest.Run("BTC", "dip", "x", gross, zeros, zeros, trades)
        output = backtest.yearly_rows(result, {"BTC": [run]}, ("No fees", 0, 0, 0), {"BTC": bars}, first_day, days)
        self.assertEqual(sum(r["days"] for r in output), 30)
        self.assertAlmostEqual(sum(r["picked_pct"] for r in output), 2.0)
        self.assertAlmostEqual(sum(r["fees_pct"] for r in output), 0.1)
        self.assertEqual(sum(r["trades"] for r in output), 1)
        self.assertAlmostEqual(output[0]["hold_btc_pct"], 10.0, places=6)
        self.assertEqual(output[0]["rules_profitable_pct"], 100)


class Scoring(unittest.TestCase):
    def test_price_moves_counts_each_threshold_crossing(self):
        bars = bars_from(flat(100, 2) + [(100, 102, 100, 102)] + flat(102, 2) + [(102, 102, 99, 99)])
        result = price_moves(bars, 1.5)
        self.assertEqual(result["moves_per_day"], 2)

    def test_stats(self):
        result = backtest.stats([0.01, -0.02, 0.0, 0.03], 4, 0.05, 0.03)
        self.assertAlmostEqual(result["return_pct"], 2.0)
        self.assertAlmostEqual(result["max_drawdown_pct"], -2.0)
        self.assertEqual(result["green_days_pct"], 50)
        self.assertEqual(result["red_days_pct"], 25)

    def test_walk_forward_only_uses_the_past_to_choose(self):
        days = backtest.TRAIN_DAYS + backtest.TEST_DAYS
        zeros = [0.0] * days

        def make(params, gross):
            from array import array
            trades = [1.0 if i % 10 == 0 else 0.0 for i in range(days)]
            return backtest.Run("BTC", "dip", params, array("d", gross), array("d", zeros), array("d", zeros), array("d", trades))

        # Rule A is great in the past and terrible later. Rule B is the reverse.
        a = make("a", [0.01] * backtest.TRAIN_DAYS + [-0.01] * backtest.TEST_DAYS)
        b = make("b", [-0.01] * backtest.TRAIN_DAYS + [0.01] * backtest.TEST_DAYS)
        tier = ("No fees", 0, 0, 0)
        result = backtest.walk_forward({"BTC": [a, b]}, tier, days)
        self.assertEqual(result["chosen"][0]["rule"], ("dip", "a"))
        self.assertLess(result["return_pct"], 0)

    def test_stays_in_cash_when_nothing_made_money(self):
        from array import array
        days = backtest.TRAIN_DAYS + backtest.TEST_DAYS
        zeros = array("d", [0.0] * days)
        loser = backtest.Run("BTC", "dip", "x", array("d", [-0.01] * days), zeros, zeros, array("d", [1.0] * days))
        result = backtest.walk_forward({"BTC": [loser]}, ("No fees", 0, 0, 0), days)
        self.assertEqual(result["chosen"][0]["rule"], "cash")
        self.assertEqual(result["return_pct"], 0)


if __name__ == "__main__":
    unittest.main()
