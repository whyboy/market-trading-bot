"""离线验证行情校验边界及信号计算。"""
import pandas as pd
import unittest
from market_trading_bot.market.AKShareMarket import MarketDataError
from MarketFixture import MarketFixture


class AKShareMarketTests(MarketFixture, unittest.TestCase):
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
