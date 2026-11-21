"""生成报告后直接发送，推送异常不影响交易结果。"""
import logging
from market_trading_bot.notification.PushPlusClient import PushPlusClient
from market_trading_bot.notification.report.DailyAccountReport import DailyAccountReport
from market_trading_bot.notification.report.TradeExecutionReport import TradeExecutionReport


class PushPlusNotifier:
    def __init__(self, settings):
        self.logger = logging.getLogger(__name__)
        token = settings['pushplus']['token'].strip()
        self.client = PushPlusClient(token)

    def send_trade_execution_report(self, task):
        """策略执行结束后发送报告，无成交也发送；推送失败仅记录日志，不影响交易结果。"""
        try:
            title, content = TradeExecutionReport.build(task)
            self.client.send(title, content)
        except PushPlusClient.Error as exc:
            self.logger.error('执行报告发送失败：%s；交易结果不受影响', exc)
        except Exception:
            self.logger.error('执行报告生成或发送失败 strategy=%s instance=%s；交易结果不受影响',
                              task.strategy_id, task.instance_id)

    def send_daily_summary_report(self, day, summaries):
        """将所有策略和账户汇总为一份日报并发送，失败仅记录日志。

        :param day: 报告日期。
        :param summaries: 列表，每项包含 strategy_id、instance_id 和 summary。
        """
        try:
            title, content = DailyAccountReport.build(day, summaries)
            self.client.send(title, content)
        except PushPlusClient.Error as exc:
            self.logger.error('每日总结发送失败：%s', exc)
        except Exception:
            self.logger.error('每日总结生成或发送失败 date=%s', day)
