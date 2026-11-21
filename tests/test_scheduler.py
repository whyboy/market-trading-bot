"""验证每日注册、日历重试及交易线程中的同步执行。"""
import json
import tempfile
import threading
import unittest
from datetime import date, datetime, timedelta
from market_trading_bot.AKShareMarket import SHANGHAI
from market_trading_bot.runtime.TradingScheduler import TradingScheduler
from market_trading_bot.runtime.TradingWorker import TradingWorker
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
        self.config = json.loads((ROOT / 'config/etf_rotation_strategy.json').read_text())
        self.config_path = self.root / 'config/etf_rotation_strategy.json'
        self.save_config()
        self.current = DUE - timedelta(seconds=10)
        self.calendar_patch = patch('market_trading_bot.runtime.TradingScheduler.AKShareMarket.trading_calendar',
                                    return_value=[date(2026, 11, 20), date(2026, 11, 24)])
        self.calendar = self.calendar_patch.start()
        self.scheduler = TradingScheduler(self.root)
        self.scheduler.now = lambda: self.current
        self.stop = threading.Event()
        self.execute = Mock(return_value={'success': True, 'finished_at': DUE.isoformat()})

    def tearDown(self):
        self.calendar_patch.stop()
        self.temporary.cleanup()

    def save_config(self):
        self.config_path.write_text(json.dumps(self.config))

    def test_registers_without_loading_calendar_or_trading(self):
        self.scheduler.register_jobs(self.execute, self.stop)
        self.scheduler.run_pending()
        self.calendar.assert_not_called()
        self.execute.assert_not_called()
        self.assertEqual(self.scheduler.snapshot()['scheduled_tasks'][0]['next_execution_at'], DUE.isoformat())

    def test_due_executes_once_and_registers_tomorrow(self):
        self.scheduler.register_jobs(self.execute, self.stop)
        self.current = DUE
        self.scheduler.run_pending()
        self.scheduler.run_pending()
        self.execute.assert_called_once()
        task = self.scheduler.snapshot()['scheduled_tasks'][0]
        self.assertEqual(task['last_execution']['state'], 'success')
        self.assertEqual(task['next_execution_at'], (DUE + timedelta(days=1)).isoformat())

    def test_starting_after_time_waits_until_tomorrow(self):
        self.current = DUE + timedelta(seconds=1)
        self.scheduler.register_jobs(self.execute, self.stop)
        self.scheduler.run_pending()
        self.execute.assert_not_called()
        self.assertEqual(self.scheduler.snapshot()['scheduled_tasks'][0]['next_execution_at'],
                         (DUE + timedelta(days=1)).isoformat())

    def test_calendar_failure_retries_every_five_seconds(self):
        self.current = DUE
        self.calendar.side_effect = [ValueError('网络失败'), ValueError('仍不可用'),
                                     [date(2026, 11, 20), date(2026, 11, 24)]]
        stop = Mock(spec=threading.Event)
        stop.is_set.return_value = False
        stop.wait.return_value = False
        self.assertTrue(self.scheduler.load_calendar_with_retry(DUE, stop))
        self.assertEqual(self.calendar.call_count, 3)
        self.assertEqual(stop.wait.call_args_list, [((5,),), ((5,),)])
        self.assertIsNone(self.scheduler.error)

    def test_non_trading_day_finishes_without_retry(self):
        self.current = DUE.replace(day=23)
        stop = Mock(spec=threading.Event)
        stop.is_set.return_value = False
        self.assertFalse(self.scheduler.load_calendar_with_retry(self.current, stop))
        self.calendar.assert_called_once()
        stop.wait.assert_not_called()

    def test_calendar_retry_can_be_stopped(self):
        self.current = DUE
        self.calendar.side_effect = ValueError('网络失败')
        stop = Mock(spec=threading.Event)
        stop.is_set.return_value = False
        stop.wait.return_value = True
        self.assertFalse(self.scheduler.load_calendar_with_retry(DUE, stop))
        self.calendar.assert_called_once()
        stop.wait.assert_called_once_with(5)

    def test_calendar_retry_stops_at_session_end(self):
        self.current = DUE
        self.calendar.side_effect = ValueError('网络失败')
        stop = Mock(spec=threading.Event)
        stop.is_set.return_value = False

        def wait(seconds):
            self.current = DUE.replace(hour=15)
            return False

        stop.wait.side_effect = wait
        self.assertFalse(self.scheduler.load_calendar_with_retry(DUE, stop))
        self.calendar.assert_called_once()

    def test_calendar_response_after_session_end_does_not_allow_trade(self):
        self.current = DUE

        def calendar():
            self.current = DUE.replace(hour=15)
            return [date(2026, 11, 20), date(2026, 11, 24)]

        self.calendar.side_effect = calendar
        self.assertFalse(self.scheduler.load_calendar_with_retry(DUE, self.stop))

    def test_failed_account_does_not_prevent_other_account(self):
        self.config['instances'].append({**self.config['instances'][0], 'id': 'second'})
        self.save_config()
        scheduler = TradingScheduler(self.root)
        scheduler.now = lambda: self.current
        scheduler.register_jobs(self.execute, self.stop)
        self.execute.side_effect = [ValueError('策略失败'), {'success': True}]
        self.current = DUE
        scheduler.run_pending()
        self.assertEqual(self.execute.call_count, 2)
        self.assertEqual([task['last_execution']['state'] for task in scheduler.snapshot()['scheduled_tasks']],
                         ['failed', 'success'])

    def test_disabled_accounts_are_not_registered(self):
        self.config['instances'][0]['enabled'] = False
        self.save_config()
        scheduler = TradingScheduler(self.root)
        scheduler.register_jobs(self.execute, self.stop)
        self.assertFalse(scheduler.scheduler.queue)

    def test_worker_loads_calendar_and_trades_in_same_thread(self):
        entered = threading.Event()
        thread_ids = []
        register_jobs = TradingScheduler.register_jobs

        def register(scheduler, execute, stop):
            register_jobs(scheduler, execute, stop)
            self.current = DUE

        def execute(*args):
            thread_ids.append(threading.get_ident())
            entered.set()

        with patch.object(TradingScheduler, 'now', side_effect=lambda: self.current), \
                patch.object(TradingScheduler, 'register_jobs', register), \
                patch('market_trading_bot.runner.run_pap', side_effect=execute):
            worker = TradingWorker(self.root)
            worker.start()
            try:
                self.assertTrue(entered.wait(2))
                self.assertEqual(thread_ids, [worker.thread.ident])
                self.calendar.assert_called_once()
            finally:
                worker.stop()
            self.assertTrue(worker.status['last_execution']['success'])
            self.assertFalse((self.root / 'data/etf_rotation_pap').exists())

    def test_invalid_schedule_is_rejected(self):
        for value in ('08:30', '12:00', '15:00', '1430'):
            self.config['schedule']['trade_time'] = value
            self.save_config()
            with self.assertRaises(ValueError):
                TradingScheduler(self.root)

    def test_worker_skips_holiday_without_recording_execution(self):
        self.current = DUE.replace(day=23)
        worker = TradingWorker(self.root)
        worker.scheduler.now = lambda: self.current
        with patch('market_trading_bot.runner.run_pap') as execute:
            result = worker._execute_trade(self.config_path, 'etf_rotation_pap', self.current)
        execute.assert_not_called()
        self.assertIsNone(result)
        self.assertIsNone(worker.status['last_execution'])
        self.assertEqual(worker.status['state'], 'idle')
        self.assertFalse((self.root / 'data/etf_rotation_pap').exists())
