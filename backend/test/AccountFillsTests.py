"""验证账户成交记录的读取和排序。"""
import json
import sqlite3
import tempfile
import unittest
from market_trading_bot.service.AccountService import AccountService
from pathlib import Path


class AccountFillsTests(unittest.TestCase):
    def test_recent_fills_skip_empty_days_and_preserve_order(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(AccountService.get_recent_fills(root, 'account'), {'items': [], 'has_more': False})
            service = AccountService(root, 'account')
            with sqlite3.connect(service.database) as connection:
                for day, fills in [('2026-11-18', [{'fill_id': 'old'}]), ('2026-11-19', []),
                                   ('2026-11-20', [{'fill_id': 'first'}, {'fill_id': 'last'}])]:
                    connection.execute('INSERT INTO daily_results(day, execution_payload) VALUES (?, ?)',
                                       (day, json.dumps({'date': day, 'fills': fills})))
            page = AccountService.get_recent_fills(root, 'account', 2)
            fills = page['items']
            self.assertEqual([fill['fill_id'] for fill in fills], ['last', 'first'])
            self.assertEqual(fills[0]['date'], '2026-11-20')
            self.assertTrue(page['has_more'])
            next_page = AccountService.get_recent_fills(root, 'account', 2, 2)
            self.assertEqual([fill['fill_id'] for fill in next_page['items']], ['old'])
            self.assertFalse(next_page['has_more'])
            with self.assertRaises(ValueError):
                AccountService.get_recent_fills(root, '../account')
