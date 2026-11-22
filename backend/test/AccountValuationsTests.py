"""验证账户估值历史只返回已完成的记录。"""
import json
import sqlite3
import tempfile
import unittest
from market_trading_bot.service.AccountService import AccountService
from pathlib import Path


class AccountValuationsTests(unittest.TestCase):
    def test_read_successful_valuations_in_date_order(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(AccountService.get_valuations(root, 'account'), [])
            service = AccountService(root, 'account')
            with sqlite3.connect(service.database) as connection:
                for day, status in [('2026-11-20', 'success'), ('2026-11-19', 'success'), ('2026-11-21', 'failed')]:
                    payload = json.dumps({'date': day, 'cumulative_return_pct': '1.5'})
                    connection.execute('INSERT INTO daily_results(day, valuation_payload, valuation_status) VALUES (?, ?, ?)',
                                       (day, payload, status))
            result = AccountService.get_valuations(root, 'account')
            self.assertEqual([item['date'] for item in result], ['2026-11-19', '2026-11-20'])
            with self.assertRaises(ValueError):
                AccountService.get_valuations(root, '../account')
