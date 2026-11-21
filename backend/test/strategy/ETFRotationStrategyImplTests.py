"""离线验证行情校验边界及信号计算。"""
import math
import unittest
from MarketFixture import MarketFixture


class ETFRotationStrategyImplTests(MarketFixture, unittest.TestCase):
    def test_valid_market_data_produces_finite_signals(self):
        as_of, snapshots = self.load()
        result = self.strategy.calculate(snapshots, as_of)
        self.assertEqual(result.ranking[0].symbol, self.strategy.symbols[0])
        self.assertTrue(all(math.isfinite(item.score) for item in result.ranking))
        self.assertTrue(all(0 <= weight <= 1 for weight in result.target_weights.values()))
        self.assertEqual(sum(weight > 0 for weight in result.target_weights.values()), 1)
