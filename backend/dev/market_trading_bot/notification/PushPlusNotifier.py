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

    def report(self, task):
        try:
            title, content = TradeExecutionReport.build(task)
            self.client.send(title, content)
        except PushPlusClient.Error as exc:
            self.logger.error('执行报告发送失败：%s；交易结果不受影响', exc)
        except Exception:
            self.logger.error('执行报告生成或发送失败 strategy=%s instance=%s；交易结果不受影响',
                              task.strategy_id, task.instance_id)

    def daily_report(self, strategy_id, instance_id, summary):
        """接收已计算的每日估值，生成并发送总结。"""
        try:
            title, content = DailyAccountReport.build(strategy_id, instance_id, summary)
            self.client.send(title, content)
        except PushPlusClient.Error as exc:
            self.logger.error('每日总结发送失败：%s', exc)
        except Exception:
            self.logger.error('每日总结生成或发送失败 strategy=%s instance=%s', strategy_id, instance_id)
