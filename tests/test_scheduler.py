"""验证每日调度与一次性策略任务。"""
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
        self.task = TradingTask(self.root, self.path)
        self.current = DUE
        self.task.now = lambda: self.current
        self.calendar_patch = patch('market_trading_bot.runtime.TradingTask.AKShareMarket.trading_calendar',
                                    return_value=[date(2026, 11, 20), date(2026, 11, 24)])
        self.calendar = self.calendar_patch.start()

    def tearDown(self):
        self.calendar_patch.stop()
        self.temporary.cleanup()

    def test_daily_trigger_repeats_without_task_registration(self):
        scheduler = TradingScheduler(self.root)
        scheduler.start()
        try:
            self.assertEqual(scheduler.tasks, {})
            trigger = scheduler.jobs['etf_rotation'].trigger
            self.assertEqual(trigger.get_next_fire_time(None, DUE - timedelta(seconds=1)), DUE)
            self.assertEqual(trigger.get_next_fire_time(DUE, DUE + timedelta(seconds=1)), DUE + timedelta(days=1))
            self.calendar.assert_not_called()
        finally:
            scheduler.stop()

    def test_task_runs_once_and_thread_exits(self):
        thread_ids = []
        with patch('market_trading_bot.runner.run_pap', side_effect=lambda *args: thread_ids.append(threading.get_ident())):
            self.task.start()
            self.task.thread.join(timeout=2)
        self.assertFalse(self.task.thread.is_alive())
        self.assertEqual(thread_ids, [self.task.thread.ident])
        self.assertEqual(self.task.status['state'], 'completed')
        self.assertTrue(self.task.status['last_execution']['success'])
        self.calendar.assert_called_once()

    def test_next_launch_creates_new_task_and_skips_active_task(self):
        scheduler = TradingScheduler(self.root)
        scheduler.start()
        entered = threading.Event()
        release = threading.Event()

        def trade(*args):
            entered.set()
            release.wait(3)

        try:
            with patch.object(TradingTask, 'load_calendar_with_retry', return_value=True), \
                    patch('market_trading_bot.runner.run_pap', side_effect=trade):
                scheduler._start_task(self.path)
                try:
                    self.assertTrue(entered.wait(2))
                    first = scheduler.tasks['etf_rotation']
                    scheduler._start_task(self.path)
                    self.assertIs(scheduler.tasks['etf_rotation'], first)
                finally:
                    release.set()
                    first.thread.join(timeout=2)
                scheduler._start_task(self.path)
                second = scheduler.tasks['etf_rotation']
                second.thread.join(timeout=2)
                self.assertIsNot(first, second)
                self.assertNotEqual(first.status['run_id'], second.status['run_id'])
        finally:
            scheduler.stop()

    def test_calendar_retries_five_seconds_then_succeeds(self):
        self.calendar.side_effect = [ValueError('网络失败'), [date(2026, 11, 20), date(2026, 11, 24)]]
        stop = Mock(spec=threading.Event)
        stop.is_set.return_value = False
        stop.wait.return_value = False
        self.task.stop_event = stop
        self.assertTrue(self.task.load_calendar_with_retry(Mock()))
        stop.wait.assert_called_once_with(5)

    def test_holiday_finishes_without_trade(self):
        self.current = DUE.replace(day=23)
        with patch('market_trading_bot.runner.run_pap') as trade:
            self.task.start()
            self.task.thread.join(timeout=2)
        trade.assert_not_called()
        self.assertIsNone(self.task.status['last_execution'])
        self.assertFalse(self.task.thread.is_alive())

    def test_retry_stops_at_close(self):
        self.calendar.side_effect = ValueError('网络失败')
        stop = Mock(spec=threading.Event)
        stop.is_set.return_value = False

        def wait(seconds):
            self.current = DUE.replace(hour=15)
            return False

        stop.wait.side_effect = wait
        self.task.stop_event = stop
        self.assertFalse(self.task.load_calendar_with_retry(Mock()))
        self.calendar.assert_called_once()

    def test_shutdown_interrupts_calendar_retry(self):
        self.calendar.side_effect = ValueError('网络失败')
        self.task.start()
        self.task.stop()
        self.assertFalse(self.task.thread.is_alive())

    def test_failed_account_does_not_prevent_next_account(self):
        self.config['instances'].append({**self.config['instances'][0], 'id': 'second'})
        self.path.write_text(json.dumps(self.config))
        task = TradingTask(self.root, self.path)
        task.now = lambda: DUE
        with patch('market_trading_bot.runner.run_pap', side_effect=[ValueError('策略失败'), {}]) as trade:
            task.start()
            task.thread.join(timeout=2)
        self.assertEqual(trade.call_count, 2)
        self.assertEqual(task.results['etf_rotation_pap']['state'], 'failed')
        self.assertEqual(task.results['second']['state'], 'success')
        self.calendar.assert_called_once()
