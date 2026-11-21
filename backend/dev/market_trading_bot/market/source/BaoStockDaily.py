"""在独立进程中获取 BaoStock 日线，避免网络故障阻塞调度线程。"""
import baostock as bs
import contextlib
import io
import json
import pandas as pd
import subprocess
import sys


class BaoStockDaily:
    def __init__(self, timeout):
        self.timeout = timeout

    def get_daily_closes(self, symbol, start, end, adjust):
        result = subprocess.run(
            [sys.executable, '-c',
             'import json, sys; from market_trading_bot.market.source.BaoStockDaily import BaoStockDaily; '
             'print(json.dumps(BaoStockDaily._fetch_daily_closes(*json.loads(sys.stdin.read()))))'],
            input=json.dumps([symbol, start, end, adjust]), text=True, capture_output=True,
            timeout=self.timeout, check=True)
        return pd.DataFrame(json.loads(result.stdout), columns=['日期', '收盘'])

    @staticmethod
    def _fetch_daily_closes(symbol, start, end, adjust):
        """读取指定证券的日线；调用方负责限制整个进程的运行时间。"""
        market = 'sh' if symbol.endswith('.XSHG') else 'sz'
        with contextlib.redirect_stdout(io.StringIO()):
            login = bs.login()
            if login.error_code != '0':
                raise RuntimeError(f'BaoStock 登录失败: {login.error_msg}')
            try:
                result = bs.query_history_k_data_plus(
                    f'{market}.{symbol[:6]}', 'date,close', start_date=start, end_date=end,
                    frequency='d', adjustflag='2' if adjust == 'qfq' else '3')
                if result.error_code != '0':
                    raise RuntimeError(f'BaoStock 查询失败: {result.error_msg}')
                rows = []
                while result.next():
                    rows.append(result.get_row_data())
                if result.error_code != '0':
                    raise RuntimeError(f'BaoStock 分页失败: {result.error_msg}')
                return rows
            finally:
                bs.logout()
