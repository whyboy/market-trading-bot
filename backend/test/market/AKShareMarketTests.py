"""离线验证行情校验边界及信号计算。"""
import pandas as pd
import subprocess
import unittest
from datetime import date, datetime
from decimal import Decimal
from market_trading_bot.market.AKShareMarket import AKShareMarket, MarketDataError
from market_trading_bot.market.BaoStockDaily import BaoStockDaily
from market_trading_bot.market.HttpMarket import HttpMarket
from market_trading_bot.market.MarketSource import MarketSource
from MarketFixture import MarketFixture
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo


class AKShareMarketTests(MarketFixture, unittest.TestCase):
    def test_sina_quote_parses_price_and_shanghai_time(self):
        market = HttpMarket(15)
        fields = ['0'] * 32
        fields[3], fields[30], fields[31] = '2.230', '2026-11-20', '14:30:01'
        response = Mock(text=f'var hq_str_sz159915="{",".join(fields)}";')
        with patch('market_trading_bot.market.HttpMarket.requests.get', return_value=response) as request:
            quotes = market.quotes(['159915.XSHE'], 'sina')
        self.assertEqual(quotes['159915.XSHE'], (2.23, datetime(2026, 11, 20, 14, 30, 1,
                                                               tzinfo=ZoneInfo('Asia/Shanghai'))))
        self.assertEqual(response.encoding, 'gbk')
        self.assertIn('sz159915', request.call_args.args[0])

    def test_sina_quote_rejects_invalid_response(self):
        market = HttpMarket(15)
        for body in ('', 'bad response', 'var hq_str_sh510300="";',
                     'var hq_str_sh510300="' + ','.join(['0'] * 32) + '";'):
            with self.subTest(body=body), patch('market_trading_bot.market.HttpMarket.requests.get',
                                                return_value=Mock(text=body)):
                with self.assertRaises(ValueError):
                    market.quotes(['510300.XSHG'], 'sina')

    def test_baostock_daily_normalizes_dates_and_prices(self):
        market = BaoStockDaily(15)
        with patch('market_trading_bot.market.BaoStockDaily.subprocess.run',
                   return_value=Mock(stdout='[["2026-11-20", "3.5000"]]')) as run:
            frame = market.daily('510300.XSHG', '2026-11-01', '2026-11-20', 'qfq')
        self.assertEqual(frame.to_dict('records'), [{'日期': '2026-11-20', '收盘': '3.5000'}])
        self.assertEqual(run.call_args.kwargs['timeout'], market.timeout)
        self.assertIn('"qfq"', run.call_args.kwargs['input'])

    def test_baostock_daily_timeout_is_market_error(self):
        market = BaoStockDaily(15)
        with patch('market_trading_bot.market.BaoStockDaily.subprocess.run',
                   side_effect=subprocess.TimeoutExpired('baostock', 1)):
            with self.assertRaises(subprocess.TimeoutExpired):
                market.daily('510300.XSHG', '2026-11-01', '2026-11-20', '')

    def test_daily_falls_back_to_tencent_with_same_adjustment(self):
        source = MarketSource(15)
        expected = pd.DataFrame({'日期': ['2026-11-20'], '收盘': ['2.23']})
        with patch.object(source.baostock, 'daily', side_effect=TimeoutError), \
                patch.object(source.http, 'daily', return_value=expected) as fetch:
            self.assertIs(source.daily('159915.XSHE', '2026-11-01', '2026-11-20', 'qfq'), expected)
        fetch.assert_called_once_with('159915.XSHE', '2026-11-01', '2026-11-20', 'qfq', 'tencent')

    def test_tencent_quote_and_stale_sina_fallback(self):
        fields = ['0'] * 31
        fields[2], fields[3], fields[30] = '159915', '2.230', '20261120143000'
        response = Mock(text=f'v_sz159915="{"~".join(fields)}";')
        now = datetime(2026, 11, 20, 14, 30, 10, tzinfo=ZoneInfo('Asia/Shanghai'))
        source = MarketSource(15)
        with patch('market_trading_bot.market.HttpMarket.requests.get', side_effect=[Mock(text=''), response]):
            self.assertEqual(source.quotes(['159915.XSHE'], lambda: now, 60)['159915.XSHE'][0], 2.23)

    def test_missing_daily_date_triggers_fallback(self):
        source = MarketSource(15)
        missing = pd.DataFrame({'日期': ['2026-11-19'], '收盘': ['2.30']})
        expected = pd.DataFrame({'日期': ['2026-11-20'], '收盘': ['2.23']})
        with patch.object(source.baostock, 'daily', return_value=missing), \
                patch.object(source.http, 'daily', return_value=expected):
            self.assertIs(source.daily('159915.XSHE', '2026-11-01', '2026-11-20', 'qfq',
                                       [date(2026, 11, 20)]), expected)

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
