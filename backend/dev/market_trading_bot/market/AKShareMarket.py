"""AKShare适配：东财前复权日线、批量当前报价、上海市场交易日历。"""
import akshare as ak
import logging
import math
import pandas as pd
import re
import requests
import time as clock
from akshare.utils.context import config as ak_config
from datetime import datetime, time, timedelta
from decimal import Decimal
from market_trading_bot.market.MarketData import DailyClose, MarketData
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo('Asia/Shanghai')


class MarketDataError(RuntimeError):
    """行情缺失、过期或无法验证时停止计算。"""


class AKShareMarket:
    """获取前复权历史收盘价和未复权盘中报价，并校验可用性。"""

    def __init__(self, settings: dict, logger: logging.Logger | None = None, *, test_mode=False):
        """读取行情配置并设置 AKShare 请求超时。"""
        self.logger = logger or logging.getLogger(__name__)
        self.test_mode = test_mode
        if settings['provider'] != 'akshare_eastmoney':
            raise ValueError('当前仅支持akshare_eastmoney行情源')
        self.max_quote_age = int(settings['max_quote_age_seconds'])
        self.lookback_days = int(settings['history_lookback_days'])
        self.timeout = float(settings['request_timeout_seconds'])
        self.interval = float(settings['request_interval_seconds'])
        self.last_request = 0.0
        if not math.isfinite(self.interval) or self.interval < 0:
            raise ValueError('请求间隔必须为非负有限数')
        if not math.isfinite(self.timeout) or min(self.max_quote_age, self.lookback_days, self.timeout) <= 0:
            raise ValueError('行情超时、时效和回溯天数必须为正数')
        ak_config.timeout = self.timeout

    def load(self, symbols: tuple[str, ...], history_points: int) -> tuple[datetime, dict[str, MarketData]]:
        """加载连续历史收盘价及当日报价，校验交易时段、日期和价格。"""
        started_at = datetime.now(SHANGHAI)
        self.logger.info('行情获取开始 provider=akshare_eastmoney symbols=%s', ','.join(symbols))
        try:
            if self.test_mode:
                return self._load_test(symbols, history_points, started_at)
            self._check_session(started_at)
            secids = {}
            for symbol in symbols:
                match = re.fullmatch(r'(\d{6})\.(XSHG|XSHE)', symbol)
                if not match:
                    raise MarketDataError(f'不支持的证券代码: {symbol}')
                market_id = '1' if match.group(2) == 'XSHG' else '0'
                secids[f'{market_id}.{match.group(1)}'] = symbol
            calendar = self.trading_calendar()
            if not calendar or not calendar[0] <= started_at.date() <= calendar[-1]:
                raise MarketDataError('交易日历未覆盖当前日期')
            if started_at.date() not in calendar:
                raise MarketDataError(f'{started_at.date()}: 非A股交易日')
            self.logger.info('交易日历获取成功 date=%s coverage=%s~%s', started_at.date(), calendar[0], calendar[-1])
            expected = [day for day in calendar if day < started_at.date()][-history_points:]
            if len(expected) != history_points:
                raise MarketDataError('交易日历历史不足')
            histories = self._histories(symbols, expected, started_at.date())
            # 历史加载后批量取价，减少报价时间差。
            self.logger.info('批量当前报价请求开始 count=%d', len(symbols))
            quotes = self._quotes(secids)
            as_of = datetime.now(SHANGHAI)
            self._check_session(as_of)
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

    def _load_test(self, symbols, history_points, as_of):
        """使用最近已完成交易日收盘数据，按当前时间模拟执行。"""
        calendar = self.trading_calendar()
        if not calendar or not calendar[0] <= as_of.date() <= calendar[-1]:
            raise MarketDataError('交易日历未覆盖当前日期')
        completed = [day for day in calendar if day < as_of.date()
                     or day == as_of.date() and as_of.time() >= time(15)]
        if len(completed) <= history_points:
            raise MarketDataError('交易日历历史不足')
        close_day = completed[-1]
        expected = completed[-history_points - 1:-1]
        prices = self.closing_prices(symbols, close_day)
        histories = self._histories(symbols, expected, close_day)
        finished_at = datetime.now(SHANGHAI)
        if finished_at.date() != as_of.date():
            raise MarketDataError('行情获取跨日期，停止测试')
        quoted_at = datetime.combine(close_day, time(15), SHANGHAI)
        self.logger.info('链路测试模式：执行时间=%s 行情日期=%s，仅用于模拟验证', as_of.isoformat(), close_day)
        snapshots = {symbol: MarketData(symbol, quoted_at, float(prices[symbol]), histories[symbol])
                     for symbol in symbols}
        return finished_at, snapshots

    def closing_prices(self, symbols, day):
        """查询 day 当天的不复权收盘价，返回 symbol 到 Decimal 的映射。"""
        prices = {}
        for symbol in sorted(symbols):
            if not re.fullmatch(r'\d{6}\.(XSHG|XSHE)', symbol):
                raise MarketDataError(f'不支持的证券代码: {symbol}')
            daily = self._request(ak.fund_etf_hist_em, symbol=symbol.split('.')[0], period='daily',
                                  start_date=day.strftime('%Y%m%d'), end_date=day.strftime('%Y%m%d'), adjust='')
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

    def _quotes(self, secids: dict[str, str]) -> dict[str, tuple[float, datetime]]:
        """批量获取未复权价格和报价时间，每个标的须恰好返回一条。"""
        response = self._request(
            requests.get, url='https://push2.eastmoney.com/api/qt/ulist.np/get',
            params={'secids': ','.join(secids), 'fields': 'f2,f12,f13,f124', 'fltt': '2'},
            timeout=self.timeout)
        response.raise_for_status()
        payload = response.json()
        if payload.get('rc') != 0 or not payload.get('data'):
            raise MarketDataError('当前报价接口返回错误或空数据')
        quotes = {}
        for item in payload['data']['diff']:
            key = f"{item['f13']}.{item['f12']}"
            if key not in secids or key in quotes:
                raise MarketDataError('当前报价包含错误或重复证券代码')
            quotes[key] = (float(item['f2']), datetime.fromtimestamp(int(item['f124']), SHANGHAI))
        if set(quotes) != set(secids):
            raise MarketDataError('当前报价缺少标的')
        return {secids[key]: value for key, value in quotes.items()}

    @staticmethod
    def _check_session(as_of: datetime):
        """仅允许 A 股连续交易时段。"""
        if not (time(9, 30) <= as_of.time() <= time(11, 30) or time(13) <= as_of.time() <= time(15)):
            raise MarketDataError('当前不在A股连续交易时段，策略执行时间为14:30')

    def _histories(self, symbols, expected, day):
        """读取 day 之前的前复权历史，校验连续交易日和价格。"""
        start_date = (day - timedelta(days=self.lookback_days)).strftime('%Y%m%d')
        end_date = (day - timedelta(days=1)).strftime('%Y%m%d')
        histories = {}
        for symbol in symbols:
            daily = self._request(ak.fund_etf_hist_em,
                symbol=symbol.split('.')[0], period='daily', start_date=start_date, end_date=end_date, adjust='qfq')
            self.logger.info('历史日线获取结果 symbol=%s rows=%d window_days=%d', symbol, len(daily), self.lookback_days)
            if daily.empty:
                raise MarketDataError(f'{symbol}: 无历史行情')
            dates = pd.to_datetime(daily['日期'], errors='raise').dt.date
            daily = daily.assign(day=dates).sort_values('day')
            daily = daily[daily['day'] < day]
            if daily['day'].duplicated().any():
                raise MarketDataError(f'{symbol}: 历史日期重复')
            daily = daily.tail(len(expected))
            if daily['day'].tolist() != expected:
                raise MarketDataError(f'{symbol}: 历史行情不足或缺少交易日，请检查回溯窗口及数据源')
            self.logger.info('历史日线校验成功 symbol=%s used=%d start=%s end=%s', symbol, len(daily), daily.iloc[0]['day'], daily.iloc[-1]['day'])
            history = tuple(DailyClose(row.day, float(row.收盘)) for row in daily.itertuples())
            if any(not math.isfinite(bar.close) or bar.close <= 0 for bar in history):
                raise MarketDataError(f'{symbol}: 价格无效')
            histories[symbol] = history
        return histories

    def _request(self, function, **kwargs):
        """按配置限制请求间隔。"""
        remaining = self.interval - (clock.monotonic() - self.last_request)
        if remaining > 0:
            clock.sleep(remaining)
        self.last_request = clock.monotonic()
        return function(**kwargs)
