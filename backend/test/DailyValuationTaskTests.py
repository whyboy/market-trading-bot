"""估值写回同一账户数据库，交易与收盘数据互不覆盖。"""
import tempfile
import threading
import unittest
from datetime import date, datetime
from decimal import Decimal
from market_trading_bot.market.MarketProvider import SHANGHAI
from market_trading_bot.notification.PushPlusNotifier import PushPlusNotifier
from market_trading_bot.service.AccountService import AccountService
from market_trading_bot.service.DailyResultsService import DailyResultsService
from market_trading_bot.task.DailySummaryTask import DailySummaryTask
from market_trading_bot.task.DailyValuationTask import DailyValuationTask
from pathlib import Path
from unittest.mock import Mock, patch


class DailyValuationTaskTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.day = date(2026, 11, 20)
        self.current = datetime(2026, 11, 20, 15, 5, tzinfo=SHANGHAI)
        self.settings = {'storage': {'data_dir': 'data'}, 'market': {}}
        self.configs = {'etf_rotation': {'instances': [{'id': 'TradeExecutor', 'enabled': True, 'mode': 'pap'}]}}
        self.context = {'day': self.day, 'is_trading_day': True, 'previous_trading_day': date(2026, 11, 19)}
        self.stop = threading.Event()
        self.running = Mock(return_value=False)
        self.task = DailyValuationTask(self.root, self.settings, self.configs, self.context,
                                       self.stop, self.running, lambda: self.current)
        self.market = Mock()
        self.market.closing_prices.side_effect = lambda symbols, day: {
            symbol: Decimal('105' if day == self.day else '100') for symbol in symbols}

    def store(self, instance='TradeExecutor'):
        return AccountService(self.root / 'data', instance, 'etf_rotation')

    def save_account(self, day, cash='79995', quantity=200):
        repository = self.store()
        with repository.transaction(day.isoformat(), 100000) as transaction:
            transaction.save_execution({'date': day.isoformat(), 'cash': cash, 'equity': '100000',
                'positions': {'510300.XSHG': {'quantity': quantity}} if quantity else {},
                'fills': [{'side': 'buy', 'amount': '20000', 'commission': '5'}],
                'signal': {'as_of': f'{day}T14:30:00+08:00'}})
        return repository

    def run_task(self):
        with patch('market_trading_bot.task.DailyValuationTask.MarketProvider', return_value=self.market):
            return self.task.run()

    def result(self, day='2026-11-20'):
        return DailyResultsService.get_daily_result(self.root / 'data', 'TradeExecutor', day)

    def test_first_day_uses_actual_initial_cash_and_preserves_execution(self):
        repository = self.save_account(self.day)
        execution = repository.get_execution(self.day.isoformat())
        self.assertTrue(self.run_task())
        result = self.result()
        self.assertEqual(result['valuation_status'], 'success')
        self.assertEqual(result['valuation']['equity'], '100995.00')
        self.assertEqual(result['valuation']['daily_pnl'], '995.00')
        self.assertEqual(Decimal(result['valuation']['daily_return_pct']), Decimal('0.995'))
        self.assertEqual(result['valuation']['comparison_date'], '开户资金')
        self.assertEqual(result['execution'], execution)
        self.market.trading_calendar.assert_not_called()

    def test_restart_preserves_successful_valuation_when_market_or_calendar_is_unavailable(self):
        self.save_account(self.day)
        self.run_task()
        saved = self.result()
        self.market.closing_prices.reset_mock()
        self.market.closing_prices.side_effect = TimeoutError('temporary outage')
        for context in (self.context, {'day': self.day, 'notice': '交易日历获取失败'}):
            self.task = DailyValuationTask(self.root, self.settings, self.configs, context,
                                           self.stop, self.running, lambda: self.current)
            self.assertTrue(self.run_task())
            self.assertEqual(self.result(), saved)
        self.market.closing_prices.assert_not_called()

    def test_execution_invalidating_valuation_is_recalculated(self):
        self.save_account(date(2026, 11, 19), cash='90000', quantity=100)
        self.run_task()
        self.assertEqual(self.result()['valuation']['equity'], '100500.00')
        self.save_account(self.day)
        self.assertEqual(self.result()['valuation_status'], 'pending')
        self.run_task()
        self.assertEqual(self.result()['valuation_status'], 'success')
        self.assertEqual(self.result()['valuation']['equity'], '100995.00')

    def test_prior_close_is_rebuilt_using_historical_not_current_holdings(self):
        self.save_account(date(2026, 11, 19), cash='90000', quantity=100)
        self.save_account(self.day)
        self.run_task()
        summary = self.result()['valuation']
        self.assertEqual(summary['daily_pnl'], '995.00')
        self.assertEqual(summary['comparison_date'], '2026-11-19')
        previous = self.result('2026-11-19')['valuation']
        self.assertEqual(previous['equity'], '100000.00')
        self.assertEqual(previous['positions']['510300.XSHG']['quantity'], 100)

    def test_no_trade_still_creates_daily_valuation_without_fabricating_execution(self):
        self.save_account(date(2026, 11, 19), cash='90000', quantity=100)
        self.run_task()
        result = self.result()
        self.assertIsNone(result['execution'])
        self.assertEqual(result['valuation']['equity'], '100500.00')
        self.assertEqual(result['valuation']['buy_count'], 0)
        self.assertIn('未完成记账', result['valuation']['execution_note'])
        self.assertIsNone(self.store().get_execution('2026-11-20'))

    def test_holiday_reuses_existing_close_without_creating_holiday_row(self):
        self.save_account(self.day)
        self.run_task()
        self.context.update(day=date(2026, 11, 21), is_trading_day=False, previous_trading_day=self.day)
        self.current = self.current.replace(day=21)
        self.market.closing_prices.reset_mock()
        self.run_task()
        self.market.closing_prices.assert_not_called()
        self.assertIsNone(self.result('2026-11-21'))

    def test_failure_is_recorded_and_other_accounts_continue(self):
        self.save_account(self.day)
        self.configs['etf_rotation']['instances'].append({'id': 'second', 'enabled': True, 'mode': 'pap'})
        self.market.closing_prices.side_effect = ValueError('missing close')
        with patch.object(self.stop, 'wait', return_value=False) as wait:
            self.assertTrue(self.run_task())
        self.assertEqual(wait.call_count, 2)
        self.assertEqual(self.market.closing_prices.call_count, 3)
        result = self.result()
        self.assertEqual(result['valuation_status'], 'failed')
        self.assertIn('估值失败', result['valuation_error'])
        self.assertIsNone(result['valuation'])
        self.assertFalse((self.root / 'data/second').exists())

    def test_valuation_then_summary_sends_saved_returns_in_one_message(self):
        self.save_account(self.day)
        self.run_task()
        notifier = PushPlusNotifier({'pushplus': {'token': 'test-token'}})
        summary = DailySummaryTask(self.root, self.settings, self.configs, notifier,
                                   self.context, self.stop, lambda: self.current.replace(minute=10))
        with patch.object(notifier.client, 'send', return_value='receipt') as send:
            self.assertTrue(summary.run())
        send.assert_called_once()
        self.assertIn('100,995.00 元', send.call_args.args[1])
        self.assertIn('当日盈亏：+995.00 元', send.call_args.args[1])
        self.assertIn('策略：etf_rotation', send.call_args.args[1])

    def test_summary_does_not_wait_or_guess_returns_while_valuation_is_running(self):
        self.save_account(self.day)
        entered = threading.Event()
        release = threading.Event()

        def close(symbols, day):
            entered.set()
            release.wait(3)
            return {'510300.XSHG': Decimal('105')}

        self.market.closing_prices.side_effect = close
        notifier = Mock()
        with patch('market_trading_bot.task.DailyValuationTask.MarketProvider', return_value=self.market):
            self.task.start()
            try:
                self.assertTrue(entered.wait(2))
                summary = DailySummaryTask(self.root, self.settings, self.configs, notifier,
                                           self.context, self.stop, lambda: self.current.replace(minute=10))
                summary.run()
                result = notifier.send_daily_report.call_args.args[1][0]['summary']
                self.assertIn('仍在执行', result['notice'])
                self.assertNotIn('equity', result)
            finally:
                release.set()
                self.task.thread.join(timeout=3)
        self.assertEqual(self.result()['valuation_status'], 'success')
