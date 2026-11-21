"""每日收益总结消息。"""
from decimal import Decimal


class DailyAccountReport:
    """消息示例（标题及正文）：

    etf_rotation｜每日收益总结
    日期：2026-11-20
    账户：etf_rotation_pap
    估值时间：2026-11-20T15:00:00+08:00
    总资产：102,800.00 元
    可用现金：12,350.00 元
    比较基准：2026-11-19
    当日盈亏：+800.00 元
    当日收益率：+0.78%
    累计盈亏：+2,800.00 元
    累计收益率：+2.80%
    持仓：510300.XSHG × 20000 份
    今日交易：买入 1 笔，卖出 0 笔
    """

    @staticmethod
    def build(strategy_id, instance_id, summary):
        title = f'{strategy_id}｜每日收益总结'
        lines = [f"日期：{summary['date']}", f'账户：{instance_id}']
        if summary.get('notice'):
            lines.append(summary['notice'])
            return title, '\n'.join(lines)
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
        if summary.get('execution_note'):
            lines.append(summary['execution_note'])
        return title, '\n'.join(lines)
