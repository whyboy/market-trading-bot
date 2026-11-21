"""隔离报告生成与网络发送，发送任务由现有调度器驱动。"""
import logging
from market_trading_bot.notification.DailyAccountReport import DailyAccountReport
from market_trading_bot.notification.PushPlusClient import PushPlusClient
from market_trading_bot.notification.TradeExecutionReport import TradeExecutionReport
from market_trading_bot.repository.NotificationRepository import NotificationRepository


class PushPlusNotifier:
    def __init__(self, root, settings):
        self.logger = logging.getLogger(__name__)
        self.repository = None
        if not settings.get('pushplus', {}).get('enabled', False):
            return
        token = settings['pushplus']['token'].strip()
        if not token:
            self.logger.error('PushPlus 已启用但未设置 pushplus.token，通知已停用；交易继续运行')
            return
        self.client = PushPlusClient(token)
        try:
            self.repository = NotificationRepository(root / settings['storage']['data_dir'] / 'runtime')
        except Exception:
            self.logger.error('通知数据库初始化失败，通知已停用；交易继续运行')

    def report(self, task):
        if self.repository is None:
            return
        try:
            title, content = TradeExecutionReport.build(task)
            self.repository.enqueue((task.strategy_id, task.instance_id,
                                     task.scheduled_at.date().isoformat(), 'execution'), title, content)
        except Exception:
            self.logger.error('执行报告保存失败 strategy=%s instance=%s；交易结果不受影响',
                              task.strategy_id, task.instance_id)

    def daily_report(self, strategy_id, instance_id, summary):
        """接收已计算的每日估值，与执行报告共用队列及重试机制。"""
        if self.repository is None:
            return
        try:
            title, content = DailyAccountReport.build(strategy_id, instance_id, summary)
            self.repository.enqueue((strategy_id, instance_id, summary['date'], 'daily'), title, content)
        except Exception:
            self.logger.error('每日总结保存失败 strategy=%s instance=%s', strategy_id, instance_id)

    def dispatch(self):
        try:
            message = self.repository.due()
            if message is None:
                return
            if message['attempts'] >= 3:
                self.repository.finish(message, 'failed', error='达到发送次数上限')
                return
            attempts = self.repository.attempted(message)
            try:
                receipt = self.client.send(message['title'], message['content'])
            except PushPlusClient.Error as exc:
                state = 'pending' if exc.retryable and attempts < 3 else 'failed'
                self.repository.finish(message, state, error=str(exc))
                self.logger.warning('通知发送失败：%s；状态=%s', exc, state)
            else:
                self.repository.finish(message, 'accepted', receipt=receipt)
        except Exception:
            self.logger.error('通知队列处理失败；交易继续运行')
