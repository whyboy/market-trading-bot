"""新浪、腾讯 HTTP 行情接口。"""
import akshare as ak
import pandas as pd
import re
import requests
from datetime import datetime
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo('Asia/Shanghai')


class HttpMarket:
    def __init__(self, timeout):
        self.timeout = timeout

    @staticmethod
    def code(symbol):
        match = re.fullmatch(r'(\d{6})\.(XSHG|XSHE)', symbol)
        if not match:
            raise ValueError(f'不支持的证券代码: {symbol}')
        return ('sh' if match.group(2) == 'XSHG' else 'sz') + match.group(1)

    def quotes(self, symbols, provider):
        codes = {self.code(symbol): symbol for symbol in symbols}
        if len(codes) != len(symbols):
            raise ValueError('证券代码重复')
        url = ('https://hq.sinajs.cn/list=' if provider == 'sina' else 'https://qt.gtimg.cn/q=') + ','.join(codes)
        response = requests.get(url, headers={'Referer': 'https://finance.sina.com.cn/'}, timeout=self.timeout)
        response.raise_for_status()
        response.encoding = 'gbk'
        quotes = {}
        pattern = r'\s*var hq_str_((?:sh|sz)\d{6})="([^"]*)";\s*' if provider == 'sina' else r'\s*v_((?:sh|sz)\d{6})="([^"]*)";\s*'
        for line in response.text.splitlines():
            match = re.fullmatch(pattern, line)
            if not match:
                raise ValueError(f'{provider}: 报价格式错误')
            code, payload = match.groups()
            if code not in codes or code in quotes:
                raise ValueError(f'{provider}: 报价包含错误或重复证券代码')
            fields = payload.split(',' if provider == 'sina' else '~')
            if len(fields) < (32 if provider == 'sina' else 31):
                raise ValueError(f'{provider}: 报价缺失字段')
            if provider == 'sina':
                quoted_at = datetime.strptime(f'{fields[30]} {fields[31]}', '%Y-%m-%d %H:%M:%S')
            else:
                if fields[2] != code[2:]:
                    raise ValueError('腾讯报价证券代码不匹配')
                quoted_at = datetime.strptime(fields[30], '%Y%m%d%H%M%S')
            quotes[code] = (float(fields[3]), quoted_at.replace(tzinfo=SHANGHAI))
        if set(quotes) != set(codes):
            raise ValueError(f'{provider}: 报价缺少标的')
        return {codes[key]: value for key, value in quotes.items()}

    def daily(self, symbol, start, end, adjust, provider):
        code = self.code(symbol)
        if provider == 'sina':
            if adjust:
                raise ValueError('新浪日线不支持确认前复权')
            frame = ak.fund_etf_hist_sina(symbol=code)
            if frame.empty:
                return pd.DataFrame(columns=['日期', '收盘'])
            dates = pd.to_datetime(frame['date'], errors='raise')
            return frame.loc[(dates >= pd.Timestamp(start)) & (dates <= pd.Timestamp(end)), ['date', 'close']].rename(columns={'date': '日期', 'close': '收盘'})
        response = requests.get('https://web.ifzq.gtimg.cn/appstock/app/fqkline/get',
                                params={'param': f'{code},day,{start},{end},640,{adjust}'}, timeout=self.timeout)
        response.raise_for_status()
        data = response.json()
        if data.get('code') != 0:
            raise ValueError('腾讯日线接口返回错误')
        rows = data['data'][code]['qfqday' if adjust == 'qfq' else 'day']
        return pd.DataFrame([(row[0], row[2]) for row in rows], columns=['日期', '收盘'])
