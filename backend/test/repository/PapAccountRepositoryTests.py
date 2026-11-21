"""验证模拟资金守恒、恢复、并发去重和失败重跑，不访问外部行情。"""
import csv
import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from market_trading_bot.service.AccountLedger import AccountLedger
from PapFixture import PapFixture
from pathlib import Path
from unittest.mock import Mock, patch


class PapAccountRepositoryTests(PapFixture, unittest.TestCase):
    def test_restore_sell_before_buy_and_t_plus_zero(self):
        self.rebalance(self.signal(), self.executor, 10000)
        restored = AccountLedger(self.root, 'pap')
        result = self.rebalance(self.signal(day=23, winner='B'), self.executor, 999999, repository=restored)
        self.assertEqual([fill['side'] for fill in result['fills']], ['sell', 'buy'])
        self.assertNotIn('A', result['positions'])
        self.assertEqual(result['positions']['B'], {'quantity': 400, 'sellable': 400})
        self.assertEqual(result['cash'], '1985.00')
        self.assertEqual(result['equity'], '9985.00')

    def test_concurrent_retry_does_not_duplicate_fills(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.rebalance(self.signal(), self.executor, 10000), range(8)))
        self.assertTrue(all(result == results[0] for result in results))
        self.account_store.export_trades()
        with (self.account_store.directory / 'account_trades.csv').open(encoding='utf-8-sig') as stream:
            self.assertEqual(len(list(csv.DictReader(stream))), 1)

    def test_failure_rolls_back_and_can_retry(self):
        broken = Mock()
        broken.rebalance.side_effect = RuntimeError('interrupted')
        broken.money = self.executor.money
        with self.assertRaises(RuntimeError):
            self.rebalance(self.signal(), broken, 10000)
        self.assertIsNone(self.account_store.get_execution('2026-11-20'))
        self.assertEqual(len(self.rebalance(self.signal(), self.executor, 10000)['fills']), 1)

    def test_failed_insert_rolls_back(self):
        broken_signal = self.signal()
        broken_signal = replace(broken_signal, ranking=(replace(broken_signal.ranking[0], score=float('nan')), *broken_signal.ranking[1:]))
        with self.assertRaises(ValueError):
            self.rebalance(broken_signal, self.executor, 10000)
        self.assertIsNone(self.account_store.get_execution('2026-11-20'))

    def test_exports_can_be_rebuilt_and_instances_are_isolated(self):
        self.rebalance(self.signal(), self.executor, 10000)
        self.account_store.export_trades()
        (self.account_store.directory / 'account_trades.csv').write_text('broken')
        self.account_store.export_trades()
        with (self.account_store.directory / 'account_trades.csv').open(encoding='utf-8-sig') as stream:
            self.assertEqual(next(csv.DictReader(stream))['buy_quantity'], '900')
        self.assertIsNone(AccountLedger(self.root, 'other').get_execution('2026-11-20'))

    def read_trades(self):
        with (self.account_store.directory / 'account_trades.csv').open(encoding='utf-8-sig') as stream:
            return list(csv.DictReader(stream))

    def test_summary_skips_no_fills_and_uses_actual_initial_cash(self):
        self.rebalance(self.signal(weight=0), self.executor, 10000)
        with sqlite3.connect(self.account_store.database) as connection:
            self.assertEqual(connection.execute('SELECT initial_cash FROM account').fetchone()[0], '10000.00')
        self.account_store.export_trades()
        self.assertEqual(self.read_trades(), [])
        self.rebalance(self.signal(day=23), self.executor, 999999)
        self.rebalance(self.signal(day=24), self.executor, 999999)
        self.account_store.export_trades()
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
        self.assertIsNotNone(self.account_store.get_execution('2026-11-24'))

    def test_legacy_initial_cash_is_restored_and_saved_once(self):
        self.rebalance(self.signal(), self.executor, 10000)
        with sqlite3.connect(self.account_store.database) as connection, connection:
            connection.execute('DELETE FROM account')
        self.account_store.export_trades()
        with sqlite3.connect(self.account_store.database) as connection:
            self.assertEqual(connection.execute('SELECT initial_cash FROM account').fetchone()[0], '10000.00')
        with patch.object(AccountLedger, '_infer_initial_cash', side_effect=AssertionError('不应再次反推')):
            self.account_store.export_trades()

    def test_summary_includes_rotation_and_liquidation(self):
        self.rebalance(self.signal(), self.executor, 10000)
        self.rebalance(self.signal(day=23, winner='B'), self.executor, 10000)
        self.rebalance(self.signal(day=24, weight=0), self.executor, 10000)
        (self.account_store.directory / 'fills.csv').write_text('old export')
        self.account_store.export_trades()
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
        self.rebalance(self.signal(), self.executor, 10000)
        self.rebalance(self.signal(day=23, prices=(12, 20)), self.executor, 10000)
        self.rebalance(self.signal(day=24, winner='B', prices=(12, 20)), self.executor, 10000)
        self.account_store.export_trades()
        rows = self.read_trades()
        self.assertEqual([row['date'] for row in rows], ['2026-11-20', '2026-11-24'])
        self.assertEqual(rows[-1]['total_assets'], '11785.00')
        self.assertEqual(rows[-1]['initial_cash'], '10000.00')
        self.assertEqual(rows[-1]['cumulative_pnl'], '1785.00')
        self.assertEqual(rows[-1]['cumulative_return_pct'], '17.850000')

    def test_failed_export_preserves_previous_csv(self):
        self.rebalance(self.signal(), self.executor, 10000)
        self.account_store.export_trades()
        path = self.account_store.directory / 'account_trades.csv'
        previous = path.read_bytes()
        self.rebalance(self.signal(day=23, winner='B'), self.executor, 10000)
        with patch.object(Path, 'replace', side_effect=OSError('interrupted')), self.assertRaises(OSError):
            self.account_store.export_trades()
        self.assertEqual(path.read_bytes(), previous)
        self.account_store.export_trades()
        self.assertEqual(len(self.read_trades()), 2)

    def test_task_retries_without_market_and_recovers_csv(self):
        task = self.make_task(self.signal())
        with patch.object(AccountLedger, 'export_trades', side_effect=OSError('disk full')):
            first = task.trade()
        self.assertFalse(first['csv_exported'])
        with patch.object(AccountLedger, 'export_trades', side_effect=sqlite3.OperationalError('database is locked')):
            self.assertFalse(task.trade()['csv_exported'])
        task.market.reset_mock()
        task.strategy.reset_mock()
        second = task.trade()
        task.market.load.assert_not_called()
        task.strategy.calculate.assert_not_called()
        self.assertTrue(second['csv_exported'])
        self.assertEqual(first['fills'], second['fills'])

    def test_transaction_without_result_rolls_back_initial_account(self):
        with self.assertRaisesRegex(RuntimeError, '必须保存'):
            with self.account_store.transaction('2026-11-20', 10000):
                pass
        self.assertIsNone(self.account_store.get_execution('2026-11-20'))
        with sqlite3.connect(self.account_store.database) as connection:
            self.assertIsNone(connection.execute('SELECT initial_cash FROM account').fetchone())
