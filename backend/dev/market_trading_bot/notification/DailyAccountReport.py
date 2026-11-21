"""将每日估值汇总格式化为消息；不获取行情、不计算收益、不调度发送。"""
from decimal import Decimal


class DailyAccountReport:
    @staticmethod
    def build(strategy_id, instance_id, summary):
        """summary 提供资产、现金、累计收益、持仓和估值时间。

        交易日另提供比较基准日期、当日盈亏及收益率、买卖笔数。
        收益数据由收盘估值流程提供，本类仅格式化；休市日不展示旧的当日收益。
        """
        title = f'{strategy_id}｜每日收益总结'
        lines = [f"日期：{summary['date']}", f'账户：{instance_id}']
        if not summary['is_trading_day']:
            lines.append('今日休市，以下为最近一次有效估值。')
        lines.extend([f"估值时间：{summary['as_of']}",
                      f"总资产：{Decimal(summary['equity']):,.2f} 元",
                      f"可用现金：{Decimal(summary['cash']):,.2f} 元"])
        if summary['is_trading_day']:
            lines.extend([f"比较基准：{summary['comparison_date']}",
                          f"当日盈亏：{Decimal(summary['daily_pnl']):+,.2f} 元",
                          f"当日收益率：{Decimal(summary['daily_return_pct']):+.2f}%"])
        lines.extend([f"累计盈亏：{Decimal(summary['cumulative_pnl']):+,.2f} 元",
                      f"累计收益率：{Decimal(summary['cumulative_return_pct']):+.2f}%"])
        lines.append('持仓：' + ('；'.join(f"{symbol} × {position['quantity']} 份"
                                      for symbol, position in sorted(summary['positions'].items())) or '空仓'))
        if summary['is_trading_day']:
            lines.append(f"今日交易：买入 {summary['buy_count']} 笔，卖出 {summary['sell_count']} 笔")
        return title, '\n'.join(lines)
