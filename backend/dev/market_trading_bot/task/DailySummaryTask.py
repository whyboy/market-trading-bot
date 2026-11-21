"""15:10只读每日结果，汇总所有策略和账户发送一份日报。"""
import logging
import threading
from market_trading_bot.repository.PapAccountRepository import PapAccountRepository


class DailySummaryTask:
    def __init__(self, root, settings, configs, notifier, context, stop_event, now):
        """创建日报任务，汇总范围为配置中启用的模拟账户。

        :param root: backend/dev 目录。
        :param settings: 含 storage.data_dir 的全局配置。
        :param configs: 按策略 ID 索引的策略及账户配置。
        :param notifier: 发送汇总日报的通知对象。
        :param context: 日期、是否交易日及上一交易日，失败时包含 notice。
        :param stop_event: 与调度器共享的停止信号。
        :param now: 返回当前北京时间的函数。
        """
        self.directory = root / settings['storage']['data_dir']
        self.configs = configs
        self.notifier = notifier
        self.context = context
        self.stop_event = stop_event
        self.now = now
        self.thread = threading.Thread(target=self._run, name='daily-summary-task', daemon=True)
        self.completed = False
        self.logger = logging.getLogger(__name__)

    def start(self):
        """由调度器在15:10启动汇总线程；不获取行情、不修改账户数据库。"""
        self.thread.start()

    def stop(self):
        """发出共享停止信号，并等待日报线程退出。"""
        self.stop_event.set()
        self.thread.join()

    def _run(self):
        """执行日报汇总，记录完成状态；未处理的异常写入日志。"""
        try:
            self.completed = self.run()
        except Exception:
            self.logger.exception('每日汇总任务失败')

    def run(self):
        """读取所有启用模拟账户的结果，合并为一份日报交给通知器发送。

        :return: 已调用通知器返回 True；停止或跨日返回 False，不保证消息送达。
        """
        day = self.context['day']
        summaries = []
        for strategy_id, config in sorted(self.configs.items()):
            for instance in config.get('instances', []):
                if not instance['enabled'] or instance['mode'] != 'pap':
                    continue
                if self.stop_event.is_set() or self.now().date() != day:
                    return False
                try:
                    summary = self._read_summary(instance['id'])
                except Exception:
                    self.logger.exception('读取日报失败 strategy=%s instance=%s', strategy_id, instance['id'])
                    summary = {'notice': '读取账户收益失败，请查看日志。'}
                summaries.append({'strategy_id': strategy_id, 'instance_id': instance['id'], 'summary': summary})
        if self.stop_event.is_set() or self.now().date() != day:
            return False
        self.notifier.send_daily_summary_report(day, summaries)
        return True

    def _read_summary(self, instance_id):
        """读取账户日报数据；休市日取最近成功估值，未完成或失败时返回提示。

        :param instance_id: 账户 ID。
        :return: 估值字典，或仅包含 notice 的提示字典；不请求行情、不写数据库。
        """
        day = self.context['day']
        if self.context.get('notice'):
            return {'notice': self.context['notice']}
        account = PapAccountRepository.get_account_snapshot_on_or_before(self.directory, instance_id, day.isoformat())
        if not account['initialized']:
            return {'notice': '账户尚未开户，暂无持仓和收益数据。'}
        if not self.context['is_trading_day']:
            snapshot = PapAccountRepository.find_latest_valuation_on_or_before(self.directory, instance_id, day.isoformat())
            if snapshot is None:
                return {'notice': '今日休市，暂无有效收盘估值。'}
            return {**snapshot, 'is_trading_day': False}
        result = PapAccountRepository.get_daily_result(self.directory, instance_id, day.isoformat())
        if result is None or result['valuation_status'] == 'pending':
            return {'notice': '当天收盘估值尚未执行或尚未完成，暂无当天收益。'}
        if result['valuation_status'] == 'running':
            return {'notice': '收盘估值仍在执行，暂无当天收益。'}
        if result['valuation_status'] == 'failed':
            return {'notice': result['valuation_error']}
        return {**result['valuation'], 'is_trading_day': True}
