"""一次性日报任务的收盘收益、跨策略汇总与故障隔离。"""
import tempfile
import threading
import unittest
from datetime import date, datetime
from decimal import Decimal
from market_trading_bot.DailySummaryTask import DailySummaryTask
from market_trading_bot.market.AKShareMarket import SHANGHAI
from market_trading_bot.repository.PapAccountRepository import PapAccountRepository
from pathlib import Path
from unittest.mock import Mock, patch


class DailySummaryTaskTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.day = date(2026, 11, 20)
        self.current = datetime(2026, 11, 20, 15, 5, tzinfo=SHANGHAI)
        self.calendar = [date(2026, 11, value) for value in (18, 19, 20, 23, 24)]
        self.settings = {'storage': {'data_dir': 'data'}, 'market': {}}
        self.configs = {'etf_rotation': {'instances': [{'id': 'pap', 'enabled': True, 'mode': 'pap'}]}}
        self.notifier = Mock()
        self.stop = threading.Event()
        self.running = Mock(return_value=False)
        self.context = {'day': self.day, 'is_trading_day': True, 'previous_trading_day': date(2026, 11, 19)}
        self.service = DailySummaryTask(self.root, self.settings, self.configs, self.notifier, self.context,
                                        self.stop, self.running, lambda: self.current)
        self.market = Mock()
        self.market.trading_calendar.return_value = self.calendar
        self.market.closing_prices.side_effect = lambda symbols, day: {
            symbol: Decimal('105' if day == self.day else '100') for symbol in symbols}

    def save_account(self, day, cash='79995', quantity=200, instance='pap'):
        repository = PapAccountRepository(self.root / 'data', instance)
        with repository.begin_rebalance_transaction(day.isoformat(), 100000) as transaction:
            transaction.save_execution_result({'date': day.isoformat(), 'cash': cash, 'equity': '100000',
                              'positions': {'510300.XSHG': {'quantity': quantity}} if quantity else {},
                              'fills': [{'side': 'buy', 'amount': '20000', 'commission': '5'}],
                              'signal': {'as_of': f'{day}T14:30:00+08:00'}})
        return repository

    def summary(self):
        return self.service._summary(self.market, 'etf_rotation', 'pap')

    def test_first_day_uses_actual_initial_cash_and_includes_fees(self):
        repository = self.save_account(self.day)
        before = repository.database.read_bytes()
        summary = self.summary()
        self.assertEqual(summary['equity'], '100995.00')
        self.assertEqual(summary['daily_pnl'], '995.00')
        self.assertEqual(Decimal(summary['daily_return_pct']), Decimal('0.995'))
        self.assertEqual(summary['comparison_date'], '开户资金')
        self.assertEqual(summary['buy_count'], 1)
        self.assertEqual(repository.database.read_bytes(), before)
        snapshot = self.service.valuations.find_latest_on_or_before('pap', self.day.isoformat())
        self.assertEqual(snapshot['equity'], summary['equity'])

    def test_missing_prior_valuation_is_rebuilt_from_historical_holdings(self):
        self.save_account(date(2026, 11, 19), cash='90000', quantity=100)
        self.save_account(self.day)
        summary = self.summary()
        self.assertEqual(summary['comparison_date'], '2026-11-19')
        self.assertEqual(summary['daily_pnl'], '995.00')
        previous = self.service.valuations.find_latest_on_or_before('pap', '2026-11-19')
        self.assertEqual(previous['equity'], '100000.00')
        self.assertEqual(previous['positions']['510300.XSHG']['quantity'], 100)

    def test_existing_previous_close_is_used_without_fetching_it_again(self):
        self.save_account(self.day)
        self.service.valuations.save_if_absent('pap', {'date': '2026-11-19', 'equity': '100500'})
        summary = self.summary()
        self.assertEqual(summary['daily_pnl'], '495.00')
        self.market.closing_prices.assert_called_once()

    def test_missed_strategy_execution_still_values_existing_positions(self):
        self.save_account(date(2026, 11, 19), cash='90000', quantity=100)
        summary = self.summary()
        self.assertEqual(summary['equity'], '100500.00')
        self.assertEqual(summary['daily_pnl'], '500.00')
        self.assertEqual(summary['buy_count'], 0)
        self.assertIn('未完成记账', summary['execution_note'])

    def test_cash_only_account_needs_no_market_quotes(self):
        self.save_account(self.day, cash='100000', quantity=0)
        self.assertEqual(self.summary()['equity'], '100000.00')
        self.assertEqual(self.market.closing_prices.call_args.args[0], {})

    def test_uninitialized_account_sends_notice_without_creating_ledger(self):
        summary = self.summary()
        self.assertIn('尚未开户', summary['notice'])
        self.assertFalse((self.root / 'data/pap').exists())
        self.market.closing_prices.assert_not_called()

    def test_holiday_uses_latest_snapshot_and_preserves_valuation_timestamp(self):
        self.save_account(self.day)
        close = self.summary()
        self.market.closing_prices.reset_mock()
        self.context.update(day=date(2026, 11, 21), is_trading_day=False, previous_trading_day=self.day)
        summary = self.service._summary(self.market, 'etf_rotation', 'pap')
        self.assertFalse(summary['is_trading_day'])
        self.assertEqual(summary['date'], '2026-11-21')
        self.assertEqual(summary['as_of'], close['as_of'])
        self.market.closing_prices.assert_not_called()

    def test_first_holiday_run_builds_last_trading_day_close(self):
        self.save_account(self.day)
        self.context.update(day=date(2026, 11, 21), is_trading_day=False, previous_trading_day=self.day)
        summary = self.service._summary(self.market, 'etf_rotation', 'pap')
        self.assertFalse(summary['is_trading_day'])
        self.assertEqual(summary['as_of'], '2026-11-20T15:00:00+08:00')
        self.market.closing_prices.assert_called_once()

    def test_valuation_failure_sends_notice_and_does_not_stop_other_accounts(self):
        self.save_account(self.day)
        self.configs['etf_rotation']['instances'].append({'id': 'second', 'enabled': True, 'mode': 'pap'})
        self.market.closing_prices.side_effect = ValueError('missing close')
        with patch('market_trading_bot.DailySummaryTask.AKShareMarket', return_value=self.market):
            with patch.object(self.stop, 'wait', return_value=False) as wait:
                self.assertTrue(self.service.run())
        self.assertEqual(wait.call_count, 2)
        self.assertEqual(self.market.closing_prices.call_count, 3)
        self.notifier.send_daily_summary_report.assert_called_once()
        accounts = self.notifier.send_daily_summary_report.call_args.args[1]
        self.assertEqual(len(accounts), 2)
        self.assertIn('估值失败', accounts[0]['summary']['notice'])
        self.assertIn('尚未开户', accounts[1]['summary']['notice'])
        self.assertIsNone(self.service.valuations.find_latest_on_or_before('pap', self.day.isoformat()))

    def test_delayed_close_retries_and_succeeds(self):
        self.save_account(self.day)
        self.market.closing_prices.side_effect = [ValueError('not ready'), {'510300.XSHG': Decimal('105')}]
        with patch('market_trading_bot.DailySummaryTask.AKShareMarket', return_value=self.market):
            with patch.object(self.stop, 'wait', return_value=False) as wait:
                self.service.run()
        wait.assert_called_once_with(30)
        self.assertEqual(self.notifier.send_daily_summary_report.call_args.args[1][0]['summary']['equity'], '100995.00')

    def test_stop_interrupts_retry_without_sending_report(self):
        self.save_account(self.day)
        self.market.closing_prices.side_effect = ValueError('offline')
        with patch('market_trading_bot.DailySummaryTask.AKShareMarket', return_value=self.market):
            with patch.object(self.stop, 'wait', return_value=True):
                self.assertFalse(self.service.run())
        self.notifier.send_daily_summary_report.assert_not_called()

    def test_calendar_failure_from_scheduler_is_reported_once_without_fetching_calendar(self):
        self.context['notice'] = '交易日历获取失败，请查看日志。'
        self.assertTrue(self.service.run())
        self.market.trading_calendar.assert_not_called()
        self.notifier.send_daily_summary_report.assert_called_once()
        self.assertIn('交易日历获取失败', self.notifier.send_daily_summary_report.call_args.args[1][0]['summary']['notice'])

    def test_running_task_waits_then_reports_failure_instead_of_old_assets(self):
        self.save_account(self.day)
        self.running.return_value = True
        with patch('market_trading_bot.DailySummaryTask.AKShareMarket', return_value=self.market):
            with patch.object(self.stop, 'wait', return_value=False):
                self.service.run()
        summary = self.notifier.send_daily_summary_report.call_args.args[1][0]['summary']
        self.assertIn('仍在运行', summary['notice'])
        self.assertNotIn('equity', summary)
        self.market.closing_prices.assert_not_called()

    def test_restart_reuses_saved_close_but_sends_report_again(self):
        self.save_account(self.day)
        self.summary()
        restarted = DailySummaryTask(self.root, self.settings, self.configs, self.notifier, self.context,
                                     self.stop, self.running, lambda: self.current)
        self.market.closing_prices.reset_mock()
        with patch('market_trading_bot.DailySummaryTask.AKShareMarket', return_value=self.market):
            restarted.run()
        self.market.closing_prices.assert_not_called()
        self.notifier.send_daily_summary_report.assert_called_once()

    def test_shutdown_during_price_fetch_does_not_send_report(self):
        self.save_account(self.day)

        def close(symbols, day):
            self.stop.set()
            return {'510300.XSHG': Decimal('105')}

        self.market.closing_prices.side_effect = close
        with patch('market_trading_bot.DailySummaryTask.AKShareMarket', return_value=self.market):
            self.assertFalse(self.service.run())
        self.notifier.send_daily_summary_report.assert_not_called()

    def test_future_ledger_state_does_not_change_previous_close(self):
        self.save_account(date(2026, 11, 19), cash='90000', quantity=100)
        self.save_account(self.day, cash='79995', quantity=200)
        previous = PapAccountRepository.get_account_snapshot_on_or_before(self.root / 'data', 'pap', '2026-11-19')
        self.assertEqual(previous['cash'], '90000')
        self.assertEqual(previous['positions']['510300.XSHG']['quantity'], 100)

    def test_all_strategies_are_sent_in_one_report_and_task_owns_no_calendar_calls(self):
        self.save_account(self.day)
        self.configs['second_strategy'] = {'instances': [{'id': 'second', 'enabled': True, 'mode': 'pap'}]}
        with patch('market_trading_bot.DailySummaryTask.AKShareMarket', return_value=self.market):
            self.service.start()
            self.service.thread.join(timeout=3)
        self.assertFalse(self.service.thread.is_alive())
        self.assertTrue(self.service.completed)
        self.notifier.send_daily_summary_report.assert_called_once()
        accounts = self.notifier.send_daily_summary_report.call_args.args[1]
        self.assertEqual({entry['strategy_id'] for entry in accounts}, {'etf_rotation', 'second_strategy'})
        self.market.trading_calendar.assert_not_called()
