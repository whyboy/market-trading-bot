"""统一行情源与失败切换。"""
import logging
import math
import pandas as pd
from market_trading_bot.market.BaoStockDaily import BaoStockDaily
from market_trading_bot.market.HttpMarket import HttpMarket


class MarketSource:
    def __init__(self, timeout, logger=None):
        self.logger = logger or logging.getLogger(__name__)
        self.baostock = BaoStockDaily(timeout)
        self.http = HttpMarket(timeout)

    def quotes(self, symbols, now, max_age):
        errors = []
        for provider in ('sina', 'tencent'):
            try:
                quotes = self.http.quotes(symbols, provider)
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

    def daily(self, symbol, start, end, adjust, required_days=None):
        if adjust not in ('', 'qfq'):
            raise ValueError('不支持的复权类型')
        sources = [('baostock', lambda: self.baostock.daily(symbol, start, end, adjust)),
                   ('tencent', lambda: self.http.daily(symbol, start, end, adjust, 'tencent'))]
        if not adjust:
            sources.append(('sina', lambda: self.http.daily(symbol, start, end, adjust, 'sina')))
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
