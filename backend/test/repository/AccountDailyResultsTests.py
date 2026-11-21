"""验证交易和估值字段的独立更新。"""
import sqlite3
import tempfile
import unittest
from market_trading_bot.service.AccountService import AccountService
from market_trading_bot.service.DailyResultsService import DailyResultsService
from pathlib import Path


class AccountDailyResultsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.execution = {'date': '2026-11-20', 'cash': '100000', 'equity': '100000', 'positions': {}, 'fills': []}

    def test_valuation_only_row_does_not_prevent_actual_execution(self):
        repository = AccountService(self.root, 'TradeExecutor', 'rotation')
        DailyResultsService(self.root, 'TradeExecutor').save_valuation({'date': '2026-11-20', 'equity': '100500'})
        self.assertIsNone(repository.get_execution('2026-11-20'))
        with repository.transaction('2026-11-20', 100000) as transaction:
            self.assertIsNone(transaction.completed)
            transaction.save_execution(self.execution)
        result = DailyResultsService.get_daily_result(self.root, 'TradeExecutor', '2026-11-20')
        self.assertEqual(result['execution'], self.execution)
        self.assertEqual(result['valuation_status'], 'pending')
        self.assertIsNone(result['valuation'])

    def test_valuation_updates_keep_execution_and_do_not_create_extra_rows(self):
        repository = AccountService(self.root, 'TradeExecutor', 'rotation')
        with repository.transaction('2026-11-20', 100000) as transaction:
            transaction.save_execution(self.execution)
        DailyResultsService(self.root, 'TradeExecutor').save_valuation({'date': '2026-11-20', 'equity': '100500'})
        DailyResultsService(self.root, 'TradeExecutor').save_valuation({'date': '2026-11-20', 'equity': '100800'})
        result = DailyResultsService.get_daily_result(self.root, 'TradeExecutor', '2026-11-20')
        self.assertEqual(result['execution'], self.execution)
        self.assertEqual(result['valuation']['equity'], '100800')
        with sqlite3.connect(repository.database) as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM daily_results').fetchone()[0], 1)

    def test_duplicate_execution_cannot_overwrite_committed_trade(self):
        repository = AccountService(self.root, 'TradeExecutor', 'rotation')
        with repository.transaction('2026-11-20', 100000) as transaction:
            transaction.save_execution(self.execution)
            with self.assertRaises(ValueError):
                transaction.save_execution({**self.execution, 'cash': '0'})
        self.assertEqual(repository.get_execution('2026-11-20')['cash'], '100000')
