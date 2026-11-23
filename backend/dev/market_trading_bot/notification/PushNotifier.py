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

    def send_morning_greeting(self, is_trading_day):
        """发送每日早安通知；推送失败只记录日志。"""
        if is_trading_day is True:
            status = '今天是交易日'
            detail = '程序目前运行正常，正在等待开盘。'
        elif is_trading_day is False:
            status = '今天休市'
            detail = '程序目前运行正常，今天市场休市。'
        else:
            status = '交易日历暂不可用'
            detail = '程序正在运行，今天是否交易暂时无法确认。'
        try:
            self.client.send(f'主人，早上好！{status}',
                             f'主人，早上好！今天又是美好的一天。\n{detail}\n'
                             '愿您今天一切顺利，又向财富自由迈进一步！')
        except Exception:
            self.logger.exception('每日早安通知发送失败')

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
