"""验证早晨统一判断交易日、按时创建一次性策略任务。"""
import json
import tempfile
import threading
import unittest
from datetime import date, datetime, timedelta
from market_trading_bot.AKShareMarket import SHANGHAI
from market_trading_bot.runtime.TradingScheduler import TradingScheduler
from market_trading_bot.runtime.TradingTask import TradingTask
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
DUE = datetime(2026, 11, 20, 14, 30, tzinfo=SHANGHAI)


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / 'config').mkdir()
        (self.root / 'config/global.json').write_text((ROOT / 'config/global.json').read_text())
        self.path = self.root / 'config/etf_rotation_strategy.json'
        self.config = json.loads((ROOT / 'config/etf_rotation_strategy.json').read_text())
        self.path.write_text(json.dumps(self.config))
        self.current = DUE.replace(hour=8, minute=0)
        self.clock = patch.object(TradingScheduler, 'now', side_effect=lambda: self.current)
        self.clock.start()
        self.calendar_patch = patch('market_trading_bot.runtime.TradingScheduler.AKShareMarket.trading_calendar',
                                    return_value=[date(2026, 11, 20), date(2026, 11, 24)])
        self.calendar = self.calendar_patch.start()
        self.scheduler = TradingScheduler(self.root)
        self.scheduler.start()
        self.scheduler.timer.pause()

    def tearDown(self):
        self.scheduler.stop()
        self.clock.stop()
        self.calendar_patch.stop()
        self.temporary.cleanup()

    def prepare(self):
        self.current = DUE.replace(hour=9, minute=0)
        self.scheduler.prepare_day()

    def test_daily_calendar_trigger_is_nine_am(self):
        trigger = self.scheduler.timer.get_job('_calendar').trigger
        morning = DUE.replace(hour=9, minute=0)
        self.assertEqual(trigger.get_next_fire_time(None, self.current), morning)
        self.assertEqual(trigger.get_next_fire_time(morning, morning + timedelta(seconds=1)), morning + timedelta(days=1))
        self.assertEqual(self.scheduler.tasks, {})
        self.assertEqual(self.scheduler.jobs, {})
        self.calendar.assert_not_called()

    def test_trading_day_registers_only_today_future_jobs(self):
        self.prepare()
        self.calendar.assert_called_once()
        self.assertTrue(self.scheduler.is_trading_day)
        self.assertEqual(self.scheduler.tasks, {})
        self.assertEqual(self.scheduler.jobs['etf_rotation'].next_run_time, DUE)
        self.scheduler.prepare_day()
        self.calendar.assert_called_once()

    def test_holiday_creates_no_jobs_or_tasks(self):
        self.current = DUE.replace(day=23, hour=9)
        self.scheduler.prepare_day()
        self.assertFalse(self.scheduler.is_trading_day)
        self.assertEqual(self.scheduler.jobs, {})
        with patch('market_trading_bot.runtime.TradingTask.TradingTask.run_pap') as trade:
            self.scheduler._start_task(self.path)
        trade.assert_not_called()
        self.assertEqual(self.scheduler.tasks, {})

    def test_calendar_failure_waits_five_seconds_then_registers_jobs(self):
        self.calendar.side_effect = [ValueError('网络失败'), [date(2026, 11, 20), date(2026, 11, 24)]]
        with patch.object(self.scheduler.stop_event, 'wait', return_value=False) as wait:
            self.prepare()
        wait.assert_called_once_with(5)
        self.assertIsNone(self.scheduler.calendar_error)
        self.assertIn('etf_rotation', self.scheduler.jobs)

    def test_late_calendar_recovery_does_not_run_past_strategy_time(self):
        self.calendar.side_effect = [ValueError('网络失败'), [date(2026, 11, 20), date(2026, 11, 24)]]

        def wait(seconds):
            self.current = DUE + timedelta(seconds=1)
            return False

        with patch.object(self.scheduler.stop_event, 'wait', side_effect=wait):
            self.prepare()
        self.assertTrue(self.scheduler.is_trading_day)
        self.assertEqual(self.scheduler.jobs, {})
        self.assertEqual(self.scheduler.tasks, {})

    def test_calendar_retry_stops_at_close(self):
        self.calendar.side_effect = ValueError('网络失败')

        def wait(seconds):
            self.current = DUE.replace(hour=15)
            return False

        with patch.object(self.scheduler.stop_event, 'wait', side_effect=wait):
            self.prepare()
        self.calendar.assert_called_once()
        self.assertFalse(self.scheduler.jobs)

    def test_calendar_retry_can_be_stopped(self):
        self.calendar.side_effect = ValueError('网络失败')
        with patch.object(self.scheduler.stop_event, 'wait', return_value=True):
            self.prepare()
        self.calendar.assert_called_once()
        self.assertFalse(self.scheduler.jobs)

    def test_previous_day_callback_does_not_create_task(self):
        self.prepare()
        self.current = DUE + timedelta(days=1)
        self.scheduler._start_task(self.path, DUE.date())
        self.assertEqual(self.scheduler.tasks, {})

    def test_task_only_executes_strategy_and_exits(self):
        task = TradingTask(self.root, self.path)
        thread_ids = []
        with patch('market_trading_bot.runtime.TradingTask.TradingTask.run_pap', side_effect=lambda *args: thread_ids.append(threading.get_ident())):
            task.start()
            task.thread.join(timeout=2)
        self.assertFalse(task.thread.is_alive())
        self.assertEqual(thread_ids, [task.thread.ident])
        self.assertTrue(task.status['last_execution']['success'])
        self.calendar.assert_not_called()

    def test_new_task_per_launch_and_no_overlapping_strategy(self):
        self.prepare()
        entered = threading.Event()
        release = threading.Event()

        def trade(*args):
            entered.set()
            release.wait(3)

        with patch('market_trading_bot.runtime.TradingTask.TradingTask.run_pap', side_effect=trade):
            self.scheduler._start_task(self.path)
            try:
                self.assertTrue(entered.wait(2))
                first = self.scheduler.tasks['etf_rotation']
                self.scheduler._start_task(self.path)
                self.assertIs(self.scheduler.tasks['etf_rotation'], first)
            finally:
                release.set()
                first.thread.join(timeout=2)
            self.scheduler._start_task(self.path)
            second = self.scheduler.tasks['etf_rotation']
            second.thread.join(timeout=2)
            self.assertIsNot(first, second)

    def test_failed_account_does_not_prevent_next_account(self):
        self.config['instances'].append({**self.config['instances'][0], 'id': 'second'})
        self.path.write_text(json.dumps(self.config))
        task = TradingTask(self.root, self.path)
        with patch('market_trading_bot.runtime.TradingTask.TradingTask.run_pap', side_effect=[ValueError('策略失败'), {}]) as trade:
            task.start()
            task.thread.join(timeout=2)
        self.assertEqual(trade.call_count, 2)
        self.assertEqual(task.results['etf_rotation_pap']['state'], 'failed')
        self.assertEqual(task.results['second']['state'], 'success')

    def test_start_after_nine_schedules_immediate_calendar_check(self):
        self.scheduler.stop()
        self.current = DUE.replace(hour=10)
        scheduler = TradingScheduler(self.root)
        try:
            with patch.object(scheduler.timer, 'start'), patch.object(scheduler.timer, 'add_job') as add_job:
                scheduler.start()
            self.assertTrue(any(call.kwargs.get('id') == '_startup_calendar' for call in add_job.call_args_list))
            self.assertFalse(scheduler.tasks)
        finally:
            scheduler.stop()

    def test_multiple_strategies_share_one_calendar_check(self):
        self.scheduler.stop()
        config = {**self.config, 'strategy': 'second'}
        (self.root / 'config/second_strategy.json').write_text(json.dumps(config))
        (self.root / 'config/disabled_strategy.json').write_text(json.dumps({**config, 'strategy': 'disabled', 'enable': False}))
        scheduler = TradingScheduler(self.root)
        scheduler.start()
        scheduler.timer.pause()
        try:
            self.current = DUE.replace(hour=9)
            scheduler.prepare_day()
            self.calendar.assert_called_once()
            self.assertEqual(set(scheduler.jobs), {'etf_rotation', 'second'})
            self.assertFalse(scheduler.tasks)
        finally:
            scheduler.stop()
