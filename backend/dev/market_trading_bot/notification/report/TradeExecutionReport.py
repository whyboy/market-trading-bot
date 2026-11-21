"""执行时估值报告；收盘估值由后续日报功能提供。"""
from decimal import Decimal


class TradeExecutionReport:
    @staticmethod
    def build(task):
        result = task.result
        title = f"{task.strategy_id}｜模拟交易{'完成' if result['success'] else '失败'}"
        lines = [f'账户：{task.instance_id}', f'计划时间：{task.scheduled_at.isoformat()}',
                 f"结束时间：{result['finished_at']}"]
        if not result['success']:
            # 原始异常可能包含请求 URL 或配置，仅在服务器日志中保留。
            lines.append('执行失败，请查看服务器策略日志；本报告不展示旧资产或收益。')
            return title, '\n'.join(lines)
        output = task.output
        lines.extend([f"估值时间：{output['signal']['as_of']}",
                      f"总资产：{Decimal(output['equity']):,.2f} 元",
                      f"可用现金：{Decimal(output['cash']):,.2f} 元"])
        account = task.account_repository.latest(task.account_repository.directory.parent, task.instance_id)
        if account.get('initial_cash'):
            initial = Decimal(account['initial_cash'])
            equity = Decimal(output['equity'])
            lines.extend([f'累计盈亏：{equity - initial:+,.2f} 元',
                          f'累计收益率：{(equity / initial - 1) * 100:+.2f}%'])
        if output['fills']:
            lines.append('成交：')
            for fill in output['fills']:
                side = '买入' if fill['side'] == 'buy' else '卖出'
                lines.append(f"{side} {fill['symbol']} × {fill['quantity']} 份，价格 {fill['price']}，手续费 {fill['commission']} 元")
        else:
            lines.append('今日无调仓。')
        lines.append('持仓：' + ('；'.join(f"{symbol} × {position['quantity']} 份"
                                      for symbol, position in sorted(output['positions'].items())) or '空仓'))
        lines.append('以上为执行时估值，非收盘收益。')
        if not output['csv_exported']:
            lines.append('账本已提交，CSV 导出失败，请查看日志。')
        return title, '\n'.join(lines)
