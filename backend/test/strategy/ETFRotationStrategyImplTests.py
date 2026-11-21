"""离线验证行情校验边界及信号计算。"""
import math
import unittest
from decimal import Decimal
from market_trading_bot.trading.TradeExecutor import TradeExecutor
from MarketFixture import MarketFixture


class ETFRotationStrategyImplTests(MarketFixture, unittest.TestCase):
    def test_valid_market_data_produces_finite_signals(self):
        as_of, snapshots = self.load()
        result = self.strategy.calculate(snapshots, as_of)
        self.assertEqual(result.ranking[0].symbol, self.strategy.symbols[0])
        self.assertTrue(all(math.isfinite(item.score) for item in result.ranking))
        self.assertTrue(all(0 <= weight <= 1 for weight in result.target_weights.values()))
        self.assertEqual(sum(weight > 0 for weight in result.target_weights.values()), 1)

    def test_market_signal_preserves_cash_and_lot_rules(self):
        self.daily['收盘'] = [100 * math.exp(.001 * day + .005 * math.sin(day)) for day in range(len(self.days))]
        as_of, snapshots = self.load()
        rules = {symbol: {'lot_size': 100, 't_plus_one': True} for symbol in self.strategy.symbols}
        trader = TradeExecutor({'commission_rate': .0002, 'minimum_commission': 5, 'slippage_rate': 0}, rules)
        for target_volatility in (0, .15):
            with self.subTest(target_volatility=target_volatility):
                self.strategy.target_volatility = target_volatility
                signal = self.strategy.calculate(snapshots, as_of)
                self.assertEqual(set(signal.target_weights), set(snapshots))
                self.assertLessEqual(sum(signal.target_weights.values()), 1)
                result = trader.rebalance(signal, {'date': None, 'cash': '100000', 'positions': {}})
                self.assertGreaterEqual(Decimal(result['cash']), 0)
                self.assertTrue(all(position['quantity'] % 100 == 0 for position in result['positions'].values()))
                if target_volatility == 0:
                    self.assertEqual(result['fills'], [])
