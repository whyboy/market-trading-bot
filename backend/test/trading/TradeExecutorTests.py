"""验证模拟资金守恒、恢复、并发去重和失败重跑，不访问外部行情。"""
import unittest
from decimal import Decimal
from TradeExecutorFixture import TradeExecutorFixture


class TradeExecutorTests(TradeExecutorFixture, unittest.TestCase):
    def test_commission_lot_size_and_conservation(self):
        result = self.rebalance(self.signal(), self.executor, 10000)
        self.assertEqual(result['positions']['A'], {'quantity': 900, 'sellable': 0})
        self.assertEqual(result['cash'], '995.00')
        self.assertEqual(Decimal(result['cash']) + 900 * Decimal('10'), Decimal('10000') - Decimal('5'))
        self.assertEqual(result['orders'][0]['status'], 'partial')
        self.assertEqual(result['orders'][0]['reason'], 'cash_limit')

    def test_proportional_commission_and_sell_slippage(self):
        self.executor.commission_rate = Decimal('.01')
        first = self.rebalance(self.signal(), self.executor, 10000)
        self.assertEqual(first['fills'][0]['commission'], '90.00')
        self.executor.slippage = Decimal('.01')
        second = self.rebalance(self.signal(day=23, weight=0), self.executor, 10000)
        self.assertEqual(second['fills'][0]['price'], '9.900')
        self.assertEqual(second['fills'][0]['commission'], '89.10')
        self.assertEqual(second['cash'], '9730.90')

    def test_same_symbol_is_rebalanced_and_zero_target_liquidates(self):
        self.rebalance(self.signal(), self.executor, 10000)
        result = self.rebalance(self.signal(day=23, weight=.5), self.executor, 10000)
        self.assertEqual(result['positions']['A']['quantity'], 400)
        self.assertEqual(result['fills'][0]['quantity'], 500)
        result = self.rebalance(self.signal(day=24, weight=0), self.executor, 10000)
        self.assertEqual(result['positions'], {})
        self.assertEqual(result['cash'], '9985.00')

    def test_exact_cash_includes_rounded_commission(self):
        result = self.rebalance(self.signal(prices=(10.001, 20)), self.executor, 30009)
        self.assertEqual(result['fills'][0]['quantity'], 3000)
        self.assertEqual(result['cash'], '0.00')

    def test_insufficient_cash_and_slippage(self):
        result = self.rebalance(self.signal(), self.executor, 1000)
        self.assertEqual(result['fills'], [])
        self.assertEqual(result['cash'], '1000.00')
        self.executor.slippage = Decimal('.01')
        result = self.executor.rebalance(self.signal(), {'date': None, 'cash': '10000', 'positions': {}})
        self.assertEqual(result['fills'][0]['price'], '10.100')
        self.assertEqual(result['cash'], '905.00')

    def test_missing_held_symbol_and_backdated_run_are_rejected(self):
        self.rebalance(self.signal(day=23), self.executor, 10000)
        with self.assertRaises(ValueError):
            self.rebalance(self.signal(), self.executor, 10000)
        account = {'date': '2026-11-19', 'cash': '1000', 'positions': {'C': {'quantity': 100, 'sellable': 100}}}
        with self.assertRaises(ValueError):
            self.executor.rebalance(self.signal(), account)
