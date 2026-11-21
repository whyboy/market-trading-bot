"""每日收益总结消息。"""
from decimal import Decimal
from itertools import groupby


class DailyAccountReport:
    """消息示例（标题及正文）：

    每日收益总结｜2026-11-20
    日期：2026-11-20

    策略：etf_rotation

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

    策略：other_strategy

    账户：second_account
    收盘估值失败或交易任务仍在运行，未生成收益，请查看日志。
    """

    @staticmethod
    def build(day, summaries):
        """按策略分组，返回标题和纯文本正文。"""
        title = f'每日收益总结｜{day.isoformat()}'
        lines = [f'日期：{day.isoformat()}']
        if not summaries:
            lines.append('暂无启用的模拟账户。')
        for strategy_id, accounts in groupby(sorted(summaries, key=lambda item: item['strategy_id']),
                                            key=lambda item: item['strategy_id']):
            lines.extend(['', f'策略：{strategy_id}'])
            for account in accounts:
                lines.extend(['', *DailyAccountReport._account_lines(account['instance_id'], account['summary'])])
        return title, '\n'.join(lines)

    @staticmethod
    def _account_lines(instance_id, summary):
        """生成账户正文；异常显示提示，休市日省略当日收益。"""
        lines = [f'账户：{instance_id}']
        if summary.get('notice'):
            lines.append(summary['notice'])
            return lines
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
        return lines
