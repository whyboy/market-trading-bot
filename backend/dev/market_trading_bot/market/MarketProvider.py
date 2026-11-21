"""统一行情源与失败切换。"""
import akshare as ak
import logging
import math
import pandas as pd
import re
import time as clock
from akshare.utils.context import config as ak_config
from datetime import datetime, time, timedelta
from decimal import Decimal
from market_trading_bot.market.MarketData import DailyClose, MarketData
from market_trading_bot.market.source.BaoStockDaily import BaoStockDaily
from market_trading_bot.market.source.HttpMarket import HttpMarket
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo('Asia/Shanghai')


class MarketDataError(RuntimeError):
    """行情缺失、过期或无法验证时停止计算。"""


class MarketProvider:
    def __init__(self, timeout, logger=None):
        self.logger = logger or logging.getLogger(__name__)
        settings = timeout if isinstance(timeout, dict) else None
        timeout = float(settings['request_timeout_seconds']) if settings else float(timeout)
        self.max_quote_age = int(settings['max_quote_age_seconds']) if settings else 60
        self.lookback_days = int(settings['history_lookback_days']) if settings else 60
        self.interval = float(settings['request_interval_seconds']) if settings else 0.0
        self.last_request = 0.0
        if not math.isfinite(self.interval) or self.interval < 0:
            raise ValueError('请求间隔必须为非负有限数')
        if not math.isfinite(timeout) or min(self.max_quote_age, self.lookback_days, timeout) <= 0:
            raise ValueError('行情超时、时效和回溯天数必须为正数')
        ak_config.timeout = timeout
        self.baostock = BaoStockDaily(timeout)
        self.http = HttpMarket(timeout)

    def get_quotes(self, symbols, now, max_age):
        errors = []
        for provider in ('tencent', 'sina'):
            try:
                quotes = self.http.get_quotes(symbols, provider)
                observed_at = now()
                for price, quoted_at in quotes.values():
                    age = (observed_at - quoted_at).total_seconds()
                    if not math.isfinite(price) or price <= 0 or quoted_at.date() != observed_at.date() or not 0 <= age <= max_age:
                        raise ValueError('报价价格无效或时间过期')
                return quotes
            except Exception as exc:
                errors.append(f'{provider}: {exc}')
                self.logger.warning('报价源失败 provider=%s error=%s', provider, exc)
        raise ValueError('所有报价源失败: ' + '; '.join(errors))

    def get_daily_closes(self, symbol, start, end, adjust, required_days=None):
        if adjust not in ('', 'qfq'):
            raise ValueError('不支持的复权类型')
        sources = [('baostock', lambda: self.baostock.get_daily_closes(symbol, start, end, adjust)),
                   ('tencent', lambda: self.http.get_daily_closes(symbol, start, end, adjust, 'tencent'))]
        if not adjust:
            sources.append(('sina', lambda: self.http.get_daily_closes(symbol, start, end, adjust, 'sina')))
        errors = []
        for name, fetch in sources:
            try:
                daily = fetch()
                if daily.empty:
                    raise ValueError('日线为空')
                dates = pd.to_datetime(daily['日期'], errors='raise')
                prices = pd.to_numeric(daily['收盘'], errors='raise')
                if dates.isna().any() or dates.duplicated().any() or prices.isna().any() or any(not math.isfinite(p) or p <= 0 for p in prices):
                    raise ValueError('日线日期或价格无效')
                if required_days is not None and not set(required_days).issubset(set(dates.dt.date)):
                    raise ValueError('日线缺少所需交易日')
                return daily
            except Exception as exc:
                errors.append(f'{name}: {exc}')
                self.logger.warning('日线源失败 provider=%s symbol=%s error=%s', name, symbol, exc)
        raise ValueError(f'{symbol}: 所有日线源失败: ' + '; '.join(errors))

    def load(self, symbols: tuple[str, ...], history_points: int) -> tuple[datetime, dict[str, MarketData]]:
        """加载连续历史收盘价及当日报价，校验交易时段、日期和价格。"""
        started_at = datetime.now(SHANGHAI)
        self.logger.info('行情获取开始 daily=baostock,tencent quotes=tencent,sina symbols=%s', ','.join(symbols))
        try:
            calendar = self.trading_calendar()
            if not calendar or not calendar[0] <= started_at.date() <= calendar[-1]:
                raise MarketDataError('交易日历未覆盖当前日期')
            if started_at.date() not in calendar or not self._is_trading_session(started_at):
                return self._load_close_snapshots(symbols, history_points, started_at, calendar)
            for symbol in symbols:
                if not re.fullmatch(r'\d{6}\.(XSHG|XSHE)', symbol):
                    raise MarketDataError(f'不支持的证券代码: {symbol}')
            self.logger.info('交易日历获取成功 date=%s coverage=%s~%s', started_at.date(), calendar[0], calendar[-1])
            trading_days = [day for day in calendar if day < started_at.date()][-history_points:]
            if len(trading_days) != history_points:
                raise MarketDataError('交易日历历史不足')
            histories = self._load_daily_closes(symbols, trading_days, started_at.date())
            # 历史加载后批量取价，减少报价时间差。
            self.logger.info('批量当前报价请求开始 count=%d', len(symbols))
            quotes = self._request(self.get_quotes, symbols=symbols, now=lambda: datetime.now(SHANGHAI), max_age=self.max_quote_age)
            as_of = datetime.now(SHANGHAI)
            self._check_trading_session(as_of)
            if as_of.date() != started_at.date():
                raise MarketDataError('行情获取跨交易日，停止计算')
            snapshots = {}
            for symbol in symbols:
                price, quoted_at = quotes[symbol]
                self.logger.info('当前报价获取结果 symbol=%s price=%s quote_time=%s', symbol, price, quoted_at.isoformat())
                age = (as_of - quoted_at).total_seconds()
                if quoted_at.date() != as_of.date() or not 0 <= age <= self.max_quote_age:
                    raise MarketDataError(f'{symbol}: 报价过期或来自未来，更新时间为{quoted_at.isoformat()}')
                history = histories[symbol]
                if not math.isfinite(price) or price <= 0:
                    raise MarketDataError(f'{symbol}: 价格无效')
                snapshots[symbol] = MarketData(symbol, quoted_at, price, history)
            self.logger.info('行情获取成功 count=%d as_of=%s', len(snapshots), as_of.isoformat())
            return as_of, snapshots
        except MarketDataError as exc:
            self.logger.error('行情获取失败: %s', exc)
            raise
        except Exception as exc:
            self.logger.error('行情接口请求或解析失败: %s', exc)
            raise MarketDataError(f'行情获取失败: {exc}') from exc

    def closing_prices(self, symbols, day):
        """查询 day 当天的不复权收盘价，返回 symbol 到 Decimal 的映射。"""
        prices = {}
        for symbol in sorted(symbols):
            if not re.fullmatch(r'\d{6}\.(XSHG|XSHE)', symbol):
                raise MarketDataError(f'不支持的证券代码: {symbol}')
            daily = self._request(self.get_daily_closes, symbol=symbol, start=day.isoformat(),
                                  end=day.isoformat(), adjust='', required_days=[day])
            if daily.empty:
                raise MarketDataError(f'{symbol}: {day} 收盘价尚未更新')
            dates = pd.to_datetime(daily['日期'], errors='raise').dt.date
            if len(daily) != 1 or dates.iloc[0] != day:
                raise MarketDataError(f'{symbol}: 收盘价日期错误或重复')
            price = Decimal(str(daily.iloc[0]['收盘']))
            if not price.is_finite() or price <= 0:
                raise MarketDataError(f'{symbol}: 收盘价无效')
            prices[symbol] = price
        return prices

    def trading_calendar(self):
        """获取完整交易日历，沿用行情请求超时和间隔配置。"""
        return sorted(set(self._request(ak.calendar_trade_date_hist_sina)['trade_date']))

    @staticmethod
    def _is_trading_session(as_of: datetime):
        """判断是否处于 A 股连续交易时段。"""
        return time(9, 30) <= as_of.time() <= time(11, 30) or time(13) <= as_of.time() < time(15)

    @staticmethod
    def _check_trading_session(as_of: datetime):
        if not MarketProvider._is_trading_session(as_of):
            raise MarketDataError('行情获取期间已离开 A 股连续交易时段')

    def _load_daily_closes(self, symbols, trading_days, before_day):
        """读取 before_day 之前的前复权收盘价，校验交易日和价格。"""
        start_date = (before_day - timedelta(days=self.lookback_days)).isoformat()
        end_date = (before_day - timedelta(days=1)).isoformat()
        histories = {}
        for symbol in symbols:
            daily = self._request(self.get_daily_closes,
                symbol=symbol, start=start_date, end=end_date, adjust='qfq', required_days=trading_days)
            self.logger.info('历史日线获取结果 symbol=%s rows=%d window_days=%d', symbol, len(daily), self.lookback_days)
            if daily.empty:
                raise MarketDataError(f'{symbol}: 无历史行情')
            dates = pd.to_datetime(daily['日期'], errors='raise').dt.date
            daily = daily.assign(day=dates).sort_values('day')
            daily = daily[daily['day'] < before_day]
            if daily['day'].duplicated().any():
                raise MarketDataError(f'{symbol}: 历史日期重复')
            daily = daily.tail(len(trading_days))
            if daily['day'].tolist() != trading_days:
                raise MarketDataError(f'{symbol}: 历史行情不足或缺少交易日，请检查回溯窗口及数据源')
            self.logger.info('历史日线校验成功 symbol=%s used=%d start=%s end=%s', symbol, len(daily), daily.iloc[0]['day'], daily.iloc[-1]['day'])
            history = tuple(DailyClose(row.day, float(row.收盘)) for row in daily.itertuples())
            if any(not math.isfinite(bar.close) or bar.close <= 0 for bar in history):
                raise MarketDataError(f'{symbol}: 价格无效')
            histories[symbol] = history
        return histories

    def _load_close_snapshots(self, symbols, history_points, as_of, calendar):
        """使用最近已完成交易日收盘数据，按当前日期模拟执行。"""
        if not calendar or not calendar[0] <= as_of.date() <= calendar[-1]:
            raise MarketDataError('交易日历未覆盖当前日期')
        completed_days = [day for day in calendar if day < as_of.date()
                          or day == as_of.date() and as_of.time() >= time(15)]
        if len(completed_days) <= history_points:
            raise MarketDataError('交易日历历史不足')
        close_day = completed_days[-1]
        trading_days = completed_days[-history_points - 1:-1]
        prices = self.closing_prices(symbols, close_day)
        histories = self._load_daily_closes(symbols, trading_days, close_day)
        finished_at = datetime.now(SHANGHAI)
        if finished_at.date() != as_of.date():
            raise MarketDataError('行情获取跨日期，停止计算')
        quoted_at = datetime.combine(close_day, time(15), SHANGHAI)
        self.logger.info('非交易时段使用收盘价：执行时间=%s 行情日期=%s', as_of.isoformat(), close_day)
        snapshots = {symbol: MarketData(symbol, quoted_at, float(prices[symbol]), histories[symbol])
                     for symbol in symbols}
        return finished_at, snapshots

    def _request(self, function, **kwargs):
        """按配置限制请求间隔。"""
        remaining = self.interval - (clock.monotonic() - self.last_request)
        if remaining > 0:
            clock.sleep(remaining)
        self.last_request = clock.monotonic()
        return function(**kwargs)
