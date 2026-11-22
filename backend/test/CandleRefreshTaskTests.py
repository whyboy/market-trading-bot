"""验证日 K 缓存只接受已完成日线，并在失败时保留旧数据。"""
import unittest
from datetime import date, datetime
from market_trading_bot.task.CandleRefreshTask import CandleRefreshTask
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo


class CandleRefreshTaskTests(unittest.TestCase):
    def test_refresh_and_preserve_previous_data_on_error(self):
        task = CandleRefreshTask({'market': {}}, {'strategy': {'parameters': {'etf_pool': ['510180.XSHG']}}})
        market = MagicMock()
        market.trading_calendar.return_value = [date(2026, 11, 19), date(2026, 11, 20)]
        market.get_daily_candles.return_value = [{'date': '2026-11-19', 'open': 2.0, 'high': 2.2, 'low': 1.9, 'close': 2.1}]
        with patch('market_trading_bot.task.CandleRefreshTask.MarketProvider', return_value=market), \
             patch('market_trading_bot.task.CandleRefreshTask.datetime') as now:
            now.now.return_value = datetime(2026, 11, 20, 10, tzinfo=ZoneInfo('Asia/Shanghai'))
            task.run()
            market.get_daily_candles.assert_called_once_with('510180.XSHG', '2026-08-23', '2026-11-19')
            market.get_daily_candles.return_value = [{'date': '2026-11-20', 'open': 2.0, 'high': 2.2, 'low': 1.9, 'close': 2.1}]
            task.run()
        result = task.snapshot(['510180.XSHG'])['510180.XSHG']
        self.assertEqual(result['candles'][0]['date'], '2026-11-19')
        self.assertIsNotNone(result['error'])
