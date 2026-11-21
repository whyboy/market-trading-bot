"""每日总结区分交易日收益与休市日最近估值。"""
import unittest
from datetime import date
from market_trading_bot.notification.report.DailyAccountReport import DailyAccountReport


class DailyAccountReportTests(unittest.TestCase):
    def summary(self):
        return {'date': '2026-11-20', 'as_of': '2026-11-20T15:00:00+08:00',
                'is_trading_day': True, 'comparison_date': '2026-11-19',
                'equity': '102800.00', 'cash': '12350.00',
                'daily_pnl': '800', 'daily_return_pct': '0.784314',
                'cumulative_pnl': '2800', 'cumulative_return_pct': '2.8',
                'positions': {'510300.XSHG': {'quantity': 100}}, 'buy_count': 1, 'sell_count': 0}

    def test_holiday_shows_valuation_date_without_stale_daily_returns(self):
        summary = self.summary()
        summary.update(date='2026-11-21', is_trading_day=False)
        _, content = self.build(summary)
        self.assertIn('今日休市', content)
        self.assertIn('2026-11-20T15:00:00+08:00', content)
        self.assertNotIn('当日盈亏', content)
        self.assertNotIn('今日交易', content)

    def build(self, summary):
        return DailyAccountReport.build(date(2026, 11, 20), [
            {'strategy_id': 'etf_rotation', 'instance_id': 'pap', 'summary': summary}])

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
