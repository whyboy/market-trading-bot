"""离线验证行情校验边界及信号计算。"""
import pandas as pd
import subprocess
import unittest
from datetime import date
from decimal import Decimal
from market_trading_bot.market.AKShareMarket import AKShareMarket, MarketDataError
from MarketFixture import MarketFixture
from unittest.mock import Mock, patch


class AKShareMarketTests(MarketFixture, unittest.TestCase):
    def test_baostock_daily_normalizes_dates_and_prices(self):
        market = AKShareMarket(self.settings)
        with patch('market_trading_bot.market.AKShareMarket.subprocess.run',
                   return_value=Mock(stdout='[["2026-11-20", "3.5000"]]')) as run:
            frame = market._daily('510300.XSHG', '20261101', '20261120', 'qfq')
        self.assertEqual(frame.to_dict('records'), [{'日期': '2026-11-20', '收盘': '3.5000'}])
        self.assertEqual(run.call_args.kwargs['timeout'], market.timeout)
        self.assertIn('"qfq"', run.call_args.kwargs['input'])

    def test_baostock_daily_timeout_is_market_error(self):
        market = AKShareMarket(self.settings)
        with patch('market_trading_bot.market.AKShareMarket.subprocess.run',
                   side_effect=subprocess.TimeoutExpired('baostock', 1)):
            with self.assertRaises(MarketDataError):
                market._daily('510300.XSHG', '20261101', '20261120', '')

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

    def test_closing_prices_require_exact_date_and_unadjusted_prices(self):

        day = date(2026, 11, 20)
        market = AKShareMarket(self.settings)
        with patch.object(market, '_request', return_value=pd.DataFrame({'日期': [day], '收盘': [3.5]})) as request:
            self.assertEqual(market.closing_prices(['510300.XSHG'], day), {'510300.XSHG': Decimal('3.5')})
        self.assertEqual(request.call_args.kwargs['adjust'], '')
        self.assertEqual(request.call_args.kwargs['start_date'], '20261120')

    def test_closing_prices_reject_stale_duplicate_missing_and_invalid_data(self):

        day = date(2026, 11, 20)
        frames = [pd.DataFrame(), pd.DataFrame({'日期': [date(2026, 11, 19)], '收盘': [3.5]}),
                  pd.DataFrame({'日期': [day, day], '收盘': [3.5, 3.5]})]
        frames.extend(pd.DataFrame({'日期': [day], '收盘': [price]}) for price in (0, -1, float('nan'), float('inf')))
        market = AKShareMarket(self.settings)
        for frame in frames:
            with self.subTest(frame=frame):
                with patch.object(market, '_request', return_value=frame):
                    with self.assertRaises(MarketDataError):
                        market.closing_prices(['510300.XSHG'], day)
