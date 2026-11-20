"""AKShare适配：东财前复权日线、批量当前报价、上海市场交易日历。"""
import akshare as ak
import math
import pandas as pd
import re
import requests
import time as clock
from .models import DailyClose, MarketSnapshot
from akshare.utils.context import config as ak_config
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo('Asia/Shanghai')


class MarketDataError(RuntimeError):
    """行情缺失、过期或无法验证时停止计算。"""


class AKShareMarket:
    def __init__(self, settings: dict):
        if settings['provider'] != 'akshare_eastmoney':
            raise ValueError('当前仅支持akshare_eastmoney行情源')
        self.max_age = int(settings['max_quote_age_seconds'])
        self.lookback_days = int(settings['history_lookback_days'])
        timeout = float(settings['request_timeout_seconds'])
        self.timeout = timeout
        self.interval = float(settings['request_interval_seconds'])
        self.last_request = 0.0
        if not math.isfinite(self.interval) or self.interval < 0:
            raise ValueError('请求间隔必须为非负有限数')
        if not math.isfinite(timeout) or min(self.max_age, self.lookback_days, timeout) <= 0:
            raise ValueError('行情超时、时效和回溯天数必须为正数')
        ak_config.timeout = timeout

    def _request(self, function, **kwargs):
        remaining = self.interval - (clock.monotonic() - self.last_request)
        if remaining > 0:
            clock.sleep(remaining)
        self.last_request = clock.monotonic()
        return function(**kwargs)

    def _quotes(self, symbols: tuple[str, ...]) -> dict:
        secids = {}
        for symbol in symbols:
            match = re.fullmatch(r'(\d{6})\.(XSHG|XSHE)', symbol)
            if not match:
                raise MarketDataError(f'不支持的证券代码: {symbol}')
            market_id = '1' if match.group(2) == 'XSHG' else '0'
            secids[f'{market_id}.{match.group(1)}'] = symbol
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
        if not (time(9, 30) <= as_of.time() <= time(11, 30) or time(13) <= as_of.time() <= time(15)):
            raise MarketDataError('当前不在A股连续交易时段，策略执行时间为14:30')

    def load(self, symbols: tuple[str, ...], history_points: int) -> tuple[datetime, dict[str, MarketSnapshot]]:
        started = datetime.now(SHANGHAI)
        self._check_session(started)
        try:
            calendar = sorted(set(self._request(ak.calendar_trade_date_hist_sina)['trade_date']))
            if not calendar or not calendar[0] <= started.date() <= calendar[-1]:
                raise MarketDataError('交易日历未覆盖当前日期')
            if started.date() not in calendar:
                raise MarketDataError(f'{started.date()}: 非A股交易日')
            expected = [day for day in calendar if day < started.date()][-history_points:]
            if len(expected) != history_points:
                raise MarketDataError('交易日历历史不足')
            histories = {}
            for symbol in symbols:
                if not re.fullmatch(r'\d{6}\.(XSHG|XSHE)', symbol):
                    raise MarketDataError(f'不支持的证券代码: {symbol}')
                start = started.date() - timedelta(days=self.lookback_days)
                daily = self._request(ak.fund_etf_hist_em,
                    symbol=symbol.split('.')[0], period='daily', start_date=start.strftime('%Y%m%d'),
                    end_date=(started.date() - timedelta(days=1)).strftime('%Y%m%d'), adjust='qfq')
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
                histories[symbol] = tuple(DailyClose(row.day, float(row.收盘)) for row in daily.itertuples())
            # 历史数据准备完毕后再批量取价，避免各标的分别请求产生较大时间差。
            quotes = self._quotes(symbols)
            as_of = datetime.now(SHANGHAI)
            self._check_session(as_of)
            if as_of.date() != started.date():
                raise MarketDataError('行情获取跨交易日，停止计算')
            snapshots = {}
            for symbol in symbols:
                price, stamp = quotes[symbol]
                age = (as_of - stamp).total_seconds()
                if stamp.date() != as_of.date() or not 0 <= age <= self.max_age:
                    raise MarketDataError(f'{symbol}: 报价过期或来自未来，更新时间为{stamp.isoformat()}')
                history = histories[symbol]
                if any(not math.isfinite(p) or p <= 0 for p in [price, *(bar.close for bar in history)]):
                    raise MarketDataError(f'{symbol}: 价格无效')
                snapshots[symbol] = MarketSnapshot(symbol, stamp, price, history)
            return as_of, snapshots
        except MarketDataError:
            raise
        except Exception as exc:
            raise MarketDataError(f'行情获取失败: {exc}') from exc
