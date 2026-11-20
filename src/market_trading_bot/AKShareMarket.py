"""AKShare适配：东财前复权日线、未复权分钟价格、上海市场交易日历。"""
import akshare as ak
import math
import pandas as pd
import re
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

    def load(self, symbols: tuple[str, ...], as_of: datetime, history_points: int) -> dict[str, MarketSnapshot]:
        as_of = as_of.astimezone(SHANGHAI)
        if as_of > datetime.now(SHANGHAI):
            raise MarketDataError('不能获取未来时刻行情')
        if not (time(9, 31) <= as_of.time() <= time(11, 30) or time(13, 1) <= as_of.time() <= time(15)):
            raise MarketDataError('请指定连续交易时段内的分钟时刻，策略执行时间为14:30')
        try:
            calendar = sorted(set(self._request(ak.calendar_trade_date_hist_sina)['trade_date']))
            if not calendar or not calendar[0] <= as_of.date() <= calendar[-1]:
                raise MarketDataError('交易日历未覆盖请求日期')
            if as_of.date() not in calendar:
                raise MarketDataError(f'{as_of.date()}: 非A股交易日')
            expected = [day for day in calendar if day < as_of.date()][-history_points:]
            if len(expected) != history_points:
                raise MarketDataError('交易日历历史不足')
            snapshots = {}
            for symbol in symbols:
                match = re.fullmatch(r'(\d{6})\.(XSHG|XSHE)', symbol)
                if not match:
                    raise MarketDataError(f'不支持的证券代码: {symbol}')
                code = match.group(1)
                start = as_of.date() - timedelta(days=self.lookback_days)
                daily = self._request(ak.fund_etf_hist_em,
                    symbol=code, period='daily', start_date=start.strftime('%Y%m%d'),
                    end_date=(as_of.date() - timedelta(days=1)).strftime('%Y%m%d'), adjust='qfq')
                if daily.empty:
                    raise MarketDataError(f'{symbol}: 无历史行情')
                dates = pd.to_datetime(daily['日期'], errors='raise').dt.date
                daily = daily.assign(day=dates).sort_values('day')
                daily = daily[daily['day'] < as_of.date()]
                if daily['day'].duplicated().any():
                    raise MarketDataError(f'{symbol}: 历史日期重复')
                daily = daily.tail(history_points)
                if daily['day'].tolist() != expected:
                    raise MarketDataError(f'{symbol}: 历史行情不足或缺少交易日，禁止跳过缺失数据')
                minute = self._request(ak.fund_etf_hist_min_em,
                    symbol=code, period='1', adjust='',
                    start_date=f'{as_of.date()} 09:30:00', end_date=as_of.strftime('%Y-%m-%d %H:%M:%S'))
                if minute.empty:
                    raise MarketDataError(f'{symbol}: 无当日分钟行情（接口仅提供近期数据）')
                stamps = pd.to_datetime(minute['时间'], errors='raise').dt.tz_localize(SHANGHAI)
                minute = minute.assign(stamp=stamps)
                minute = minute[(minute['stamp'].dt.date == as_of.date()) & (minute['stamp'] <= as_of)].sort_values('stamp')
                if minute.empty or minute['stamp'].duplicated().any():
                    raise MarketDataError(f'{symbol}: 分钟行情为空或时间重复')
                latest = minute.iloc[-1]
                stamp = latest['stamp'].to_pydatetime()
                if (as_of - stamp).total_seconds() > self.max_age:
                    raise MarketDataError(f'{symbol}: 行情过期，最新时间为{stamp.isoformat()}')
                history = tuple(DailyClose(row.day, float(row.收盘)) for row in daily.itertuples())
                price = float(latest['收盘'])
                if any(not math.isfinite(p) or p <= 0 for p in [price, *(bar.close for bar in history)]):
                    raise MarketDataError(f'{symbol}: 价格无效')
                snapshots[symbol] = MarketSnapshot(symbol, stamp, price, history)
            return snapshots
        except MarketDataError:
            raise
        except Exception as exc:
            raise MarketDataError(f'行情获取失败: {exc}') from exc
