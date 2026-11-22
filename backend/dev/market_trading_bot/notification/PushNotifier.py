"""生成报告后直接发送，推送异常不影响交易结果。"""
import logging
from market_trading_bot.notification.PushClient import PushClient
from market_trading_bot.notification.report.DailyAccountReport import DailyAccountReport
from market_trading_bot.notification.report.TradeExecutionReport import TradeExecutionReport


class PushNotifier:
    def __init__(self, settings):
        self.logger = logging.getLogger(__name__)
        token = settings['pushplus']['token'].strip()
        self.client = PushClient(token)

    def send_trade_report(self, task):
        """发送执行报告，无成交也发送；失败仅记录日志。"""
        try:
            title, content = TradeExecutionReport.build(task)
            self.client.send(title, content)
        except PushClient.Error as exc:
            self.logger.error('执行报告发送失败：%s；交易结果不受影响', exc)
        except Exception:
            self.logger.error('执行报告生成或发送失败 strategy=%s instance=%s；交易结果不受影响',
                              task.strategy_id, task.instance_id)

    def send_daily_report(self, day, summaries):
        """汇总所有策略账户并发送一份日报；失败仅记录日志。"""
        try:
            title, content = DailyAccountReport.build(day, summaries)
            self.client.send(title, content)
        except PushClient.Error as exc:
            self.logger.error('每日总结发送失败：%s', exc)
        except Exception:
            self.logger.error('每日总结生成或发送失败 date=%s', day)
