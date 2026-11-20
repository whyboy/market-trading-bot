"""验证模拟资金守恒、恢复、并发去重和失败重跑，不访问外部行情。"""
import csv
import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from market_trading_bot.models import RankedETF, StrategyResult
from market_trading_bot.PaperExecutor import PaperExecutor
from market_trading_bot.PaperLedger import PaperLedger
from market_trading_bot.runner import run_paper
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo


class PaperTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.rules = {'A': {'lot_size': 100, 't_plus_one': True},
                      'B': {'lot_size': 100, 't_plus_one': False}}
        self.executor = PaperExecutor({'commission_rate': .0002, 'minimum_commission': 5,
                                       'slippage_rate': 0}, self.rules)
        self.ledger = PaperLedger(self.root, 'pap')

    def signal(self, day=20, winner='A', weight=1, prices=(10, 20)):
        now = datetime(2026, 11, day, 14, 30, tzinfo=ZoneInfo('Asia/Shanghai'))
        ranking = tuple(RankedETF(symbol, 1, .2, price, now, now.date() - timedelta(days=30),
                                  now.date() - timedelta(days=1)) for symbol, price in zip(self.rules, prices))
        return StrategyResult(now, ranking, {symbol: weight if symbol == winner else 0 for symbol in self.rules})

    def test_commission_lot_size_and_conservation(self):
        result = self.ledger.rebalance(self.signal(), self.executor, 10000)
        self.assertEqual(result['positions']['A'], {'quantity': 900, 'sellable': 0})
        self.assertEqual(result['cash'], '995.00')
        self.assertEqual(Decimal(result['cash']) + 900 * Decimal('10'), Decimal('10000') - Decimal('5'))
        self.assertEqual(result['orders'][0]['status'], 'partial')
        self.assertEqual(result['orders'][0]['reason'], 'cash_limit')

    def test_restore_sell_before_buy_and_t_plus_zero(self):
        self.ledger.rebalance(self.signal(), self.executor, 10000)
        restored = PaperLedger(self.root, 'pap')
        result = restored.rebalance(self.signal(day=23, winner='B'), self.executor, 999999)
        self.assertEqual([fill['side'] for fill in result['fills']], ['sell', 'buy'])
        self.assertNotIn('A', result['positions'])
        self.assertEqual(result['positions']['B'], {'quantity': 400, 'sellable': 400})
        self.assertEqual(result['cash'], '1985.00')
        self.assertEqual(result['equity'], '9985.00')

    def test_proportional_commission_and_sell_slippage(self):
        self.executor.rate = Decimal('.01')
        first = self.ledger.rebalance(self.signal(), self.executor, 10000)
        self.assertEqual(first['fills'][0]['commission'], '90.00')
        self.executor.slippage = Decimal('.01')
        second = self.ledger.rebalance(self.signal(day=23, weight=0), self.executor, 10000)
        self.assertEqual(second['fills'][0]['price'], '9.900')
        self.assertEqual(second['fills'][0]['commission'], '89.10')
        self.assertEqual(second['cash'], '9730.90')

    def test_same_symbol_is_rebalanced_and_zero_target_liquidates(self):
        self.ledger.rebalance(self.signal(), self.executor, 10000)
        result = self.ledger.rebalance(self.signal(day=23, weight=.5), self.executor, 10000)
        self.assertEqual(result['positions']['A']['quantity'], 400)
        self.assertEqual(result['fills'][0]['quantity'], 500)
        result = self.ledger.rebalance(self.signal(day=24, weight=0), self.executor, 10000)
        self.assertEqual(result['positions'], {})
        self.assertEqual(result['cash'], '9985.00')

    def test_concurrent_retry_does_not_duplicate_fills(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.ledger.rebalance(self.signal(), self.executor, 10000), range(8)))
        self.assertTrue(all(result == results[0] for result in results))
        self.ledger.export_csv()
        with (self.ledger.directory / 'fills.csv').open() as stream:
            self.assertEqual(len(list(csv.DictReader(stream))), 1)

    def test_failure_rolls_back_and_can_retry(self):
        broken = Mock()
        broken.rebalance.side_effect = RuntimeError('interrupted')
        broken.money = self.executor.money
        with self.assertRaises(RuntimeError):
            self.ledger.rebalance(self.signal(), broken, 10000)
        self.assertIsNone(self.ledger.completed('2026-11-20'))
        self.assertEqual(len(self.ledger.rebalance(self.signal(), self.executor, 10000)['fills']), 1)

    def test_failed_insert_rolls_back(self):
        broken_signal = self.signal()
        broken_signal = replace(broken_signal, ranking=(replace(broken_signal.ranking[0], score=float('nan')), *broken_signal.ranking[1:]))
        with self.assertRaises(ValueError):
            self.ledger.rebalance(broken_signal, self.executor, 10000)
        self.assertIsNone(self.ledger.completed('2026-11-20'))

    def test_exports_can_be_rebuilt_and_instances_are_isolated(self):
        self.ledger.rebalance(self.signal(), self.executor, 10000)
        self.ledger.export_csv()
        (self.ledger.directory / 'fills.csv').write_text('broken')
        self.ledger.export_csv()
        with (self.ledger.directory / 'fills.csv').open() as stream:
            self.assertEqual(next(csv.DictReader(stream))['quantity'], '900')
        self.assertIsNone(PaperLedger(self.root, 'other').completed('2026-11-20'))

    def test_insufficient_cash_and_slippage(self):
        result = self.ledger.rebalance(self.signal(), self.executor, 1000)
        self.assertEqual(result['fills'], [])
        self.assertEqual(result['cash'], '1000.00')
        self.executor.slippage = Decimal('.01')
        result = self.executor.rebalance(self.signal(), {'date': None, 'cash': '10000', 'positions': {}})
        self.assertEqual(result['fills'][0]['price'], '10.100')
        self.assertEqual(result['cash'], '905.00')

    def test_missing_held_symbol_and_backdated_run_are_rejected(self):
        self.ledger.rebalance(self.signal(day=23), self.executor, 10000)
        with self.assertRaises(ValueError):
            self.ledger.rebalance(self.signal(), self.executor, 10000)
        account = {'date': '2026-11-19', 'cash': '1000', 'positions': {'C': {'quantity': 100, 'sellable': 100}}}
        with self.assertRaises(ValueError):
            self.executor.rebalance(self.signal(), account)

    def test_paper_runner_retries_without_market_and_recovers_csv(self):
        (self.root / 'config').mkdir()
        global_config = {'timezone': 'Asia/Shanghai', 'market': {}, 'storage': {'data_dir': 'data'}, 'logging': {}}
        config = {'strategy': 'etf_rotation', 'instances': [{'id': 'pap', 'enabled': True, 'mode': 'pap',
                  'initial_cash': 10000, 'execution': {'commission_rate': .0002, 'minimum_commission': 5, 'slippage_rate': 0}}],
                  'parameters': {}, 'instruments': self.rules}
        (self.root / 'config/global.json').write_text(json.dumps(global_config))
        path = self.root / 'config/strategy.json'
        path.write_text(json.dumps(config))
        with patch('market_trading_bot.runner.strategy_logging'), \
                patch('market_trading_bot.runner.datetime') as clock, \
                patch('market_trading_bot.runner.ETFRotationStrategy') as strategy, \
                patch('market_trading_bot.runner.AKShareMarket') as market:
            clock.now.return_value = self.signal().as_of
            strategy.return_value.calculate.return_value = self.signal()
            market.return_value.load.return_value = self.signal().as_of, {}
            with patch.object(PaperLedger, 'export_csv', side_effect=OSError('disk full')):
                first = run_paper(self.root, path, 'pap')
            self.assertFalse(first['csv_exported'])
            market.reset_mock()
            second = run_paper(self.root, path, 'pap')
            market.assert_not_called()
            self.assertTrue(second['csv_exported'])
            self.assertEqual(first['fills'], second['fills'])
        config['instances'][0]['mode'] = 'pro'
        path.write_text(json.dumps(config))
        with patch('market_trading_bot.runner.strategy_logging'), self.assertRaises(ValueError):
            run_paper(self.root, path, 'pap')


if __name__ == '__main__':
    unittest.main()
