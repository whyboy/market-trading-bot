"""一个策略对象与一个账户组成一次性任务，每次执行对应一个线程。"""
import logging
import os
import sqlite3
import threading
import uuid
from contextlib import nullcontext
from dataclasses import asdict
from datetime import datetime
from market_trading_bot.market.AKShareMarket import SHANGHAI


class TradingTask:
    """协调策略、行情、模拟交易和记账。"""

    def __init__(self, strategy, market, executor, account_store, *,
                 strategy_id, instance_id, initial_cash, scheduled_at, notifier, trade_timeout_seconds=300,
                 log_context=None):
        """创建交易线程；scheduled_at 决定记账日期，超时仅用于监控。"""
        self.strategy = strategy
        self.market = market
        self.executor = executor
        self.account_store = account_store
        self.strategy_id = strategy_id
        self.instance_id = instance_id
        self.initial_cash = initial_cash
        self.scheduled_at = scheduled_at
        self.logger = logging.getLogger(__name__)
        self.log_context = log_context if log_context is not None else nullcontext(self.logger)
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, name=f'trading-task-{strategy_id}-{instance_id}', daemon=True)
        self.status = {'strategy': strategy_id, 'instance_id': instance_id, 'run_id': str(uuid.uuid4()),
                       'pid': os.getpid(), 'state': 'running', 'heartbeat_at': None,
                       'last_execution': None, 'current_execution': None,
                       'trade_timeout_seconds': trade_timeout_seconds}
        self.result = None
        self.output = None
        self.notifier = notifier

    def start(self):
        """记录开始时间并启动一次性交易线程。"""
        self.status['current_execution'] = {'instance_id': self.instance_id, 'started_at': self.now().isoformat()}
        self.thread.start()

    def stop(self):
        """发出停止信号并等待线程退出，不强制中断已开始的交易。"""
        self.stop_event.set()
        self.thread.join()

    def _run(self):
        """执行交易并保存运行状态，成功或失败均发送执行报告。"""
        execution = {**self.status['current_execution'], 'scheduled_at': self.scheduled_at.isoformat()}
        try:
            with self.log_context as logger:
                self.logger = logger
                self.market.logger = logger
                self._heartbeat()
                logger.info('任务开始 strategy=%s instance=%s', self.strategy_id, self.instance_id)
                try:
                    if self.stop_event.is_set():
                        execution.update(success=False, error='任务已停止')
                    else:
                        self.output = self.trade()
                        execution['success'] = True
                        logger.info('任务完成 strategy=%s instance=%s', self.strategy_id, self.instance_id)
                except Exception as exc:
                    execution.update(success=False, error=str(exc))
                    logger.exception('策略任务失败 strategy=%s instance=%s', self.strategy_id, self.instance_id)
        except Exception as exc:
            execution.update(success=False, error=str(exc))
        finally:
            execution['finished_at'] = self.now().isoformat()
            self.result = {**execution, 'state': 'success' if execution.get('success') else 'failed'}
            self.status.update(state='completed', current_execution=None, last_execution=execution)
            try:
                self._heartbeat()
            finally:
                self.notifier.send_trade_report(self)

    def trade(self) -> dict:
        """已有当日账本则直接返回，否则获取行情、计算信号并原子调仓。"""
        committed = self.account_store.get_execution(self.scheduled_at.date().isoformat())
        if committed is None:
            self.logger.info('获取行情 instance=%s', self.instance_id)
            as_of, snapshots = self.market.load(self.strategy.symbols, self.strategy.history_points)
            signal = self.strategy.calculate(snapshots, as_of)
            if signal.as_of.date() != self.scheduled_at.date():
                raise ValueError('策略信号日期必须与任务计划日期一致')
            for rank, item in enumerate(signal.ranking, start=1):
                self.logger.info('策略排名 instance=%s rank=%d symbol=%s score=%.8f target_weight=%.6f',
                                 self.instance_id, rank, item.symbol, item.score, signal.target_weights[item.symbol])
            self.logger.info('策略计算完成 instance=%s target_weights=%s', self.instance_id, signal.target_weights)
            committed = self._rebalance(signal)
        output = {'operation': 'trade_executor_rebalance', 'instance_id': self.instance_id, **committed}
        try:
            self.account_store.export_trades()
            output['csv_exported'] = True
        except (OSError, sqlite3.Error):
            self.logger.exception('CSV导出失败；账本已提交，可重新导出')
            output['csv_exported'] = False
        return output

    def _rebalance(self, signal):
        """在同一事务中读取账户、模拟调仓并保存，已记账则复用结果。"""
        with self.account_store.transaction(signal.as_of.date().isoformat(), self.initial_cash) as transaction:
            if transaction.completed is not None:
                result = transaction.completed
            else:
                result = self.executor.rebalance(signal, transaction.account)
                result['signal'] = asdict(signal)
                result = transaction.save_execution(result)
        return result

    def _heartbeat(self):
        """更新交易任务的内存心跳。"""
        self.status['heartbeat_at'] = self.now().isoformat()

    @staticmethod
    def now():
        """返回当前北京时间。"""
        return datetime.now(SHANGHAI)
