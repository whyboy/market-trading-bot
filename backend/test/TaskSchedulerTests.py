"""验证统一注册定时任务并按当天交易日历执行。"""
import json
import tempfile
import threading
import unittest
from datetime import date, datetime, timedelta, timezone
from market_trading_bot.market.MarketProvider import SHANGHAI
from market_trading_bot.task.TradingTask import TradingTask
from market_trading_bot.TaskScheduler import TaskScheduler
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1] / 'dev'
DUE = datetime(2026, 11, 20, 14, 30, tzinfo=SHANGHAI)


class TaskSchedulerTests(unittest.TestCase):
    def setUp(self):
        sender = patch('market_trading_bot.notification.PushClient.PushClient.send', return_value='receipt')
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
        self.clock = patch.object(TaskScheduler, 'now', side_effect=lambda: self.current)
        self.clock.start()
        self.calendar_patch = patch('market_trading_bot.TaskScheduler.MarketProvider.trading_calendar',
                                    return_value=[date(2026, 11, 19), date(2026, 11, 20), date(2026, 11, 24)])
        self.calendar = self.calendar_patch.start()
        self.scheduler = TaskScheduler(self.root)
        with patch.object(self.scheduler, 'prepare_calendar_context'):
            self.scheduler.start()
        self.scheduler.timer.pause()

    def tearDown(self):
        self.scheduler.stop()
        self.clock.stop()
        self.calendar_patch.stop()
        self.temporary.cleanup()

    def prepare(self):
        self.current = DUE.replace(hour=9, minute=0)
        self.scheduler.prepare_calendar_context()

    def test_morning_greeting_uses_configured_time(self):
        trigger = self.scheduler.timer.get_job('_morning_greeting').trigger
        self.assertEqual(trigger.get_next_fire_time(None, self.current),
                         self.current.replace(hour=9, minute=0))

    def test_daily_calendar_trigger_is_eight_fifty(self):
        trigger = self.scheduler.timer.get_job('_calendar').trigger
        morning = DUE.replace(hour=8, minute=50)
        self.assertEqual(trigger.get_next_fire_time(None, self.current), morning)
        self.assertEqual(trigger.get_next_fire_time(morning, morning + timedelta(seconds=1)), morning + timedelta(days=1))
        self.assertEqual(self.scheduler.tasks, {})
        self.assertIsNotNone(self.scheduler.timer.get_job('etf_rotation'))
        self.calendar.assert_not_called()

    def test_trading_day_uses_registered_daily_trade_job(self):
        self.prepare()
        self.calendar.assert_called_once()
        self.assertTrue(self.scheduler.calendar_context['is_trading_day'])
        self.assertEqual(self.scheduler.tasks, {})
        trigger = self.scheduler.timer.get_job('etf_rotation').trigger
        self.assertEqual(trigger.get_next_fire_time(None, self.current), DUE)
        self.assertEqual(trigger.get_next_fire_time(DUE, DUE + timedelta(seconds=1)), DUE + timedelta(days=1))
        self.scheduler.prepare_calendar_context()
        self.calendar.assert_called_once()

    def test_holiday_allows_scheduled_trade_with_prepared_calendar(self):
        self.current = DUE.replace(day=23, hour=9)
        with self.assertLogs('market_trading_bot.TaskScheduler', level='INFO') as logs:
            self.scheduler.prepare_calendar_context()
        self.assertIn('非交易日', logs.output[0])
        self.assertFalse(self.scheduler.calendar_context['is_trading_day'])
        with patch.object(self.scheduler, '_build_trade_task') as build:
            self.scheduler._start_trades('etf_rotation', self.current.date())
            build.assert_called_once()
            build.return_value.start.assert_called_once()
        self.scheduler.tasks.clear()

    def test_calendar_failure_waits_five_seconds_then_registers_jobs(self):
        self.calendar.side_effect = [ValueError('网络失败'), [date(2026, 11, 19), date(2026, 11, 20), date(2026, 11, 24)]]
        with patch.object(self.scheduler.stop_event, 'wait', return_value=False) as wait:
            self.prepare()
        wait.assert_called_once_with(5)
        self.assertIsNone(self.scheduler.calendar_error)
        self.assertIsNotNone(self.scheduler.timer.get_job('etf_rotation'))

    def test_late_calendar_recovery_does_not_run_past_strategy_time(self):
        self.calendar.side_effect = [ValueError('网络失败'), [date(2026, 11, 19), date(2026, 11, 20), date(2026, 11, 24)]]

        def wait(seconds):
            self.current = DUE + timedelta(seconds=1)
            return False

        with patch.object(self.scheduler.stop_event, 'wait', side_effect=wait):
            self.prepare()
        self.assertTrue(self.scheduler.calendar_context['is_trading_day'])
        self.assertIsNotNone(self.scheduler.timer.get_job('etf_rotation'))
        self.assertEqual(self.scheduler.tasks, {})

    def test_calendar_retry_stops_at_close(self):
        self.calendar.side_effect = ValueError('网络失败')

        def wait(seconds):
            self.current = DUE.replace(hour=15)
            return False

        with patch.object(self.scheduler.stop_event, 'wait', side_effect=wait):
            self.prepare()
        self.calendar.assert_called_once()
        self.assertIsNotNone(self.scheduler.timer.get_job('etf_rotation'))

    def test_calendar_retry_can_be_stopped(self):
        self.calendar.side_effect = ValueError('网络失败')
        with patch.object(self.scheduler.stop_event, 'wait', return_value=True):
            self.prepare()
        self.calendar.assert_called_once()
        self.assertIsNotNone(self.scheduler.timer.get_job('etf_rotation'))

    def test_previous_day_callback_does_not_create_task(self):
        self.prepare()
        self.current = DUE + timedelta(days=1)
        self.scheduler._start_trades('etf_rotation', DUE.date())
        self.assertEqual(self.scheduler.tasks, {})

    def test_trade_uses_loaded_config_until_restart(self):
        self.prepare()
        job = self.scheduler.timer.get_job('etf_rotation')
        self.path.unlink()
        (self.root / 'market_trading_bot/config/global.json').unlink()
        with patch.object(TradingTask, 'trade', return_value={}):
            job.func(*job.args)
            task = self.scheduler.tasks[('etf_rotation', 'etf_rotation_pap')]
            task.thread.join(timeout=2)
        self.assertTrue(task.result['success'])
        self.assertFalse((self.root / 'data/runtime/etf_rotation/etf_rotation_pap/runtime.sqlite3').exists())

    def test_task_only_executes_strategy_and_exits(self):
        task = self.scheduler._build_trade_task('etf_rotation', self.config['instances'][0], DUE)
        thread_ids = []
        with patch('market_trading_bot.task.TradingTask.TradingTask.trade', side_effect=lambda *args: thread_ids.append(threading.get_ident())):
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

        with patch('market_trading_bot.task.TradingTask.TradingTask.trade', side_effect=trade):
            self.scheduler._start_trades('etf_rotation', self.current.date())
            try:
                self.assertTrue(entered.wait(2))
                first = self.scheduler.tasks[('etf_rotation', 'etf_rotation_pap')]
                self.scheduler._start_trades('etf_rotation', self.current.date())
                self.assertIs(self.scheduler.tasks[('etf_rotation', 'etf_rotation_pap')], first)
            finally:
                release.set()
                first.thread.join(timeout=2)
            self.scheduler._start_trades('etf_rotation', self.current.date())
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

        with patch.object(TradingTask, 'trade', autospec=True, side_effect=trade):
            self.scheduler._start_trades('etf_rotation', self.current.date())
            for task in self.scheduler.tasks.values():
                task.thread.join(timeout=2)
        first = self.scheduler.tasks[('etf_rotation', 'etf_rotation_pap')]
        second = self.scheduler.tasks[('etf_rotation', 'second')]
        self.assertEqual(first.result['state'], 'failed')
        self.assertEqual(second.result['state'], 'success')
        self.assertIsNot(first.strategy, second.strategy)
        self.assertIsNot(first.executor, second.executor)
        self.assertIsNot(first.status, second.status)

    def test_start_after_nine_schedules_immediate_calendar_check(self):
        self.scheduler.stop()
        self.current = DUE.replace(hour=10)
        scheduler = TaskScheduler(self.root)
        try:
            with patch.object(scheduler.timer, 'start'), patch.object(scheduler.timer, 'add_job') as add_job:
                scheduler.start()
            self.calendar.assert_called_once()
            self.assertTrue(scheduler.calendar_context['is_trading_day'])
            self.assertFalse(scheduler.tasks)
        finally:
            scheduler.stop()

    def test_start_before_nine_checks_calendar_immediately(self):
        self.scheduler.stop()
        scheduler = TaskScheduler(self.root)
        try:
            scheduler.start()
            scheduler.timer.pause()
            self.calendar.assert_called_once()
            self.assertTrue(scheduler.calendar_context['is_trading_day'])
            self.assertIsNotNone(scheduler.timer.get_job('etf_rotation'))
        finally:
            scheduler.stop()

    def test_startup_calendar_retries_before_scheduling(self):
        self.scheduler.stop()
        self.calendar.side_effect = [ValueError('网络失败'), [date(2026, 11, 19), date(2026, 11, 20), date(2026, 11, 24)]]
        scheduler = TaskScheduler(self.root)
        try:
            with patch.object(scheduler.stop_event, 'wait', return_value=False) as wait:
                scheduler.start()
            scheduler.timer.pause()
            wait.assert_called_once_with(5)
            self.assertTrue(scheduler.calendar_context['is_trading_day'])
            self.assertIsNotNone(scheduler.timer.get_job('etf_rotation'))
        finally:
            scheduler.stop()

    def test_multiple_strategies_share_one_calendar_check(self):
        self.scheduler.stop()
        config = {**self.config, 'strategy': 'second'}
        (self.root / 'market_trading_bot/config/second_strategy.json').write_text(json.dumps(config))
        (self.root / 'market_trading_bot/config/disabled_strategy.json').write_text(json.dumps({**config, 'strategy': 'disabled', 'enable': False}))
        scheduler = TaskScheduler(self.root)
        scheduler.start()
        scheduler.timer.pause()
        try:
            self.current = DUE.replace(hour=9)
            scheduler.prepare_calendar_context()
            self.calendar.assert_called_once()
            self.assertIsNotNone(scheduler.timer.get_job('etf_rotation'))
            self.assertIsNotNone(scheduler.timer.get_job('second'))
            self.assertIsNone(scheduler.timer.get_job('disabled'))
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

        with patch.object(TradingTask, 'trade', autospec=True, side_effect=trade):
            self.scheduler._start_trades('etf_rotation', self.current.date())
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
            self.scheduler._build_trade_task('etf_rotation', {**self.config['instances'][0], 'mode': 'pro'}, DUE)

    def test_valuation_and_summary_have_daily_cron_triggers(self):
        valuation = self.scheduler.timer.get_job('_daily_valuation')
        summary = self.scheduler.timer.get_job('_daily_report_15:10')
        self.assertEqual(valuation.trigger.get_next_fire_time(None, self.current), DUE.replace(hour=15, minute=5))
        self.assertEqual(summary.trigger.get_next_fire_time(None, self.current), DUE.replace(hour=15, minute=10))

    def test_report_times_register_separate_jobs_and_select_strategies(self):
        self.scheduler.stop()
        second = {**self.config, 'strategy': 'second', 'schedule': {'trade_time': '14:30', 'report_time': '19:30'}}
        (self.root / 'market_trading_bot/config/second_strategy.json').write_text(json.dumps(second))
        scheduler = TaskScheduler(self.root)
        try:
            scheduler.start()
            scheduler.timer.pause()
            self.assertIsNotNone(scheduler.timer.get_job('_daily_report_15:10'))
            self.assertIsNotNone(scheduler.timer.get_job('_daily_report_19:30'))
            self.current = DUE.replace(hour=19, minute=30)
            with patch('market_trading_bot.TaskScheduler.DailySummaryTask') as task:
                task.return_value.thread.is_alive.return_value = False
                scheduler._start_summary(report_time='19:30')
                self.assertEqual(list(task.call_args.args[2]), ['second'])
        finally:
            scheduler.stop()

    def test_late_start_schedules_recovery(self):
        self.scheduler.stop()
        self.current = DUE.replace(hour=16)
        scheduler = TaskScheduler(self.root)
        try:
            with patch.object(scheduler.timer, 'start'), patch.object(scheduler.timer, 'add_job') as add_job:
                scheduler.start()
            self.assertTrue(any(call.kwargs.get('id') == '_startup_daily_tasks' for call in add_job.call_args_list))
            self.assertTrue(scheduler.calendar_context['is_trading_day'])
        finally:
            scheduler.stop()

    def test_daily_task_is_started_once_per_day_and_shares_calendar(self):
        self.prepare()
        self.current = DUE.replace(hour=15, minute=10)
        with patch('market_trading_bot.TaskScheduler.DailySummaryTask') as task_class:
            task_class.return_value.thread.is_alive.return_value = False
            self.scheduler._start_summary()
            self.scheduler._start_summary()
            task_class.assert_called_once()
            task_class.return_value.start.assert_called_once()
            self.assertEqual(task_class.call_args.args[4]['previous_trading_day'], date(2026, 11, 19))
            self.calendar.assert_called_once()
            self.current += timedelta(days=1)
            self.scheduler._start_summary()
            self.assertEqual(task_class.call_count, 2)
            self.assertEqual(self.scheduler.calendar_context['day'], self.current.date())

    def test_daily_jobs_prepare_calendar_on_first_run(self):
        self.current = DUE.replace(day=23, hour=15, minute=10)
        with patch('market_trading_bot.TaskScheduler.DailyValuationTask') as valuation, \
             patch('market_trading_bot.TaskScheduler.DailySummaryTask') as summary:
            self.scheduler._start_valuation()
            self.scheduler._start_summary()
        valuation.assert_called_once()
        summary.assert_called_once()
        self.assertEqual(self.scheduler.calendar_context['day'], self.current.date())
        self.calendar.assert_called_once()

    def test_all_jobs_skip_when_calendar_preparation_failed_or_stale(self):
        self.prepare()
        self.current = DUE.replace(day=23, hour=15, minute=10)
        self.calendar.side_effect = ValueError('网络失败')
        with patch.object(self.scheduler.stop_event, 'wait', return_value=True), \
             patch.object(self.scheduler, '_build_trade_task') as trade, \
             patch('market_trading_bot.TaskScheduler.DailyValuationTask') as valuation, \
             patch('market_trading_bot.TaskScheduler.DailySummaryTask') as summary:
            self.scheduler.prepare_calendar_context()
            self.scheduler._start_trades('etf_rotation')
            self.scheduler._start_valuation()
            self.scheduler._start_summary()
        trade.assert_not_called()
        valuation.assert_not_called()
        summary.assert_not_called()
        self.assertEqual(self.scheduler.calendar_error, '网络失败')

    def test_late_start_waits_for_valuation_before_summary(self):
        self.current = DUE.replace(hour=16)
        events = []
        task = Mock()
        task.thread.join.side_effect = lambda: events.append('valuation-finished')
        self.scheduler.valuation_task = task
        with patch.object(self.scheduler, '_start_valuation', side_effect=lambda: events.append('valuation-start')):
            with patch.object(self.scheduler, '_start_summary', side_effect=lambda **kwargs: events.append('summary-start')):
                self.scheduler._recover_daily_tasks()
        self.assertEqual(events, ['valuation-start', 'valuation-finished', 'summary-start'])
        self.scheduler.valuation_task = None
