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
from market_trading_bot.data.MarketSnapshot import DailyClose, MarketSnapshot
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo('Asia/Shanghai')


class MarketDataError(RuntimeError):
    """行情缺失、过期或无法验证时停止计算。"""


class AKShareMarket:
    """获取前复权历史收盘价和未复权盘中报价，并校验可用性。"""

    def __init__(self, settings: dict, logger: logging.Logger | None = None):
        """读取行情配置，并设置 AKShare 全局请求超时。

        :param settings: 行情源、报价时效、回溯天数、超时及请求间隔配置。
        :param logger: 运行日志；省略时使用模块日志。
        :raises ValueError: 行情源不受支持或请求参数无效。
        """
        self.logger = logger or logging.getLogger(__name__)
        if settings['provider'] != 'akshare_eastmoney':
            raise ValueError('当前仅支持akshare_eastmoney行情源')
        self.max_age = int(settings['max_quote_age_seconds'])
        self.lookback_days = int(settings['history_lookback_days'])
        self.timeout = float(settings['request_timeout_seconds'])
        self.interval = float(settings['request_interval_seconds'])
        self.last_request = 0.0
        if not math.isfinite(self.interval) or self.interval < 0:
            raise ValueError('请求间隔必须为非负有限数')
        if not math.isfinite(self.timeout) or min(self.max_age, self.lookback_days, self.timeout) <= 0:
            raise ValueError('行情超时、时效和回溯天数必须为正数')
        ak_config.timeout = self.timeout

    def load(self, symbols: tuple[str, ...], history_points: int) -> tuple[datetime, dict[str, MarketSnapshot]]:
        """加载连续历史交易日收盘价，再批量获取当日盘中报价。

        :param symbols: 待获取行情的标的代码。
        :param history_points: 每个标的所需的历史收盘价数量，不含当日。
        :return: 行情校验时刻（北京时间）及按标的索引的行情快照。
        :raises MarketDataError: 非交易日/时段、历史不完整、报价过期、价格无效或接口失败。
        """
        started = datetime.now(SHANGHAI)
        self.logger.info('行情获取开始 provider=akshare_eastmoney symbols=%s', ','.join(symbols))
        try:
            self._check_session(started)
            secids = {}
            for symbol in symbols:
                match = re.fullmatch(r'(\d{6})\.(XSHG|XSHE)', symbol)
                if not match:
                    raise MarketDataError(f'不支持的证券代码: {symbol}')
                market_id = '1' if match.group(2) == 'XSHG' else '0'
                secids[f'{market_id}.{match.group(1)}'] = symbol
            calendar = self.trading_calendar()
            if not calendar or not calendar[0] <= started.date() <= calendar[-1]:
                raise MarketDataError('交易日历未覆盖当前日期')
            if started.date() not in calendar:
                raise MarketDataError(f'{started.date()}: 非A股交易日')
            self.logger.info('交易日历获取成功 date=%s coverage=%s~%s', started.date(), calendar[0], calendar[-1])
            expected = [day for day in calendar if day < started.date()][-history_points:]
            if len(expected) != history_points:
                raise MarketDataError('交易日历历史不足')
            start = (started.date() - timedelta(days=self.lookback_days)).strftime('%Y%m%d')
            end = (started.date() - timedelta(days=1)).strftime('%Y%m%d')
            histories = {}
            for symbol in symbols:
                daily = self._request(ak.fund_etf_hist_em,
                    symbol=symbol.split('.')[0], period='daily', start_date=start, end_date=end, adjust='qfq')
                self.logger.info('历史日线获取结果 symbol=%s rows=%d window_days=%d', symbol, len(daily), self.lookback_days)
                if daily.empty:
                    raise MarketDataError(f'{symbol}: 无历史行情')
                dates = pd.to_datetime(daily['日期'], errors='raise').dt.date
                daily = daily.assign(day=dates).sort_values('day')
                daily = daily[daily['day'] < started.date()]
                if daily['day'].duplicated().any():
                    raise MarketDataError(f'{symbol}: 历史日期重复')
                daily = daily.tail(history_points)
                if daily['day'].tolist() != expected:
                    raise MarketDataError(f'{symbol}: 历史行情不足或缺少交易日，请检查回溯窗口及数据源')
                self.logger.info('历史日线校验成功 symbol=%s used=%d start=%s end=%s', symbol, len(daily), daily.iloc[0]['day'], daily.iloc[-1]['day'])
                histories[symbol] = tuple(DailyClose(row.day, float(row.收盘)) for row in daily.itertuples())
            # 历史数据准备完毕后再批量取价，避免各标的分别请求产生较大时间差。
            self.logger.info('批量当前报价请求开始 count=%d', len(symbols))
            quotes = self._quotes(secids)
            as_of = datetime.now(SHANGHAI)
            self._check_session(as_of)
            if as_of.date() != started.date():
                raise MarketDataError('行情获取跨交易日，停止计算')
            snapshots = {}
            for symbol in symbols:
                price, stamp = quotes[symbol]
                self.logger.info('当前报价获取结果 symbol=%s price=%s quote_time=%s', symbol, price, stamp.isoformat())
                age = (as_of - stamp).total_seconds()
                if stamp.date() != as_of.date() or not 0 <= age <= self.max_age:
                    raise MarketDataError(f'{symbol}: 报价过期或来自未来，更新时间为{stamp.isoformat()}')
                history = histories[symbol]
                if any(not math.isfinite(p) or p <= 0 for p in [price, *(bar.close for bar in history)]):
                    raise MarketDataError(f'{symbol}: 价格无效')
                snapshots[symbol] = MarketSnapshot(symbol, stamp, price, history)
            self.logger.info('行情获取成功 count=%d as_of=%s', len(snapshots), as_of.isoformat())
            return as_of, snapshots
        except MarketDataError as exc:
            self.logger.error('行情获取失败: %s', exc)
            raise
        except Exception as exc:
            self.logger.error('行情接口请求或解析失败: %s', exc)
            raise MarketDataError(f'行情获取失败: {exc}') from exc

    def closing_prices(self, symbols, day):
        """读取指定交易日不复权收盘价；拒绝缺失、重复、过期或非法价格。"""
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
        """批量获取当前报价，要求每个标的恰好返回一条。

        :param secids: 东方财富市场代码到项目标的代码的映射。
        :return: 标的到（未复权价格、北京时间报价时间）的映射。
        :raises MarketDataError: 返回数据错误、缺失或重复。
        """
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
        """检查北京时间是否处于连续交易时段；不满足时抛出 MarketDataError。"""
        if not (time(9, 30) <= as_of.time() <= time(11, 30) or time(13) <= as_of.time() <= time(15)):
            raise MarketDataError('当前不在A股连续交易时段，策略执行时间为14:30')

    def _request(self, function, **kwargs):
        """按配置限制请求开始时间的间隔，透传参数、结果和异常。"""
        remaining = self.interval - (clock.monotonic() - self.last_request)
        if remaining > 0:
            clock.sleep(remaining)
        self.last_request = clock.monotonic()
        return function(**kwargs)
