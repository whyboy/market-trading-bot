"""监控接口不得创建账户，且必须识别失联心跳和限制日志路径。"""
import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from fastapi.testclient import TestClient
from market_trading_bot import app as app_module
from market_trading_bot.market.source.AKShareMarket import SHANGHAI
from market_trading_bot.task.TradingTask import TradingTask
from market_trading_bot.TaskScheduler import TaskScheduler
from pathlib import Path
from unittest.mock import Mock, patch


class AppTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / 'market_trading_bot/config').mkdir(parents=True)
        (self.root / 'market_trading_bot/config/global.json').write_text(json.dumps({
            'schedule': {'calendar_time': '09:00', 'valuation_time': '15:05'},
            'pushplus': {'token': 'test-token'}, 'market': {'provider': 'akshare_eastmoney',
                'max_quote_age_seconds': 60, 'history_lookback_days': 60,
                'request_timeout_seconds': 15, 'request_interval_seconds': 0},
            'storage': {'data_dir': 'data'}, 'logging': {'directory': 'logs', 'level': 'INFO', 'retention_dates': 30}}))
        (self.root / 'market_trading_bot/config/example_strategy.json').write_text(json.dumps({
            'strategy': 'example', 'schedule': {'trade_time': '14:30'}, 'instances': [{'id': 'example_pap', 'mode': 'pap', 'enabled': False}]}))
        self.patch = patch.object(app_module, 'ROOT', self.root)
        self.patch.start()
        self.client = TestClient(app_module.app)
        self.clock_patch = patch.object(TaskScheduler, 'now', return_value=datetime.now(SHANGHAI).replace(hour=8, minute=0))
        self.clock_patch.start()
        self.calendar_patch = patch('market_trading_bot.TaskScheduler.AKShareMarket.trading_calendar',
                                    side_effect=lambda: [TaskScheduler.now().date() - timedelta(days=1), TaskScheduler.now().date()])
        self.calendar_patch.start()
        def build(scheduler, strategy_id, instance, scheduled_at):
            return TradingTask(Mock(), Mock(), Mock(), Mock(), strategy_id=strategy_id,
                               instance_id=instance['id'], initial_cash=100000, scheduled_at=scheduled_at, notifier=Mock())
        self.build_patch = patch.object(TaskScheduler, '_build_trade_task', build)
        self.build_patch.start()

    def tearDown(self):
        self.calendar_patch.stop()
        self.build_patch.stop()
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
            connection.execute('CREATE TABLE daily_results(day TEXT, execution_payload TEXT)')
            connection.execute('CREATE TABLE account(id INTEGER, initial_cash TEXT)')
            connection.execute("INSERT INTO account VALUES(1, '100000')")
            connection.execute('INSERT INTO daily_results VALUES(?, ?)', ('2026-11-20', json.dumps({
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
            self.assertIsNone(app_module.app.state.scheduler.timer.get_job('example'))
            self.assertEqual(client.get('/api/runtime?strategy_id=example').json()['state'], 'disabled')

    def test_duplicate_service_is_rejected_and_lock_released_after_shutdown(self):
        with TestClient(app_module.app):
            duplicate = TaskScheduler(self.root)
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

        with patch('market_trading_bot.task.TradingTask.TradingTask.trade', autospec=True, side_effect=trade):
            with TestClient(app_module.app) as client:
                scheduler = app_module.app.state.scheduler
                scheduler.calendar_context = {'day': scheduler.now().date(), 'is_trading_day': True,
                                              'previous_trading_day': scheduler.now().date() - timedelta(days=1)}
                scheduler._start_trades('example', scheduler.now().date())
                try:
                    self.assertTrue(entered.wait(2))
                    result = client.get('/api/runtime?strategy_id=example').json()
                    self.assertEqual(result['state'], 'running')
                    self.assertEqual(result['heartbeat_scope'], 'task')
                    task = scheduler.tasks[('example', 'example_pap')]
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
                    scheduler.tasks[('example', 'example_pap')].thread.join(timeout=2)
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

        def trade(task):
            if task.instance_id == 'example_pap':
                entered.set()
                release.wait(3)
            else:
                finished.set()

        with patch('market_trading_bot.task.TradingTask.TradingTask.trade', autospec=True, side_effect=trade):
            with TestClient(app_module.app):
                scheduler = app_module.app.state.scheduler
                scheduler.calendar_context = {'day': scheduler.now().date(), 'is_trading_day': True,
                                              'previous_trading_day': scheduler.now().date() - timedelta(days=1)}
                scheduler._start_trades('example', scheduler.now().date())
                try:
                    self.assertTrue(entered.wait(2))
                    scheduler._start_trades('second', scheduler.now().date())
                    self.assertTrue(finished.wait(2))
                    self.assertNotEqual(scheduler.tasks[('example', 'example_pap')].thread.ident, scheduler.tasks[('second', 'second_pap')].thread.ident)
                finally:
                    release.set()
