"""每日总结区分交易日收益与休市日最近估值。"""
import unittest
from datetime import date
from market_trading_bot.notification.PushPlusNotifier import PushPlusNotifier
from market_trading_bot.notification.report.DailyAccountReport import DailyAccountReport
from unittest.mock import patch


class DailyAccountReportTests(unittest.TestCase):
    def summary(self):
        return {'date': '2026-11-20', 'as_of': '2026-11-20T15:00:00+08:00',
                'is_trading_day': True, 'comparison_date': '2026-11-19',
                'equity': '102800.00', 'cash': '12350.00',
                'daily_pnl': '800', 'daily_return_pct': '0.784314',
                'cumulative_pnl': '2800', 'cumulative_return_pct': '2.8',
                'positions': {'510300.XSHG': {'quantity': 100}}, 'buy_count': 1, 'sell_count': 0}

    def test_trading_day_formats_supplied_returns_and_holdings(self):
        title, content = self.build(self.summary())
        self.assertIn('每日收益总结', title)
        self.assertIn('当日盈亏：+800.00 元', content)
        self.assertIn('当日收益率：+0.78%', content)
        self.assertIn('累计收益率：+2.80%', content)
        self.assertIn('比较基准：2026-11-19', content)
        self.assertIn('510300.XSHG × 100 份', content)
        self.assertIn('买入 1 笔，卖出 0 笔', content)

    def test_holiday_shows_valuation_date_without_stale_daily_returns(self):
        summary = self.summary()
        summary.update(date='2026-11-21', is_trading_day=False)
        _, content = self.build(summary)
        self.assertIn('今日休市', content)
        self.assertIn('2026-11-20T15:00:00+08:00', content)
        self.assertNotIn('当日盈亏', content)
        self.assertNotIn('今日交易', content)

    def test_loss_and_empty_positions(self):
        summary = self.summary()
        summary.update(daily_pnl='-300', daily_return_pct='-0.3', positions={})
        _, content = self.build(summary)
        self.assertIn('当日盈亏：-300.00 元', content)
        self.assertIn('当日收益率：-0.30%', content)
        self.assertIn('持仓：空仓', content)

    def build(self, summary):
        return DailyAccountReport.build(date(2026, 11, 20), [
            {'strategy_id': 'etf_rotation', 'instance_id': 'pap', 'summary': summary}])

    def test_daily_report_is_sent_directly(self):
        notifier = PushPlusNotifier({'pushplus': {'token': 'test-token'}})
        summaries = [{'strategy_id': 'etf_rotation', 'instance_id': 'pap', 'summary': self.summary()}]
        with patch.object(notifier.client, 'send', return_value='receipt') as send:
            notifier.send_daily_summary_report(date(2026, 11, 20), summaries)
        send.assert_called_once_with(*self.build(self.summary()))

    def test_unavailable_valuation_only_shows_status_not_old_returns(self):
        _, content = self.build({
            'date': '2026-11-20', 'notice': '收盘估值失败，请查看日志。'})
        self.assertIn('收盘估值失败', content)
        self.assertNotIn('当日收益率', content)
        self.assertNotIn('总资产', content)

    def test_incomplete_execution_is_explicitly_labelled(self):
        summary = self.summary()
        summary['execution_note'] = '今日策略未完成记账，按现有持仓估值。'
        _, content = self.build(summary)
        self.assertIn('未完成记账', content)

    def test_multiple_accounts_are_grouped_by_strategy_in_one_message(self):
        summaries = [{'strategy_id': 'rotation', 'instance_id': 'first', 'summary': self.summary()},
                     {'strategy_id': 'other', 'instance_id': 'second', 'summary': {'notice': '估值失败'}},
                     {'strategy_id': 'rotation', 'instance_id': 'third', 'summary': self.summary()}]
        title, content = DailyAccountReport.build(date(2026, 11, 20), summaries)
        self.assertEqual(title, '每日收益总结｜2026-11-20')
        self.assertEqual(content.count('策略：rotation'), 1)
        for account in ('first', 'second', 'third'):
            self.assertIn(f'账户：{account}', content)
        self.assertIn('估值失败', content)
