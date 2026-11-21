"""独立行情源的解析与故障切换。"""
import pandas as pd
import unittest
from datetime import date, datetime
from market_trading_bot.market.source.HttpMarket import HttpMarket
from market_trading_bot.market.MarketProvider import MarketProvider
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo


class MarketProviderTests(unittest.TestCase):
    def test_missing_baostock_day_uses_tencent_qfq(self):
        source = MarketProvider(15)
        missing = pd.DataFrame({'日期': ['2026-11-19'], '收盘': ['2.30']})
        expected = pd.DataFrame({'日期': ['2026-11-20'], '收盘': ['2.23']})
        with patch.object(source.baostock, 'daily', return_value=missing), \
                patch.object(source.http, 'daily', return_value=expected) as fetch:
            self.assertIs(source.daily('159915.XSHE', '2026-11-01', '2026-11-20', 'qfq',
                                       [date(2026, 11, 20)]), expected)
        fetch.assert_called_once_with('159915.XSHE', '2026-11-01', '2026-11-20', 'qfq', 'tencent')

    def test_sina_failure_uses_tencent_quote(self):
        fields = ['0'] * 31
        fields[2], fields[3], fields[30] = '159915', '2.230', '20261120143000'
        response = Mock(text=f'v_sz159915="{"~".join(fields)}";')
        now = datetime(2026, 11, 20, 14, 30, 10, tzinfo=ZoneInfo('Asia/Shanghai'))
        source = MarketProvider(15)
        with patch('market_trading_bot.market.source.HttpMarket.requests.get', side_effect=[Mock(text=''), response]):
            self.assertEqual(source.quotes(['159915.XSHE'], lambda: now, 60)['159915.XSHE'][0], 2.23)

    def test_sina_quote_time(self):
        fields = ['0'] * 32
        fields[3], fields[30], fields[31] = '2.230', '2026-11-20', '14:30:01'
        response = Mock(text=f'var hq_str_sz159915="{",".join(fields)}";')
        with patch('market_trading_bot.market.source.HttpMarket.requests.get', return_value=response):
            result = HttpMarket(15).quotes(['159915.XSHE'], 'sina')
        self.assertEqual(result['159915.XSHE'], (2.23, datetime(2026, 11, 20, 14, 30, 1,
                                                              tzinfo=ZoneInfo('Asia/Shanghai'))))
