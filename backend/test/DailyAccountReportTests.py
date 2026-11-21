"""每日总结区分交易日收益与休市日最近估值。"""
import unittest
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
        title, content = DailyAccountReport.build('etf_rotation', 'pap', self.summary())
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
        _, content = DailyAccountReport.build('etf_rotation', 'pap', summary)
        self.assertIn('今日休市', content)
        self.assertIn('2026-11-20T15:00:00+08:00', content)
        self.assertNotIn('当日盈亏', content)
        self.assertNotIn('今日交易', content)

    def test_loss_and_empty_positions(self):
        summary = self.summary()
        summary.update(daily_pnl='-300', daily_return_pct='-0.3', positions={})
        _, content = DailyAccountReport.build('etf_rotation', 'pap', summary)
        self.assertIn('当日盈亏：-300.00 元', content)
        self.assertIn('当日收益率：-0.30%', content)
        self.assertIn('持仓：空仓', content)

    def test_daily_report_is_sent_directly(self):
        notifier = PushPlusNotifier({'pushplus': {'token': 'test-token'}})
        with patch.object(notifier.client, 'send', return_value='receipt') as send:
            notifier.send_daily_account_report('etf_rotation', 'pap', self.summary())
        send.assert_called_once_with(*DailyAccountReport.build('etf_rotation', 'pap', self.summary()))
