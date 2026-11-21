"""监控接口不得创建账户，且必须识别失联心跳和限制日志路径。"""
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from fastapi.testclient import TestClient
from market_trading_bot.runtime.RuntimeStore import RuntimeStore
from market_trading_bot.web import app as web
from pathlib import Path
from unittest.mock import patch


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / 'config').mkdir()
        (self.root / 'config/global.json').write_text(json.dumps({
            'storage': {'data_dir': 'data'}, 'logging': {'directory': 'logs'}}))
        (self.root / 'config/example_strategy.json').write_text(json.dumps({
            'strategy': 'example', 'instances': [{'id': 'example_pap', 'mode': 'pap', 'enabled': True}]}))
        self.patch = patch.object(web, 'ROOT', self.root)
        self.patch.start()
        self.client = TestClient(web.app)

    def tearDown(self):
        self.patch.stop()
        self.temporary.cleanup()

    def test_missing_account_is_read_only(self):
        self.assertEqual(self.client.get('/api/accounts/example_pap').json(), {'initialized': False})
        self.assertFalse((self.root / 'data').exists())
        self.assertEqual(self.client.get('/api/accounts/unknown').status_code, 404)

    def test_heartbeat_staleness_and_stop(self):
        self.assertFalse(self.client.get('/api/runtime').json()['online'])
        now = datetime.now(timezone.utc)
        store = RuntimeStore(self.root)
        status = {'state': 'idle', 'started_at': now.isoformat(), 'heartbeat_at': now.isoformat()}
        store.write(status)
        self.assertTrue(self.client.get('/api/runtime').json()['online'])
        store.write({**status, 'heartbeat_at': (now - timedelta(seconds=30)).isoformat()})
        self.assertFalse(self.client.get('/api/runtime').json()['online'])
        store.write({**status, 'state': 'stopped'})
        self.assertFalse(self.client.get('/api/runtime').json()['online'])

    def test_logs_are_ordered_paginated_and_validated(self):
        directory = self.root / 'logs/example'
        directory.mkdir(parents=True)
        content = '交易运行正常\n' * 10000
        (directory / '2026-11-20.log').write_text(content)
        (directory / '2026-11-19.log').touch()
        (directory / 'other.log').touch()
        self.assertEqual(self.client.get('/api/strategies/example/logs').json(), ['2026-11-20', '2026-11-19'])
        first = self.client.get('/api/strategies/example/logs/2026-11-20').json()
        self.assertTrue(first['has_more'])
        combined = first['content']
        page = first
        while page['has_more']:
            page = self.client.get(f"/api/strategies/example/logs/2026-11-20?offset={page['next_offset']}").json()
            combined += page['content']
        self.assertEqual(combined, content)
        self.assertEqual(self.client.get('/api/strategies/unknown/logs').status_code, 404)
        self.assertEqual(self.client.get('/api/strategies/example/logs/not-a-date').status_code, 422)

    def test_latest_account_snapshot(self):
        database = self.root / 'data/example_pap/ledger.sqlite3'
        database.parent.mkdir(parents=True)
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute('CREATE TABLE runs(day TEXT, payload TEXT)')
            connection.execute('CREATE TABLE account(id INTEGER, initial_cash TEXT)')
            connection.execute("INSERT INTO account VALUES(1, '100000')")
            connection.execute('INSERT INTO runs VALUES(?, ?)', ('2026-11-20', json.dumps({
                'date': '2026-11-20', 'cash': '50000', 'equity': '101000', 'positions': {}})))
        result = self.client.get('/api/accounts/example_pap').json()
        self.assertTrue(result['initialized'])
        self.assertEqual(result['initial_cash'], '100000')
        self.assertEqual(result['equity'], '101000')
