"""验证模拟资金守恒、恢复、并发去重和失败重跑，不访问外部行情。"""
import csv
import json
import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from market_trading_bot.models import RankedETF, StrategyResult
from market_trading_bot.PapAccountStore import PapAccountStore
from market_trading_bot.PapExecutor import PapExecutor
from market_trading_bot.runtime.TradingTask import TradingTask
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo


class PapTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.rules = {'A': {'lot_size': 100, 't_plus_one': True},
                      'B': {'lot_size': 100, 't_plus_one': False}}
        self.executor = PapExecutor({'commission_rate': .0002, 'minimum_commission': 5,
                                       'slippage_rate': 0}, self.rules)
        self.account_store = PapAccountStore(self.root, 'pap')

    def signal(self, day=20, winner='A', weight=1, prices=(10, 20)):
        now = datetime(2026, 11, day, 14, 30, tzinfo=ZoneInfo('Asia/Shanghai'))
        ranking = tuple(RankedETF(symbol, 1, .2, price, now, now.date() - timedelta(days=30),
                                  now.date() - timedelta(days=1)) for symbol, price in zip(self.rules, prices))
        return StrategyResult(now, ranking, {symbol: weight if symbol == winner else 0 for symbol in self.rules})

    def test_commission_lot_size_and_conservation(self):
        result = self.account_store.rebalance(self.signal(), self.executor, 10000)
        self.assertEqual(result['positions']['A'], {'quantity': 900, 'sellable': 0})
        self.assertEqual(result['cash'], '995.00')
        self.assertEqual(Decimal(result['cash']) + 900 * Decimal('10'), Decimal('10000') - Decimal('5'))
        self.assertEqual(result['orders'][0]['status'], 'partial')
        self.assertEqual(result['orders'][0]['reason'], 'cash_limit')

    def test_restore_sell_before_buy_and_t_plus_zero(self):
        self.account_store.rebalance(self.signal(), self.executor, 10000)
        restored = PapAccountStore(self.root, 'pap')
        result = restored.rebalance(self.signal(day=23, winner='B'), self.executor, 999999)
        self.assertEqual([fill['side'] for fill in result['fills']], ['sell', 'buy'])
        self.assertNotIn('A', result['positions'])
        self.assertEqual(result['positions']['B'], {'quantity': 400, 'sellable': 400})
        self.assertEqual(result['cash'], '1985.00')
        self.assertEqual(result['equity'], '9985.00')

    def test_proportional_commission_and_sell_slippage(self):
        self.executor.rate = Decimal('.01')
        first = self.account_store.rebalance(self.signal(), self.executor, 10000)
        self.assertEqual(first['fills'][0]['commission'], '90.00')
        self.executor.slippage = Decimal('.01')
        second = self.account_store.rebalance(self.signal(day=23, weight=0), self.executor, 10000)
        self.assertEqual(second['fills'][0]['price'], '9.900')
        self.assertEqual(second['fills'][0]['commission'], '89.10')
        self.assertEqual(second['cash'], '9730.90')

    def test_same_symbol_is_rebalanced_and_zero_target_liquidates(self):
        self.account_store.rebalance(self.signal(), self.executor, 10000)
        result = self.account_store.rebalance(self.signal(day=23, weight=.5), self.executor, 10000)
        self.assertEqual(result['positions']['A']['quantity'], 400)
        self.assertEqual(result['fills'][0]['quantity'], 500)
        result = self.account_store.rebalance(self.signal(day=24, weight=0), self.executor, 10000)
        self.assertEqual(result['positions'], {})
        self.assertEqual(result['cash'], '9985.00')

    def test_concurrent_retry_does_not_duplicate_fills(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.account_store.rebalance(self.signal(), self.executor, 10000), range(8)))
        self.assertTrue(all(result == results[0] for result in results))
        self.account_store.export_csv()
        with (self.account_store.directory / 'account_trades.csv').open(encoding='utf-8-sig') as stream:
            self.assertEqual(len(list(csv.DictReader(stream))), 1)

    def test_failure_rolls_back_and_can_retry(self):
        broken = Mock()
        broken.rebalance.side_effect = RuntimeError('interrupted')
        broken.money = self.executor.money
        with self.assertRaises(RuntimeError):
            self.account_store.rebalance(self.signal(), broken, 10000)
        self.assertIsNone(self.account_store.completed('2026-11-20'))
        self.assertEqual(len(self.account_store.rebalance(self.signal(), self.executor, 10000)['fills']), 1)

    def test_failed_insert_rolls_back(self):
        broken_signal = self.signal()
        broken_signal = replace(broken_signal, ranking=(replace(broken_signal.ranking[0], score=float('nan')), *broken_signal.ranking[1:]))
        with self.assertRaises(ValueError):
            self.account_store.rebalance(broken_signal, self.executor, 10000)
        self.assertIsNone(self.account_store.completed('2026-11-20'))

    def test_exports_can_be_rebuilt_and_instances_are_isolated(self):
        self.account_store.rebalance(self.signal(), self.executor, 10000)
        self.account_store.export_csv()
        (self.account_store.directory / 'account_trades.csv').write_text('broken')
        self.account_store.export_csv()
        with (self.account_store.directory / 'account_trades.csv').open(encoding='utf-8-sig') as stream:
            self.assertEqual(next(csv.DictReader(stream))['buy_quantity'], '900')
        self.assertIsNone(PapAccountStore(self.root, 'other').completed('2026-11-20'))

    def read_trades(self):
        with (self.account_store.directory / 'account_trades.csv').open(encoding='utf-8-sig') as stream:
            return list(csv.DictReader(stream))

    def test_summary_skips_no_fills_and_uses_actual_initial_cash(self):
        self.account_store.rebalance(self.signal(weight=0), self.executor, 10000)
        with sqlite3.connect(self.account_store.database) as connection:
            self.assertEqual(connection.execute('SELECT initial_cash FROM account').fetchone()[0], '10000.00')
        self.account_store.export_csv()
        self.assertEqual(self.read_trades(), [])
        self.account_store.rebalance(self.signal(day=23), self.executor, 999999)
        self.account_store.rebalance(self.signal(day=24), self.executor, 999999)
        self.account_store.export_csv()
        rows = self.read_trades()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row['date'], '2026-11-23')
        self.assertEqual(row['buy_etf'], 'A')
        self.assertEqual(row['sell_etf'], '')
        self.assertEqual(row['commission'], '5.00')
        self.assertEqual(row['cash'], '995.00')
        self.assertEqual(row['market_value'], '9000.00')
        self.assertEqual(row['total_assets'], '9995.00')
        self.assertEqual(row['initial_cash'], '10000.00')
        self.assertEqual(row['cumulative_pnl'], '-5.00')
        self.assertEqual(row['cumulative_return_pct'], '-0.050000')
        self.assertEqual(row['net_value'], '0.99950000')
        self.assertIsNotNone(self.account_store.completed('2026-11-24'))

    def test_legacy_initial_cash_is_restored_and_saved_once(self):
        self.account_store.rebalance(self.signal(), self.executor, 10000)
        with sqlite3.connect(self.account_store.database) as connection, connection:
            connection.execute('DELETE FROM account')
        self.account_store.export_csv()
        with sqlite3.connect(self.account_store.database) as connection:
            self.assertEqual(connection.execute('SELECT initial_cash FROM account').fetchone()[0], '10000.00')
        with patch.object(PapAccountStore, '_restore_initial_cash', side_effect=AssertionError('不应再次反推')):
            self.account_store.export_csv()

    def test_summary_includes_rotation_and_liquidation(self):
        self.account_store.rebalance(self.signal(), self.executor, 10000)
        self.account_store.rebalance(self.signal(day=23, winner='B'), self.executor, 10000)
        self.account_store.rebalance(self.signal(day=24, weight=0), self.executor, 10000)
        (self.account_store.directory / 'fills.csv').write_text('old export')
        self.account_store.export_csv()
        rows = self.read_trades()
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[1]['buy_etf'], 'B')
        self.assertEqual(rows[1]['buy_quantity'], '400')
        self.assertEqual(rows[1]['sell_etf'], 'A')
        self.assertEqual(rows[1]['sell_quantity'], '900')
        self.assertEqual(rows[1]['commission'], '10.00')
        self.assertEqual(rows[1]['cumulative_pnl'], '-15.00')
        self.assertEqual(rows[2]['holding_etf'], '')
        self.assertEqual(rows[2]['market_value'], '0.00')
        self.assertEqual(rows[2]['total_assets'], rows[2]['cash'])
        self.assertEqual(len(list(self.account_store.directory.glob('*.csv'))), 1)

    def test_summary_tracks_gains_across_untraded_dates(self):
        self.account_store.rebalance(self.signal(), self.executor, 10000)
        self.account_store.rebalance(self.signal(day=23, prices=(12, 20)), self.executor, 10000)
        self.account_store.rebalance(self.signal(day=24, winner='B', prices=(12, 20)), self.executor, 10000)
        self.account_store.export_csv()
        rows = self.read_trades()
        self.assertEqual([row['date'] for row in rows], ['2026-11-20', '2026-11-24'])
        self.assertEqual(rows[-1]['total_assets'], '11785.00')
        self.assertEqual(rows[-1]['initial_cash'], '10000.00')
        self.assertEqual(rows[-1]['cumulative_pnl'], '1785.00')
        self.assertEqual(rows[-1]['cumulative_return_pct'], '17.850000')

    def test_failed_export_preserves_previous_csv(self):
        self.account_store.rebalance(self.signal(), self.executor, 10000)
        self.account_store.export_csv()
        path = self.account_store.directory / 'account_trades.csv'
        previous = path.read_bytes()
        self.account_store.rebalance(self.signal(day=23, winner='B'), self.executor, 10000)
        with patch.object(Path, 'replace', side_effect=OSError('interrupted')), self.assertRaises(OSError):
            self.account_store.export_csv()
        self.assertEqual(path.read_bytes(), previous)
        self.account_store.export_csv()
        self.assertEqual(len(self.read_trades()), 2)

    def test_exact_cash_includes_rounded_commission(self):
        result = self.account_store.rebalance(self.signal(prices=(10.001, 20)), self.executor, 30009)
        self.assertEqual(result['fills'][0]['quantity'], 3000)
        self.assertEqual(result['cash'], '0.00')

    def test_insufficient_cash_and_slippage(self):
        result = self.account_store.rebalance(self.signal(), self.executor, 1000)
        self.assertEqual(result['fills'], [])
        self.assertEqual(result['cash'], '1000.00')
        self.executor.slippage = Decimal('.01')
        result = self.executor.rebalance(self.signal(), {'date': None, 'cash': '10000', 'positions': {}})
        self.assertEqual(result['fills'][0]['price'], '10.100')
        self.assertEqual(result['cash'], '905.00')

    def test_missing_held_symbol_and_backdated_run_are_rejected(self):
        self.account_store.rebalance(self.signal(day=23), self.executor, 10000)
        with self.assertRaises(ValueError):
            self.account_store.rebalance(self.signal(), self.executor, 10000)
        account = {'date': '2026-11-19', 'cash': '1000', 'positions': {'C': {'quantity': 100, 'sellable': 100}}}
        with self.assertRaises(ValueError):
            self.executor.rebalance(self.signal(), account)

    def test_task_retries_without_market_and_recovers_csv(self):
        (self.root / 'config').mkdir()
        global_config = {'timezone': 'Asia/Shanghai', 'market': {}, 'storage': {'data_dir': 'data'}, 'logging': {}}
        config = {'strategy': 'etf_rotation', 'instances': [{'id': 'pap', 'enabled': True, 'mode': 'pap',
                  'initial_cash': 10000, 'execution': {'commission_rate': .0002, 'minimum_commission': 5, 'slippage_rate': 0}}],
                  'parameters': {}, 'instruments': self.rules}
        (self.root / 'config/global.json').write_text(json.dumps(global_config))
        path = self.root / 'config/strategy.json'
        path.write_text(json.dumps(config))
        with patch('market_trading_bot.runtime.TradingTask.strategy_logging'), \
                patch('market_trading_bot.runtime.TradingTask.datetime') as clock, \
                patch('market_trading_bot.runtime.TradingTask.ETFRotationStrategy') as strategy, \
                patch('market_trading_bot.runtime.TradingTask.AKShareMarket') as market:
            clock.now.return_value = self.signal().as_of
            strategy.return_value.calculate.return_value = self.signal()
            market.return_value.load.return_value = self.signal().as_of, {}
            with patch.object(PapAccountStore, 'export_csv', side_effect=OSError('disk full')):
                first = TradingTask(self.root, path).run_pap('pap')
            self.assertFalse(first['csv_exported'])
            with patch.object(PapAccountStore, 'export_csv', side_effect=sqlite3.OperationalError('database is locked')):
                self.assertFalse(TradingTask(self.root, path).run_pap('pap')['csv_exported'])
            market.reset_mock()
            second = TradingTask(self.root, path).run_pap('pap')
            market.assert_not_called()
            self.assertTrue(second['csv_exported'])
            self.assertEqual(first['fills'], second['fills'])
        config['instances'][0]['mode'] = 'pro'
        path.write_text(json.dumps(config))
        with patch('market_trading_bot.runtime.TradingTask.strategy_logging'), self.assertRaises(ValueError):
            TradingTask(self.root, path).run_pap('pap')


if __name__ == '__main__':
    unittest.main()
