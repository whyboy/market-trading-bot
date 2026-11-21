"""日报只读已保存估值，不调用行情接口、不更新每日结果。"""
import tempfile
import threading
import unittest
from datetime import date, datetime
from market_trading_bot.market.AKShareMarket import SHANGHAI
from market_trading_bot.service.AccountService import AccountService
from market_trading_bot.service.DailyResultsService import DailyResultsService
from market_trading_bot.task.DailySummaryTask import DailySummaryTask
from pathlib import Path
from unittest.mock import Mock


class DailySummaryTaskTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.day = date(2026, 11, 20)
        self.current = datetime(2026, 11, 20, 15, 10, tzinfo=SHANGHAI)
        self.settings = {'storage': {'data_dir': 'data'}}
        self.configs = {'etf_rotation': {'instances': [{'id': 'TradeExecutor', 'enabled': True, 'mode': 'pap'}]}}
        self.context = {'day': self.day, 'is_trading_day': True, 'previous_trading_day': date(2026, 11, 19)}
        self.notifier = Mock()
        self.stop = threading.Event()
        self.task = DailySummaryTask(self.root, self.settings, self.configs, self.notifier,
                                     self.context, self.stop, lambda: self.current)

    def store(self, instance='TradeExecutor', strategy='etf_rotation'):
        repository = AccountService(self.root / 'data', instance, strategy)
        with repository.transaction('2026-11-20', 100000) as transaction:
            transaction.save_execution({'date': '2026-11-20', 'cash': '100000', 'equity': '100000',
                                               'positions': {}, 'fills': []})
        return repository

    def summary(self, index=0):
        return self.notifier.send_daily_report.call_args.args[1][index]['summary']

    def test_pending_running_and_failed_never_use_old_valuation(self):
        self.store()
        DailyResultsService(self.root / 'data', 'TradeExecutor').save_valuation({'date': '2026-11-20', 'equity': '999999'})
        for status in ('pending', 'running', 'failed'):
            with self.subTest(status=status):
                DailyResultsService(self.root / 'data', 'TradeExecutor').set_valuation_status('2026-11-20', status, '估值失败')
                self.task.run()
                self.assertIn('notice', self.summary())
                self.assertNotIn('equity', self.summary())

    def test_holiday_reads_latest_success_and_preserves_date(self):
        self.store()
        DailyResultsService(self.root / 'data', 'TradeExecutor').save_valuation({'date': '2026-11-20', 'as_of': '2026-11-20T15:00:00+08:00', 'equity': '100500'})
        self.context.update(day=date(2026, 11, 21), is_trading_day=False)
        self.current = self.current.replace(day=21)
        self.task.run()
        self.assertFalse(self.summary()['is_trading_day'])
        self.assertEqual(self.summary()['as_of'], '2026-11-20T15:00:00+08:00')

    def test_all_strategies_and_uninitialized_accounts_are_one_message(self):
        self.store()
        self.configs['other'] = {'instances': [{'id': 'second', 'enabled': True, 'mode': 'pap'}]}
        self.task.start()
        self.task.thread.join(timeout=3)
        self.assertTrue(self.task.completed)
        self.notifier.send_daily_report.assert_called_once()
        entries = self.notifier.send_daily_report.call_args.args[1]
        self.assertEqual({entry['strategy_id'] for entry in entries}, {'etf_rotation', 'other'})
        self.assertIn('尚未开户', self.summary(1)['notice'])
        self.assertFalse((self.root / 'data/second').exists())
