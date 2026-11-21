"""离线验证行情校验边界及信号计算。"""
import importlib
import json
import math
import pandas as pd
from datetime import datetime, timedelta
from market_trading_bot.market.source.AKShareMarket import AKShareMarket
from market_trading_bot.strategy.ETFRotationStrategyImpl import ETFRotationStrategyImpl
from pathlib import Path
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1] / 'dev'
MARKET_MODULE = importlib.import_module('market_trading_bot.market.source.AKShareMarket')
NOW = datetime(2026, 11, 20, 14, 30, tzinfo=ZoneInfo('Asia/Shanghai'))


class MarketFixture:
    def setUp(self):
        self.settings = json.loads((ROOT / 'market_trading_bot/config/global.json').read_text())['market']
        self.settings['request_interval_seconds'] = 0
        parameters = json.loads((ROOT / 'market_trading_bot/config/etf_rotation_strategy.json').read_text())['parameters']
        self.strategy = ETFRotationStrategyImpl(parameters)
        self.days = pd.bdate_range(end=NOW.date() - timedelta(days=1), periods=self.strategy.history_points)
        self.daily = pd.DataFrame({'日期': self.days, '收盘': [100 * math.exp(i * .001) for i in range(len(self.days))]})
        self.quotes = [
            {'f13': 1 if symbol.endswith('XSHG') else 0, 'f12': symbol[:6],
             'f2': 100 * math.exp(len(self.days) * .001), 'f124': int(NOW.timestamp())}
            for symbol in self.strategy.symbols
        ]

    def load(self, symbols=None):
        response = Mock()
        response.json.return_value = {'rc': 0, 'data': {'diff': self.quotes}}
        with patch.object(MARKET_MODULE, 'datetime', wraps=datetime) as clock, \
                patch.object(MARKET_MODULE.ak, 'calendar_trade_date_hist_sina', return_value=pd.DataFrame(
                    {'trade_date': [day.date() for day in self.days] + [NOW.date()]})), \
                patch.object(MARKET_MODULE.ak, 'fund_etf_hist_em', return_value=self.daily), \
                patch.object(MARKET_MODULE.requests, 'get', return_value=response) as request:
            clock.now.return_value = NOW
            result = AKShareMarket(self.settings).load(
                self.strategy.symbols if symbols is None else symbols, self.strategy.history_points)
            self.assertEqual(request.call_count, 1)
            return result
