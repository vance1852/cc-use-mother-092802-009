import unittest

from beverage_snapshot import calc


def _row(fact_id, source, segment, brand, region, period, metric, value,
         recorded_at, currency="CNY", batch_id="b1", is_late=0):
    return {"fact_id": fact_id, "source": source, "segment": segment, "brand": brand,
            "region": region, "period": period, "metric_id": metric, "value": value,
            "currency": currency, "batch_id": batch_id, "recorded_at": recorded_at,
            "is_late": is_late, "payload_hash": fact_id}


BEER_CURRENT = [
    _row("v", "s", "beer", "B", "east", "2026H1", "sales_volume", 1000.0, "t1"),
    _row("pv", "s", "beer", "B", "east", "2026H1", "premium_volume", 400.0, "t1"),
    _row("gr", "s", "beer", "B", "east", "2026H1", "gross_revenue", 8000.0, "t1"),
    _row("pr", "s", "beer", "B", "east", "2026H1", "premium_revenue", 4000.0, "t1"),
    _row("cogs", "s", "beer", "B", "east", "2026H1", "cogs", 4800.0, "t1"),
    _row("opex", "s", "beer", "B", "east", "2026H1", "operating_expense", 1200.0, "t1"),
    _row("da", "s", "beer", "B", "east", "2026H1", "depreciation_amortization", 300.0, "t1"),
]
BEER_PRIOR = [
    _row("gr0", "s", "beer", "B", "east", "2025H1", "gross_revenue", 6400.0, "t0"),
]


class CalcTest(unittest.TestCase):
    def test_prior_period(self):
        self.assertEqual("2025H1", calc.prior_period("2026H1"))
        self.assertEqual("2025Q4", calc.prior_period("2026Q4"))
        self.assertIsNone(calc.prior_period("bad"))

    def test_derived_metrics_recompute(self):
        result = calc.compute_result(
            period="2026H1", reporting_ccy="CNY",
            current_rows=BEER_CURRENT, prior_rows=BEER_PRIOR, rates={"CNY": 1.0})
        metrics = result["groups"][0]["metrics"]
        self.assertEqual(0.4, metrics["premium_volume_share"])
        self.assertEqual(0.5, metrics["premium_revenue_share"])
        self.assertEqual(4.8, metrics["unit_cost"])
        self.assertEqual(2300.0, metrics["ebitda"])          # 8000-4800-1200+300
        self.assertEqual(0.2875, metrics["ebitda_margin"])
        self.assertEqual(0.25, metrics["yoy_growth"])        # 8000/6400-1

    def test_missing_prior_leaves_yoy_empty(self):
        result = calc.compute_result(
            period="2026H1", reporting_ccy="CNY",
            current_rows=BEER_CURRENT, prior_rows=[], rates={"CNY": 1.0})
        self.assertIsNone(result["groups"][0]["metrics"]["yoy_growth"])

    def test_zero_denominator_is_none_not_infinity(self):
        self.assertIsNone(calc.ratio(1.0, 0))
        self.assertIsNone(calc.ratio(1.0, None))

    def test_currency_conversion_uses_frozen_rates(self):
        rows = [_row("gr", "s", "beer", "B", "east", "2026H1",
                     "gross_revenue", 100.0, "t1", currency="USD")]
        result = calc.compute_result(
            period="2026H1", reporting_ccy="CNY",
            current_rows=rows, prior_rows=[], rates={"CNY": 1.0, "USD": 7.2})
        self.assertEqual(720.0, result["totals"]["gross_revenue"])
        with self.assertRaises(KeyError):
            calc.to_reporting_ccy(1.0, "EUR", {"CNY": 1.0})

    def test_latest_batch_wins_without_deleting_old_rows(self):
        old = _row("f1", "s", "beer", "B", "east", "2026H1",
                   "gross_revenue", 100.0, "2026-09-01T00:00:00Z", batch_id="b1")
        late = _row("f2", "s", "beer", "B", "east", "2026H1",
                    "gross_revenue", 130.0, "2026-09-25T00:00:00Z", batch_id="b2", is_late=1)
        chosen = calc.latest_batch_rows([old, late], period="2026H1")
        self.assertEqual(["f2"], [r["fact_id"] for r in chosen])
        # 其他期间的行不参与本期间选择。
        self.assertEqual([], calc.latest_batch_rows([old], period="2025H1"))

    def test_cross_segment_volume_is_not_aggregated(self):
        baijiu = [
            _row("v2", "s", "baijiu", "C", "east", "2026H1", "sales_volume", 50.0, "t1"),
            _row("g2", "s", "baijiu", "C", "east", "2026H1", "gross_revenue", 5000.0, "t1"),
            _row("c2", "s", "baijiu", "C", "east", "2026H1", "cogs", 2000.0, "t1"),
        ]
        result = calc.compute_result(
            period="2026H1", reporting_ccy="CNY",
            current_rows=BEER_CURRENT + baijiu, prior_rows=[], rates={"CNY": 1.0})
        self.assertNotIn("sales_volume", result["totals"])
        self.assertIsNone(result["totals"]["unit_cost"])
        self.assertIsNone(result["totals"]["premium_volume_share"])
        # 金额口径一致，可在总体汇总。
        self.assertEqual(13000.0, result["totals"]["gross_revenue"])
        self.assertEqual(5300.0, result["totals"]["ebitda"])


if __name__ == "__main__":
    unittest.main()
