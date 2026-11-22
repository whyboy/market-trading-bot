"""离线验证行情校验边界及信号计算。"""
import importlib
import json
import math
import pandas as pd
from datetime import datetime, timedelta
from market_trading_bot.market.MarketProvider import MarketProvider
from market_trading_bot.strategy.ETFRotationStrategyImpl import ETFRotationStrategyImpl
from pathlib import Path
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1] / 'dev'
MARKET_MODULE = importlib.import_module('market_trading_bot.market.MarketProvider')
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
        lines = []
        for quote in self.quotes:
            fields = ['0'] * 31
            fields[2], fields[3] = quote['f12'], str(quote['f2'])
            fields[30] = datetime.fromtimestamp(quote['f124'], ZoneInfo('Asia/Shanghai')).strftime('%Y%m%d%H%M%S')
            prefix = 'sh' if quote['f13'] == 1 else 'sz'
            lines.append(f'v_{prefix}{quote["f12"]}="{"~".join(fields)}";')
        response.text = '\n'.join(lines)
        with patch.object(MARKET_MODULE, 'datetime', wraps=datetime) as clock, \
                patch.object(MARKET_MODULE.ak, 'calendar_trade_date_hist_sina', return_value=pd.DataFrame(
                    {'trade_date': [day.date() for day in self.days] + [NOW.date()]})), \
                patch.object(MarketProvider, 'get_daily_closes', return_value=self.daily), \
                patch('market_trading_bot.market.source.HttpMarket.requests.get', return_value=response):
            clock.now.return_value = NOW
            result = MarketProvider(self.settings).load(
                self.strategy.symbols if symbols is None else symbols, self.strategy.history_points)
            return result
