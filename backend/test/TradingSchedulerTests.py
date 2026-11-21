"""验证早晨统一判断交易日、按时创建一次性策略任务。"""
import json
import tempfile
import threading
import unittest
from datetime import date, datetime, timedelta, timezone
from market_trading_bot.market.AKShareMarket import SHANGHAI
from market_trading_bot.TradingScheduler import TradingScheduler
from market_trading_bot.TradingTask import TradingTask
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1] / 'dev'
DUE = datetime(2026, 11, 20, 14, 30, tzinfo=SHANGHAI)


class TradingSchedulerTests(unittest.TestCase):
    def setUp(self):
        sender = patch('market_trading_bot.notification.PushPlusClient.PushPlusClient.send', return_value='receipt')
        sender.start()
        self.addCleanup(sender.stop)
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / 'market_trading_bot/config').mkdir(parents=True)
        (self.root / 'market_trading_bot/config/global.json').write_text((ROOT / 'market_trading_bot/config/global.json').read_text())
        self.path = self.root / 'market_trading_bot/config/etf_rotation_strategy.json'
        self.config = json.loads((ROOT / 'market_trading_bot/config/etf_rotation_strategy.json').read_text())
        self.path.write_text(json.dumps(self.config))
        self.current = DUE.replace(hour=8, minute=0)
        self.clock = patch.object(TradingScheduler, 'now', side_effect=lambda: self.current)
        self.clock.start()
        self.calendar_patch = patch('market_trading_bot.TradingScheduler.AKShareMarket.trading_calendar',
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
        with patch('market_trading_bot.TradingTask.TradingTask.run_pap') as trade:
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
        task = self.scheduler._build_task('etf_rotation', self.config['instances'][0], DUE)
        thread_ids = []
        with patch('market_trading_bot.TradingTask.TradingTask.run_pap', side_effect=lambda *args: thread_ids.append(threading.get_ident())):
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

        with patch('market_trading_bot.TradingTask.TradingTask.run_pap', side_effect=trade):
            self.scheduler._start_task(self.path)
            try:
                self.assertTrue(entered.wait(2))
                first = self.scheduler.tasks[('etf_rotation', 'etf_rotation_pap')]
                self.scheduler._start_task(self.path)
                self.assertIs(self.scheduler.tasks[('etf_rotation', 'etf_rotation_pap')], first)
            finally:
                release.set()
                first.thread.join(timeout=2)
            self.scheduler._start_task(self.path)
            second = self.scheduler.tasks[('etf_rotation', 'etf_rotation_pap')]
            second.thread.join(timeout=2)
            self.assertIsNot(first, second)

    def test_accounts_have_independent_tasks_and_failures(self):
        self.config['instances'].append({**self.config['instances'][0], 'id': 'second'})
        self.scheduler.configs['etf_rotation'] = self.config
        self.prepare()

        def trade(task):
            if task.instance_id == 'etf_rotation_pap':
                raise ValueError('策略失败')
            return {}

        with patch.object(TradingTask, 'run_pap', autospec=True, side_effect=trade):
            self.scheduler._start_task(self.path)
            for task in self.scheduler.tasks.values():
                task.thread.join(timeout=2)
        first = self.scheduler.tasks[('etf_rotation', 'etf_rotation_pap')]
        second = self.scheduler.tasks[('etf_rotation', 'second')]
        self.assertEqual(first.result['state'], 'failed')
        self.assertEqual(second.result['state'], 'success')
        self.assertIsNot(first.strategy, second.strategy)
        self.assertIsNot(first.executor, second.executor)
        self.assertNotEqual(first.status_repository.database, second.status_repository.database)

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
        (self.root / 'market_trading_bot/config/second_strategy.json').write_text(json.dumps(config))
        (self.root / 'market_trading_bot/config/disabled_strategy.json').write_text(json.dumps({**config, 'strategy': 'disabled', 'enable': False}))
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

    def test_same_strategy_accounts_execute_independently_and_report_separate_status(self):
        self.config['instances'].append({**self.config['instances'][0], 'id': 'second'})
        self.scheduler.configs['etf_rotation'] = self.config
        self.prepare()
        entered = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def trade(task):
            if task.instance_id == 'etf_rotation_pap':
                entered.set()
                release.wait(3)
            else:
                finished.set()

        with patch.object(TradingTask, 'run_pap', autospec=True, side_effect=trade):
            self.scheduler._start_task(self.path)
            try:
                self.assertTrue(entered.wait(2))
                self.assertTrue(finished.wait(2))
                first = self.scheduler.tasks[('etf_rotation', 'etf_rotation_pap')]
                second = self.scheduler.tasks[('etf_rotation', 'second')]
                second.thread.join(timeout=2)
                first.status['current_execution'] = {**first.status['current_execution'],
                    'started_at': (datetime.now(timezone.utc) - timedelta(seconds=301)).isoformat()}
                first_status = self.scheduler.get_status('etf_rotation', 'etf_rotation_pap')
                second_status = self.scheduler.get_status('etf_rotation', 'second')
                self.assertEqual(first_status['health'], 'trade_timeout')
                self.assertFalse(first_status['online'])
                self.assertTrue(second_status['online'])
                self.assertEqual(second_status['last_execution']['instance_id'], 'second')
                self.assertEqual(self.scheduler.get_status('etf_rotation')['health'], 'trade_timeout')
                self.assertNotEqual(first.thread.ident, second.thread.ident)
            finally:
                release.set()

    def test_factory_rejects_unsupported_execution_mode(self):
        with self.assertRaises(ValueError):
            self.scheduler._build_task('etf_rotation', {**self.config['instances'][0], 'mode': 'pro'}, DUE)

    def test_daily_report_trigger_is_fifteen_oh_five_even_on_weekends(self):
        trigger = self.scheduler.timer.get_job('_daily_report').trigger
        due = DUE.replace(hour=15, minute=5)
        self.assertEqual(trigger.get_next_fire_time(None, self.current), due)
        self.assertEqual(trigger.get_next_fire_time(due, due + timedelta(seconds=1)), due + timedelta(days=1))

    def test_daily_report_runs_once_per_day_and_releases_guard(self):
        self.current = DUE.replace(hour=15, minute=5)
        with patch.object(self.scheduler.daily_reports, 'send_reports', return_value=True) as send:
            self.scheduler.send_daily_reports()
            self.scheduler.send_daily_reports()
            send.assert_called_once_with(self.current.date())
            self.current += timedelta(days=1)
            self.scheduler.send_daily_reports()
        self.assertEqual(send.call_count, 2)

    def test_stopped_daily_run_is_not_marked_complete(self):
        self.current = DUE.replace(hour=15, minute=5)
        with patch.object(self.scheduler.daily_reports, 'send_reports', return_value=False):
            self.scheduler.send_daily_reports()
        self.assertIsNone(self.scheduler.reported_day)

    def test_start_after_fifteen_oh_five_schedules_same_day_report(self):
        self.scheduler.stop()
        self.current = DUE.replace(hour=16)
        scheduler = TradingScheduler(self.root)
        try:
            with patch.object(scheduler.timer, 'start'), patch.object(scheduler.timer, 'add_job') as add_job:
                scheduler.start()
            self.assertTrue(any(call.kwargs.get('id') == '_startup_daily_report' for call in add_job.call_args_list))
        finally:
            scheduler.stop()
