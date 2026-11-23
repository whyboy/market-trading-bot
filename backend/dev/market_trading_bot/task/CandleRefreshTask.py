"""独立刷新策略标的日 K 线，失败时保留上次成功的数据。"""
import logging
import math
import threading
from datetime import date, datetime, time, timedelta
from market_trading_bot.market.MarketProvider import MarketProvider, SHANGHAI


class CandleRefreshTask:
    def __init__(self, settings, configs):
        self.settings = settings
        self.configs = configs
        self.lock = threading.Lock()
        self.data = {}
        self.logger = logging.getLogger(__name__)

    def snapshot(self, symbols):
        with self.lock:
            return {symbol: self.data.get(symbol, {'candles': [], 'updated_at': None, 'error': None})
                    for symbol in symbols}

    def run(self):
        """拉取最近 90 个自然日内已完成交易日的前复权日线。"""
        symbols = sorted({symbol for config in self.configs.values()
                          for symbol in config.get('parameters', {}).get('etf_pool', [])})
        if not symbols:
            return
        market = MarketProvider(self.settings['market'])
        now = datetime.now(SHANGHAI)
        try:
            calendar = market.trading_calendar()
            if not calendar or not calendar[0] <= now.date() <= calendar[-1]:
                raise ValueError('交易日历未覆盖当前日期')
            completed = [day for day in calendar if day < now.date() or day == now.date() and now.time() >= time(15)]
            if not completed:
                raise ValueError('交易日历缺少已完成交易日')
            end = completed[-1]
            start = now.date() - timedelta(days=89)
        except Exception as exc:
            self.logger.warning('K线交易日历获取失败: %s', exc)
            for symbol in symbols:
                self._set_error(symbol, str(exc))
            return
        for symbol in symbols:
            try:
                candles = market.get_daily_candles(symbol, start.isoformat(), end.isoformat())
                days = set()
                for candle in candles:
                    day = date.fromisoformat(candle['date'])
                    values = [candle[key] for key in ('open', 'high', 'low', 'close')]
                    if day in days or not start <= day <= end or any(not math.isfinite(value) or value <= 0 for value in values):
                        raise ValueError('K线日期或价格无效')
                    if candle['low'] > min(candle['open'], candle['close']) or candle['high'] < max(candle['open'], candle['close']):
                        raise ValueError('K线最高价或最低价无效')
                    days.add(day)
                if not candles:
                    raise ValueError('K线数据为空')
                candles.sort(key=lambda item: item['date'])
                with self.lock:
                    self.data[symbol] = {'candles': candles, 'updated_at': datetime.now(SHANGHAI).isoformat(), 'error': None}
            except Exception as exc:
                self.logger.warning('K线获取失败 symbol=%s error=%s', symbol, exc)
                self._set_error(symbol, str(exc))

    def _set_error(self, symbol, error):
        with self.lock:
            self.data[symbol] = {**self.data.get(symbol, {'candles': [], 'updated_at': None}), 'error': error}
