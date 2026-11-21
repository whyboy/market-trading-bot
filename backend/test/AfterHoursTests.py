"""验证收盘后和周末的部署测试链路，外部行情和推送使用替身。"""
import importlib
import json
import math
import pandas as pd
import sqlite3
import tempfile
import threading
import unittest
from datetime import date, datetime, timedelta
from fastapi.testclient import TestClient
from market_trading_bot import app as app_module
from market_trading_bot.market.AKShareMarket import AKShareMarket, MarketDataError, SHANGHAI
from market_trading_bot.TaskScheduler import TaskScheduler
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1] / 'dev'
MARKET = importlib.import_module('market_trading_bot.market.AKShareMarket')


class AfterHoursTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        directory = self.root / 'market_trading_bot/config'
        directory.mkdir(parents=True)
        settings = json.loads((ROOT / 'market_trading_bot/config/global.json').read_text())
        settings['runtime']['test_mode'] = True
        settings['storage']['data_dir'] = 'data/test'
        settings['logging']['directory'] = 'logs/test'
        settings['market']['request_interval_seconds'] = 0
        settings['pushplus']['token'] = 'test-token'
        self.settings = settings
        self.config = json.loads((ROOT / 'market_trading_bot/config/etf_rotation_strategy.json').read_text())
        self.config['schedule']['trade_time'] = '19:30'
        (directory / 'global.json').write_text(json.dumps(settings))
        (directory / 'etf_rotation_strategy.json').write_text(json.dumps(self.config))
        self.days = pd.bdate_range(end='2026-11-23', periods=40).date.tolist()
        self.daily = pd.DataFrame({'日期': self.days,
                                   '收盘': [100 * math.exp(day * .001) for day in range(len(self.days))]})
        self.current = datetime(2026, 11, 21, 19, 23, tzinfo=SHANGHAI)
        patches = (
            patch.object(app_module, 'ROOT', self.root),
            patch.object(TaskScheduler, 'now', side_effect=lambda: self.current),
            patch.object(MARKET, 'datetime', wraps=datetime),
            patch.object(MARKET.ak, 'calendar_trade_date_hist_sina', return_value=pd.DataFrame({'trade_date': self.days})),
            patch.object(MARKET.ak, 'fund_etf_hist_em', side_effect=self.history),
            patch.object(MARKET.requests, 'get'),
            patch('market_trading_bot.notification.PushPlusClient.PushPlusClient.send', return_value='receipt'),
        )
        mocks = []
        for item in patches:
            mocks.append(item.start())
            self.addCleanup(item.stop)
        self.clock, self.calendar, self.histories, self.spot, self.send = mocks[2:]
        self.clock.now.side_effect = lambda *args: self.current

    def history(self, **kwargs):
        days = pd.to_datetime(self.daily['日期']).dt.strftime('%Y%m%d')
        return self.daily[(days >= kwargs['start_date']) & (days <= kwargs['end_date'])].copy()

    def test_weekend_schedule_executes_trade_and_frontend_reads_test_data(self):
        production = self.root / 'data/etf_rotation_pap/ledger.sqlite3'
        production.parent.mkdir(parents=True)
        production.write_bytes(b'production-marker')
        with TestClient(app_module.app) as client:
            scheduler = app_module.app.state.scheduler
            scheduler.timer.pause()
            with scheduler.calendar_lock:
                scheduler.prepare_day()
            job = scheduler.timer.get_job('etf_rotation')
            self.assertEqual(job.next_run_time, self.current.replace(hour=19, minute=30))
            self.assertIsNone(scheduler.timer.get_job('_daily_valuation'))
            self.assertIsNone(scheduler.timer.get_job('_daily_report'))
            self.calendar.assert_not_called()
            self.current = self.current.replace(minute=30)
            job.func(*job.args)
            task = scheduler.tasks[('etf_rotation', 'etf_rotation_pap')]
            task.thread.join(timeout=3)
            self.assertFalse(task.thread.is_alive())
            self.assertTrue(task.result['success'])
            self.assertTrue(task.output['csv_exported'])
            self.assertTrue(task.output['fills'])
            self.spot.assert_not_called()
            self.assertEqual(task.output['signal']['ranking'][0]['quote_time'], '2026-11-20 15:00:00+08:00')
            self.assertTrue(client.get('/api/accounts/etf_rotation_pap').json()['initialized'])
            status = client.get('/api/runtime?strategy_id=etf_rotation&instance_id=etf_rotation_pap').json()
            self.assertTrue(status['last_execution']['success'])
            days = client.get('/api/strategies/etf_rotation/logs').json()
            log = client.get(f'/api/strategies/etf_rotation/logs/{days[0]}').json()['content']
            for text in ('任务开始', '链路测试模式', '行情日期=2026-11-20', '策略计算完成', '任务完成'):
                self.assertIn(text, log)
            self.assertEqual(self.send.call_args.args[0], 'etf_rotation｜模拟交易完成')
            self.assertIn('成交：', self.send.call_args.args[1])
            database = self.root / 'data/test/etf_rotation_pap/ledger.sqlite3'
            with sqlite3.connect(database) as connection:
                self.assertEqual(connection.execute('SELECT day FROM daily_results').fetchone()[0], '2026-11-21')
            self.assertEqual(production.read_bytes(), b'production-marker')
            self.assertFalse((self.root / 'logs/etf_rotation').exists())

    def test_test_quotes_use_completed_day_without_duplicate_history(self):
        market = AKShareMarket(self.settings['market'], test_mode=True)
        cases = ((datetime(2026, 11, 20, 19, 30, tzinfo=SHANGHAI), date(2026, 11, 20)),
                 (datetime(2026, 11, 21, 19, 30, tzinfo=SHANGHAI), date(2026, 11, 20)),
                 (datetime(2026, 11, 23, 10, 0, tzinfo=SHANGHAI), date(2026, 11, 20)))
        for current, close_day in cases:
            with self.subTest(current=current):
                self.current = current
                as_of, snapshots = market.load(tuple(self.config['parameters']['etf_pool']), 24)
                self.assertEqual(as_of, current)
                for snapshot in snapshots.values():
                    self.assertEqual(snapshot.quote_time.date(), close_day)
                    self.assertEqual(len(snapshot.history), 24)
                    self.assertLess(snapshot.history[-1].date, close_day)
        self.spot.assert_not_called()

    def test_real_timer_runs_full_chain_after_startup(self):
        now = datetime.now(SHANGHAI)
        due = now + timedelta(seconds=5)
        if due.date() != now.date():
            self.skipTest('跨午夜时由次日日历回调安排任务')
        self.days = pd.bdate_range(end=now.date() + timedelta(days=7), periods=60).date.tolist()
        self.daily = pd.DataFrame({'日期': self.days,
                                   '收盘': [100 * math.exp(day * .001) for day in range(len(self.days))]})
        self.calendar.return_value = pd.DataFrame({'trade_date': self.days})
        self.clock.now.side_effect = lambda *args: datetime.now(SHANGHAI)
        self.config['schedule']['trade_time'] = due.strftime('%H:%M:%S')
        path = self.root / 'market_trading_bot/config/etf_rotation_strategy.json'
        path.write_text(json.dumps(self.config))
        finished = threading.Event()
        self.send.side_effect = lambda *args: finished.set() or 'receipt'
        with patch.object(TaskScheduler, 'now', side_effect=lambda: datetime.now(SHANGHAI)):
            with TestClient(app_module.app) as client:
                self.assertTrue(finished.wait(12), '到点后交易任务未完成')
                status = client.get('/api/runtime?strategy_id=etf_rotation').json()
                self.assertTrue(status['last_execution']['success'])
                self.assertTrue(client.get('/api/accounts/etf_rotation_pap').json()['initialized'])
                self.assertTrue(client.get('/api/strategies/etf_rotation/logs').json())

    def test_failed_market_request_is_visible_in_frontend_logs(self):
        self.histories.side_effect = RuntimeError('行情测试失败')
        with TestClient(app_module.app) as client:
            scheduler = app_module.app.state.scheduler
            scheduler.timer.pause()
            with scheduler.calendar_lock:
                scheduler.prepare_day()
            job = scheduler.timer.get_job('etf_rotation')
            self.current = self.current.replace(minute=30)
            job.func(*job.args)
            task = scheduler.tasks[('etf_rotation', 'etf_rotation_pap')]
            task.thread.join(timeout=3)
            status = client.get('/api/runtime?strategy_id=etf_rotation').json()
            self.assertFalse(status['last_execution']['success'])
            days = client.get('/api/strategies/etf_rotation/logs').json()
            log = client.get(f'/api/strategies/etf_rotation/logs/{days[0]}').json()['content']
            self.assertIn('行情测试失败', log)
            self.assertFalse(client.get('/api/accounts/etf_rotation_pap').json()['initialized'])

    def test_test_mode_schedules_before_nine_and_skips_missed_time(self):
        for hour, due_hour in ((8, 9), (20, 19)):
            with self.subTest(hour=hour):
                self.current = self.current.replace(hour=hour)
                self.config['schedule']['trade_time'] = f'{due_hour:02d}:30'
                path = self.root / 'market_trading_bot/config/etf_rotation_strategy.json'
                path.write_text(json.dumps(self.config))
                with TestClient(app_module.app):
                    scheduler = app_module.app.state.scheduler
                    scheduler.timer.pause()
                    with scheduler.calendar_lock:
                        scheduler.prepare_day()
                    job = scheduler.timer.get_job('etf_rotation')
                    if hour < due_hour:
                        self.assertEqual(job.next_run_time, self.current.replace(hour=due_hour, minute=30))
                    else:
                        self.assertIsNone(job)
        self.histories.assert_not_called()

    def test_normal_mode_rejects_after_hours(self):
        market = AKShareMarket(self.settings['market'])
        with self.assertRaisesRegex(MarketDataError, '连续交易时段'):
            market.load(tuple(self.config['parameters']['etf_pool']), 24)
        self.histories.assert_not_called()

    def test_bad_test_history_and_closing_prices_fail_instead_of_falling_back(self):
        market = AKShareMarket(self.settings['market'], test_mode=True)
        original = self.daily.copy()
        for invalid in ('missing_close', 'zero_close', 'missing_history'):
            with self.subTest(invalid=invalid):
                self.daily = original.copy()
                if invalid == 'missing_close':
                    self.daily = self.daily[self.daily['日期'] != date(2026, 11, 20)]
                elif invalid == 'zero_close':
                    self.daily.loc[self.daily['日期'] == date(2026, 11, 20), '收盘'] = 0
                else:
                    self.daily = self.daily[self.daily['日期'] != date(2026, 11, 19)]
                with self.assertRaises(MarketDataError):
                    market.load(tuple(self.config['parameters']['etf_pool']), 24)
