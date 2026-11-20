"""离线验证行情校验边界及信号计算。"""
import importlib
import json
import math
import pandas as pd
import unittest
from datetime import datetime, timedelta
from market_trading_bot.AKShareMarket import AKShareMarket, MarketDataError
from market_trading_bot.runner import preview_signals
from market_trading_bot.strategies.ETFRotationStrategy import ETFRotationStrategy
from pathlib import Path
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
MARKET_MODULE = importlib.import_module('market_trading_bot.AKShareMarket')
NOW = datetime(2026, 11, 20, 14, 30, tzinfo=ZoneInfo('Asia/Shanghai'))


class SignalTests(unittest.TestCase):
    def setUp(self):
        self.settings = json.loads((ROOT / 'config/global.json').read_text())['market']
        self.settings['request_interval_seconds'] = 0
        parameters = json.loads((ROOT / 'config/etf_rotation_strategy.json').read_text())['parameters']
        self.strategy = ETFRotationStrategy(parameters)
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

    def test_valid_market_data_produces_finite_signals(self):
        as_of, snapshots = self.load()
        result = self.strategy.calculate(snapshots, as_of)
        self.assertEqual(result.ranking[0].symbol, self.strategy.symbols[0])
        self.assertTrue(all(math.isfinite(item.score) for item in result.ranking))
        self.assertTrue(all(0 <= weight <= 1 for weight in result.target_weights.values()))
        self.assertEqual(sum(weight > 0 for weight in result.target_weights.values()), 1)

    def test_market_rejects_invalid_inputs(self):
        cases = ('invalid_symbol', 'missing_day', 'duplicate_day', 'zero_price',
                 'nan_price', 'stale_quote', 'future_quote', 'missing_quote', 'duplicate_quote')
        for case in cases:
            with self.subTest(case=case):
                self.setUp()
                symbols = None
                if case == 'invalid_symbol':
                    symbols = ('invalid',)
                elif case == 'missing_day':
                    self.daily = self.daily.iloc[1:]
                elif case == 'duplicate_day':
                    self.daily = pd.concat([self.daily, self.daily.iloc[-1:]])
                elif case in ('zero_price', 'nan_price'):
                    self.daily.loc[0, '收盘'] = 0 if case == 'zero_price' else float('nan')
                elif case in ('stale_quote', 'future_quote'):
                    self.quotes[0]['f124'] += -61 if case == 'stale_quote' else 1
                elif case == 'missing_quote':
                    self.quotes.pop()
                else:
                    self.quotes.append(self.quotes[0].copy())
                with self.assertRaises(MarketDataError):
                    self.load(symbols)

    def test_preview_signals_uses_validated_market_data(self):
        as_of, snapshots = self.load()
        with patch('market_trading_bot.runner.AKShareMarket') as market, \
                patch('market_trading_bot.runner.strategy_logging'):
            market.return_value.load.return_value = as_of, snapshots
            result = preview_signals(ROOT, ROOT / 'config/etf_rotation_strategy.json')
        self.assertEqual(result['operation'], 'signal_preview')
        self.assertEqual(set(result['target_weights']), set(self.strategy.symbols))


if __name__ == '__main__':
    unittest.main()
