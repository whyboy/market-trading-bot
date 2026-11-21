"""验证两表结构、旧账本迁移和交易/估值字段的独立更新。"""
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime
from market_trading_bot.repository.PapAccountRepository import PapAccountRepository
from pathlib import Path


class PapAccountDailyResultsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.execution = {'date': '2026-11-20', 'cash': '100000', 'equity': '100000', 'positions': {}, 'fills': []}

    def seed_legacy(self, duplicate=False):
        directory = self.root / 'pap'
        directory.mkdir()
        database = directory / 'ledger.sqlite3'
        with sqlite3.connect(database) as connection, connection:
            connection.execute('CREATE TABLE account (id INTEGER PRIMARY KEY, initial_cash TEXT NOT NULL)')
            connection.execute("INSERT INTO account VALUES(1, '100000')")
            connection.execute('CREATE TABLE runs (day TEXT, payload TEXT)')
            connection.execute('INSERT INTO runs VALUES (?, ?)', ('2026-11-20', json.dumps(self.execution)))
            if duplicate:
                connection.execute('INSERT INTO runs VALUES (?, ?)', ('2026-11-20', json.dumps(self.execution)))
        return database

    def test_new_account_has_two_tables_and_creation_metadata(self):
        repository = PapAccountRepository(self.root, 'pap', 'rotation')
        with repository.begin_rebalance_transaction('2026-11-20', 100000) as transaction:
            transaction.save_execution_result(self.execution)
        with sqlite3.connect(repository.database) as connection:
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            metadata = connection.execute('SELECT instance_id, strategy_id, created_at FROM account').fetchone()
        self.assertEqual(tables, {'account', 'daily_results'})
        self.assertEqual(metadata[:2], ('pap', 'rotation'))
        self.assertIsNotNone(datetime.fromisoformat(metadata[2]).tzinfo)

    def test_legacy_migration_preserves_all_data_and_can_be_reopened(self):
        database = self.seed_legacy()
        readonly = PapAccountRepository.get_latest_account(self.root, 'pap')
        self.assertEqual(readonly['initial_cash'], '100000')
        repository = PapAccountRepository(self.root, 'pap', 'rotation')
        self.assertEqual(repository.find_execution_by_date('2026-11-20'), self.execution)
        reopened = PapAccountRepository(self.root, 'pap', 'rotation')
        self.assertEqual(reopened.find_execution_by_date('2026-11-20'), self.execution)
        with sqlite3.connect(database) as connection:
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertEqual(connection.execute('SELECT initial_cash FROM account').fetchone()[0], '100000')
        self.assertEqual(tables, {'account', 'daily_results'})

    def test_failed_migration_rolls_back_schema_changes_and_keeps_runs(self):
        database = self.seed_legacy(duplicate=True)
        with self.assertRaises(sqlite3.IntegrityError):
            PapAccountRepository(self.root, 'pap', 'rotation')
        with sqlite3.connect(database) as connection:
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM runs').fetchone()[0], 2)
            columns = {row[1] for row in connection.execute('PRAGMA table_info(account)')}
        self.assertEqual(tables, {'account', 'runs'})
        self.assertEqual(columns, {'id', 'initial_cash'})

    def test_valuation_only_row_does_not_prevent_actual_execution(self):
        repository = PapAccountRepository(self.root, 'pap', 'rotation')
        repository.save_daily_valuation({'date': '2026-11-20', 'equity': '100500'})
        self.assertIsNone(repository.find_execution_by_date('2026-11-20'))
        with repository.begin_rebalance_transaction('2026-11-20', 100000) as transaction:
            self.assertIsNone(transaction.completed)
            transaction.save_execution_result(self.execution)
        result = repository.get_daily_result(self.root, 'pap', '2026-11-20')
        self.assertEqual(result['execution'], self.execution)
        self.assertEqual(result['valuation_status'], 'pending')
        self.assertIsNone(result['valuation'])

    def test_valuation_updates_keep_execution_and_do_not_create_extra_rows(self):
        repository = PapAccountRepository(self.root, 'pap', 'rotation')
        with repository.begin_rebalance_transaction('2026-11-20', 100000) as transaction:
            transaction.save_execution_result(self.execution)
        repository.save_daily_valuation({'date': '2026-11-20', 'equity': '100500'})
        repository.save_daily_valuation({'date': '2026-11-20', 'equity': '100800'})
        result = repository.get_daily_result(self.root, 'pap', '2026-11-20')
        self.assertEqual(result['execution'], self.execution)
        self.assertEqual(result['valuation']['equity'], '100800')
        with sqlite3.connect(repository.database) as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM daily_results').fetchone()[0], 1)

    def test_duplicate_execution_cannot_overwrite_committed_trade(self):
        repository = PapAccountRepository(self.root, 'pap', 'rotation')
        with repository.begin_rebalance_transaction('2026-11-20', 100000) as transaction:
            transaction.save_execution_result(self.execution)
            with self.assertRaises(ValueError):
                transaction.save_execution_result({**self.execution, 'cash': '0'})
        self.assertEqual(repository.find_execution_by_date('2026-11-20')['cash'], '100000')

    def test_account_cannot_be_reassigned_to_another_strategy(self):
        repository = PapAccountRepository(self.root, 'pap', 'rotation')
        with repository.begin_rebalance_transaction('2026-11-20', 100000) as transaction:
            transaction.save_execution_result(self.execution)
        with self.assertRaises(ValueError):
            PapAccountRepository(self.root, 'pap', 'other')
