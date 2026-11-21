"""离线验证交易日调度、触发窗口和重启后的防重复行为。"""
import json
import tempfile
import threading
import unittest
from datetime import date, datetime, timedelta
from market_trading_bot.AKShareMarket import SHANGHAI
from market_trading_bot.runtime.TradeScheduler import TradeScheduler
from market_trading_bot.runtime.TradingWorker import TradingWorker
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 20, 14, 30, tzinfo=SHANGHAI)


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / 'config').mkdir()
        (self.root / 'config/global.json').write_text((ROOT / 'config/global.json').read_text())
        self.config = json.loads((ROOT / 'config/etf_rotation_strategy.json').read_text())
        self.config_path = self.root / 'config/etf_rotation_strategy.json'
        self.save_config()
        self.calendar_patch = patch('market_trading_bot.runtime.TradeScheduler.AKShareMarket.trading_calendar',
                                    return_value=[date(2026, 11, 20), date(2026, 11, 24)])
        self.calendar = self.calendar_patch.start()
        self.scheduler = TradeScheduler(self.root)
        self.current = NOW
        self.scheduler.now = lambda: self.current
        self.stop = threading.Event()
        self.execute = Mock(return_value={'success': True, 'finished_at': NOW.isoformat()})

    def tearDown(self):
        self.calendar_patch.stop()
        self.temporary.cleanup()

    def save_config(self):
        self.config_path.write_text(json.dumps(self.config))

    def tick(self):
        self.scheduler.tick(self.execute, self.stop)

    def test_not_due_and_next_execution(self):
        self.current = NOW - timedelta(seconds=1)
        self.tick()
        self.execute.assert_not_called()
        task = self.scheduler.snapshot()['scheduled_tasks'][0]
        self.assertEqual(task['next_execution_at'], NOW.isoformat())

    def test_due_once_even_after_restart(self):
        self.tick()
        self.tick()
        restarted = TradeScheduler(self.root)
        restarted.now = lambda: self.current
        restarted.tick(self.execute, self.stop)
        self.execute.assert_called_once_with(self.config_path, 'etf_rotation_pap')
        self.assertEqual(restarted.snapshot()['scheduled_tasks'][0]['last_execution']['state'], 'success')
        self.assertEqual(restarted.snapshot()['scheduled_tasks'][0]['next_execution_at'],
                         datetime(2026, 11, 24, 14, 30, tzinfo=SHANGHAI).isoformat())

    def test_missed_window_is_recorded_without_trade(self):
        self.current = NOW + timedelta(seconds=60)
        self.tick()
        self.execute.assert_not_called()
        self.assertEqual(self.scheduler.snapshot()['scheduled_tasks'][0]['last_execution']['state'], 'missed')
        self.assertIn('错过调仓窗口', next((self.root / 'logs/etf_rotation').glob('*.log')).read_text())

    def test_window_allows_late_start_within_limit(self):
        self.current = NOW + timedelta(seconds=59)
        self.tick()
        self.execute.assert_called_once()

    def test_weekend_and_calendar_holiday_do_not_trade(self):
        for day in (21, 23):
            self.current = NOW.replace(day=day)
            self.tick()
        self.execute.assert_not_called()
        self.assertFalse(self.scheduler.store.schedule_results())

    def test_calendar_failure_pauses_then_retries_without_late_trade(self):
        self.calendar.side_effect = ValueError('日历不可用')
        self.tick()
        self.tick()
        self.assertEqual(self.calendar.call_count, 1)
        self.assertEqual(self.scheduler.snapshot()['scheduler_error'], '日历不可用')
        self.execute.assert_not_called()
        self.calendar.side_effect = None
        self.current += timedelta(seconds=61)
        self.tick()
        self.assertIsNone(self.scheduler.error)
        self.assertEqual(self.scheduler.snapshot()['scheduled_tasks'][0]['last_execution']['state'], 'missed')

    def test_outdated_calendar_never_falls_back_to_weekdays(self):
        self.calendar.return_value = [date(2025, 1, 1)]
        self.tick()
        self.execute.assert_not_called()
        self.assertIn('未覆盖', self.scheduler.error)

    def test_failed_account_does_not_prevent_other_account(self):
        self.config['instances'].append({**self.config['instances'][0], 'id': 'second'})
        self.save_config()
        self.scheduler = TradeScheduler(self.root)
        self.scheduler.now = lambda: self.current
        self.execute.side_effect = [ValueError('行情失败'), {'success': True}]
        self.tick()
        self.tick()
        self.assertEqual(self.execute.call_count, 2)
        results = self.scheduler.store.schedule_results()
        self.assertEqual(results['etf_rotation_pap']['state'], 'failed')
        self.assertEqual(results['second']['state'], 'success')

    def test_each_account_rechecks_window_after_previous_trade(self):
        self.config['instances'].append({**self.config['instances'][0], 'id': 'second'})
        self.save_config()
        self.scheduler = TradeScheduler(self.root)
        self.scheduler.now = lambda: self.current

        def execute(*args):
            self.current += timedelta(seconds=60)
            return {'success': True}

        self.execute.side_effect = execute
        self.tick()
        self.assertEqual(self.execute.call_count, 1)
        self.assertEqual(self.scheduler.store.schedule_results()['second']['state'], 'missed')

    def test_interrupted_claim_is_not_retried(self):
        self.scheduler.store.claim_schedule('2026-11-20', 'etf_rotation_pap', {
            'date': '2026-11-20', 'state': 'running', 'scheduled_at': NOW.isoformat()})
        self.tick()
        self.execute.assert_not_called()
        self.assertEqual(self.scheduler.store.schedule_results()['etf_rotation_pap']['state'], 'interrupted')

    def test_disabled_accounts_are_not_scheduled(self):
        self.config['instances'][0]['enabled'] = False
        self.save_config()
        scheduler = TradeScheduler(self.root)
        scheduler.tick(self.execute, self.stop)
        self.calendar.assert_not_called()
        self.assertFalse(scheduler.snapshot()['scheduler_enabled'])

    def test_worker_executes_schedule_on_its_own_thread(self):
        entered = threading.Event()
        thread_ids = []

        def execute(*args):
            thread_ids.append(threading.get_ident())
            entered.set()

        with patch.object(TradeScheduler, 'now', return_value=NOW), \
                patch('market_trading_bot.runner.run_pap', side_effect=execute):
            worker = TradingWorker(self.root)
            worker.start()
            try:
                self.assertTrue(entered.wait(2))
                self.assertEqual(thread_ids, [worker.thread.ident])
            finally:
                worker.stop()
            result = worker.store.schedule_results()['etf_rotation_pap']
            self.assertEqual(result['state'], 'success')

    def test_invalid_schedule_fails_before_starting_service(self):
        for invalid in ('08:30', '12:00', '15:00', '14:30:05', '1430'):
            self.config['schedule']['trade_time'] = invalid
            self.save_config()
            with self.assertRaises(ValueError):
                TradeScheduler(self.root)

    def test_next_trading_day_can_execute_again(self):
        self.tick()
        self.current = NOW.replace(day=24)
        self.tick()
        self.assertEqual(self.execute.call_count, 2)
        self.assertEqual(self.scheduler.store.schedule_results()['etf_rotation_pap']['date'], '2026-11-24')

    def test_shutdown_does_not_start_another_account(self):
        self.config['instances'].append({**self.config['instances'][0], 'id': 'second'})
        self.save_config()
        self.scheduler = TradeScheduler(self.root)
        self.scheduler.now = lambda: self.current

        def execute(*args):
            self.stop.set()
            return {'success': True}

        self.execute.side_effect = execute
        self.tick()
        self.assertEqual(self.execute.call_count, 1)
        self.assertNotIn('second', self.scheduler.store.schedule_results())
