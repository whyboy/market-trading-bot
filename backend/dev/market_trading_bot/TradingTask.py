"""执行一次策略任务，完成后线程退出。"""
import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, time
from market_trading_bot.execution.PapExecutor import PapExecutor
from market_trading_bot.log.StrategyLogger import strategy_logging
from market_trading_bot.market.AKShareMarket import AKShareMarket, SHANGHAI
from market_trading_bot.repository.PapAccountRepository import PapAccountRepository
from market_trading_bot.repository.TaskStatusRepository import TaskStatusRepository
from market_trading_bot.strategy.ETFRotationStrategyImpl import ETFRotationStrategyImpl
from pathlib import Path


class TradingTask:
    def __init__(self, root: Path, strategy_path: Path):
        self.root = root
        self.strategy_path = strategy_path
        self.config = json.loads(strategy_path.read_text())
        self.strategy_id = self.config['strategy']
        self.settings = json.loads((root / 'market_trading_bot/config/global.json').read_text())
        self.status_repository = TaskStatusRepository(root, self.strategy_id)
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, name=f'trading-task-{self.strategy_id}', daemon=True)
        self.results = {}
        self.status = {'strategy': self.strategy_id, 'run_id': str(uuid.uuid4()), 'pid': os.getpid(),
                       'state': 'running', 'heartbeat_at': None,
                       'last_execution': None, 'current_execution': None,
                       'trade_timeout_seconds': self.settings.get('runtime', {}).get('trade_timeout_seconds', 300)}

    @staticmethod
    def now():
        return datetime.now(SHANGHAI)

    def start(self):
        self.status['current_execution'] = {'strategy': self.strategy_id, 'started_at': self.now().isoformat()}
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.thread.join()

    def _heartbeat(self):
        self.status['heartbeat_at'] = self.now().isoformat()
        self.status_repository.write(self.status)

    def _run(self):
        try:
            self._heartbeat()
            with strategy_logging(self.root, self.strategy_id, self.settings['logging']) as logger:
                logger.info('策略任务开始 strategy=%s', self.strategy_id)
                for instance in self.config.get('instances', []):
                    if self.stop_event.is_set():
                        break
                    if instance['enabled']:
                        self._execute_trade(instance['id'])
        except Exception as exc:
            self.status['last_execution'] = {'success': False, 'error': str(exc), 'finished_at': self.now().isoformat()}
        finally:
            self.status.update(state='completed', current_execution=None)
            self._heartbeat()

    def _execute_trade(self, instance_id):
        execution = {'instance_id': instance_id, 'started_at': self.now().isoformat()}
        self.status['current_execution'] = execution
        self._heartbeat()
        try:
            self.run_pap(instance_id)
            execution = {**execution, 'success': True}
        except Exception as exc:
            execution = {**execution, 'success': False, 'error': str(exc)}
        execution['finished_at'] = self.now().isoformat()
        execution['scheduled_at'] = datetime.combine(self.now().date(), time.fromisoformat(self.config['schedule']['trade_time']), SHANGHAI).isoformat()
        self.results[instance_id] = {**execution, 'state': 'success' if execution['success'] else 'failed'}
        self.status['last_execution'] = execution
        self._heartbeat()

    def run_pap(self, instance_id: str) -> dict:
        """为启用的 pap 实例模拟调仓；同一北京时间日期只提交一次。

        :param instance_id: 策略配置中唯一、启用的 pap 实例 ID。
        :return: 已提交账户、订单、成交及 CSV 导出状态。
        """
        root, settings, configuration = self.root, self.settings, self.config
        operation = 'pap_rebalance'
        with strategy_logging(root, configuration['strategy'], settings['logging']) as logger:
            logger.info('运行开始 operation=%s strategy=%s instance=%s', operation, configuration['strategy'], instance_id)
            try:
                if settings['timezone'] != 'Asia/Shanghai':
                    raise ValueError('A股策略时区必须为Asia/Shanghai')
                if configuration['strategy'] != 'etf_rotation':
                    raise ValueError('当前仅实现etf_rotation策略')
                matches = [item for item in configuration['instances'] if item['id'] == instance_id]
                if len(matches) != 1 or matches[0]['mode'] != 'pap' or matches[0]['enabled'] is not True:
                    raise ValueError('必须指定唯一、启用的pap实例')
                instance = matches[0]
                executor = PapExecutor(instance['execution'], configuration['instruments'])
                account_repository = PapAccountRepository(root / settings['storage']['data_dir'], instance_id)
                completed = account_repository.completed(datetime.now(SHANGHAI).date().isoformat())
                if completed:
                    logger.info('跳过重复调仓 instance=%s date=%s', instance_id, completed['date'])
                    return self._pap_output(account_repository, completed, logger)
                strategy = ETFRotationStrategyImpl(configuration['parameters'])
                market = AKShareMarket(settings['market'], logger=logger)
                as_of, snapshots = market.load(strategy.symbols, strategy.history_points)
                logger.info('策略计算开始 symbols=%s', ','.join(strategy.symbols))
                result = strategy.calculate(snapshots, as_of)
                for rank, item in enumerate(result.ranking, start=1):
                    logger.info('策略排名 rank=%d symbol=%s score=%.8f annual_volatility=%.8f target_weight=%.6f',
                                rank, item.symbol, item.score, item.annual_volatility, result.target_weights[item.symbol])
                logger.info('策略计算成功 target_weights=%s', result.target_weights)
                committed = account_repository.rebalance(result, executor, instance['initial_cash'])
                logger.info('模拟记账成功 instance=%s date=%s fills=%d cash=%s',
                            instance_id, committed['date'], len(committed['fills']), committed['cash'])
                return self._pap_output(account_repository, committed, logger)
            except Exception:
                logger.exception('运行失败 operation=%s；模拟提交状态以账本为准', operation)
                raise

    @staticmethod
    def _pap_output(account_repository: PapAccountRepository, result: dict, logger) -> dict:
        """导出查看副本；导出失败仍返回已提交结果，重跑可补齐 CSV。"""
        output = {'operation': 'pap_rebalance', 'instance_id': account_repository.directory.name, **result}
        try:
            account_repository.export_csv()
            output['csv_exported'] = True
        except (OSError, sqlite3.Error):
            logger.exception('CSV导出失败；模拟账本已提交，重跑可恢复导出')
            output['csv_exported'] = False
        return output
