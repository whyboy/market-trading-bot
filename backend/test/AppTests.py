"""监控接口不得创建账户，且必须识别失联心跳和限制日志路径。"""
import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from fastapi.testclient import TestClient
from market_trading_bot.TradingScheduler import TradingScheduler
from market_trading_bot import app as app_module
from pathlib import Path
from unittest.mock import patch


class AppTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / 'market_trading_bot/config').mkdir(parents=True)
        (self.root / 'market_trading_bot/config/global.json').write_text(json.dumps({
            'storage': {'data_dir': 'data'}, 'logging': {'directory': 'logs', 'level': 'INFO', 'retention_dates': 30}}))
        (self.root / 'market_trading_bot/config/example_strategy.json').write_text(json.dumps({
            'strategy': 'example', 'schedule': {'trade_time': '14:30'}, 'instances': [{'id': 'example_pap', 'mode': 'pap', 'enabled': False}]}))
        self.patch = patch.object(app_module, 'ROOT', self.root)
        self.patch.start()
        self.client = TestClient(app_module.app)
        self.clock_patch = patch.object(TradingScheduler, 'now', return_value=datetime.now().replace(hour=8, minute=0))
        self.clock_patch.start()

    def tearDown(self):
        self.clock_patch.stop()
        self.patch.stop()
        self.temporary.cleanup()

    def test_missing_account_is_read_only(self):
        self.assertEqual(self.client.get('/api/accounts/example_pap').json(), {'initialized': False})
        self.assertFalse((self.root / 'data').exists())
        self.assertEqual(self.client.get('/api/accounts/unknown').status_code, 404)

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

    def test_service_registers_jobs_without_starting_trade_threads(self):
        with TestClient(app_module.app) as client:
            scheduler = app_module.app.state.scheduler
            self.assertEqual(scheduler.tasks, {})
            self.assertTrue(scheduler.timer.running)
            result = client.get('/api/runtime?strategy_id=example').json()
            self.assertEqual(result['state'], 'idle')
            self.assertTrue(result['online'])
            self.assertEqual(result['heartbeat_scope'], 'scheduler')
        self.assertFalse(scheduler.timer.running)

    def test_disabled_strategy_is_not_registered(self):
        path = self.root / 'market_trading_bot/config/example_strategy.json'
        config = json.loads(path.read_text())
        config['enable'] = False
        path.write_text(json.dumps(config))
        with TestClient(app_module.app) as client:
            self.assertEqual(app_module.app.state.scheduler.jobs, {})
            self.assertEqual(client.get('/api/runtime?strategy_id=example').json()['state'], 'disabled')

    def test_duplicate_service_is_rejected_and_lock_released_after_shutdown(self):
        with TestClient(app_module.app):
            duplicate = TradingScheduler(self.root)
            with self.assertRaisesRegex(RuntimeError, '只启动一个后端实例'):
                duplicate.start()
        with TestClient(app_module.app) as client:
            self.assertTrue(client.get('/api/runtime?strategy_id=example').json()['online'])

    def test_active_task_has_its_own_heartbeat_and_timeout(self):
        path = self.root / 'market_trading_bot/config/example_strategy.json'
        config = json.loads(path.read_text())
        config['instances'][0]['enabled'] = True
        path.write_text(json.dumps(config))
        entered = threading.Event()
        release = threading.Event()

        def trade(*args):
            entered.set()
            release.wait(3)

        with patch('market_trading_bot.task.TradingTask.TradingTask.run_pap', side_effect=trade):
            with TestClient(app_module.app) as client:
                scheduler = app_module.app.state.scheduler
                scheduler.calendar_day = scheduler.now().date()
                scheduler.is_trading_day = True
                scheduler._start_task(path)
                try:
                    self.assertTrue(entered.wait(2))
                    result = client.get('/api/runtime?strategy_id=example').json()
                    self.assertEqual(result['state'], 'running')
                    self.assertEqual(result['heartbeat_scope'], 'task')
                    task = scheduler.tasks['example']
                    heartbeat = task.status['heartbeat_at']
                    task.status['current_execution'] = {**task.status['current_execution'],
                        'started_at': (datetime.now(timezone.utc) - timedelta(seconds=301)).isoformat()}
                    scheduler._heartbeat()
                    result = client.get('/api/runtime?strategy_id=example').json()
                    self.assertEqual(result['health'], 'trade_timeout')
                    self.assertFalse(result['online'])
                    self.assertEqual(task.status['heartbeat_at'], heartbeat)
                finally:
                    release.set()
                    scheduler.tasks['example'].thread.join(timeout=2)
                result = client.get('/api/runtime?strategy_id=example').json()
                self.assertEqual(result['state'], 'idle')
                self.assertTrue(result['last_execution']['success'])
                self.assertTrue(result['online'])

    def test_one_strategy_does_not_block_another(self):
        first = self.root / 'market_trading_bot/config/example_strategy.json'
        config = json.loads(first.read_text())
        config['instances'][0]['enabled'] = True
        first.write_text(json.dumps(config))
        second = self.root / 'market_trading_bot/config/second_strategy.json'
        second.write_text(json.dumps({'strategy': 'second', 'schedule': {'trade_time': '14:30'},
                                     'instances': [{'id': 'second_pap', 'enabled': True}]}))
        entered = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def trade(instance_id):
            if instance_id == 'example_pap':
                entered.set()
                release.wait(3)
            else:
                finished.set()

        with patch('market_trading_bot.task.TradingTask.TradingTask.run_pap', side_effect=trade):
            with TestClient(app_module.app):
                scheduler = app_module.app.state.scheduler
                scheduler.calendar_day = scheduler.now().date()
                scheduler.is_trading_day = True
                scheduler._start_task(first)
                try:
                    self.assertTrue(entered.wait(2))
                    scheduler._start_task(second)
                    self.assertTrue(finished.wait(2))
                    self.assertNotEqual(scheduler.tasks['example'].thread.ident, scheduler.tasks['second'].thread.ident)
                finally:
                    release.set()
