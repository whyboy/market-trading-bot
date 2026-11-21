"""执行一次策略任务，完成后线程退出。"""
import json
import os
import threading
import uuid
from .RuntimeStore import RuntimeStore
from datetime import datetime, time
from market_trading_bot.AKShareMarket import SHANGHAI
from market_trading_bot.StrategyLogger import strategy_logging
from pathlib import Path


class TradingTask:
    def __init__(self, root: Path, strategy_path: Path):
        self.root = root
        self.strategy_path = strategy_path
        self.config = json.loads(strategy_path.read_text())
        self.strategy_id = self.config['strategy']
        self.settings = json.loads((root / 'config/global.json').read_text())
        self.store = RuntimeStore(root, self.strategy_id)
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
        self.store.write(self.status)

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
        from market_trading_bot.runner import run_pap
        execution = {'instance_id': instance_id, 'started_at': self.now().isoformat()}
        self.status['current_execution'] = execution
        self._heartbeat()
        try:
            run_pap(self.root, self.strategy_path, instance_id)
            execution = {**execution, 'success': True}
        except Exception as exc:
            execution = {**execution, 'success': False, 'error': str(exc)}
        execution['finished_at'] = self.now().isoformat()
        execution['scheduled_at'] = datetime.combine(self.now().date(), time.fromisoformat(self.config['schedule']['trade_time']), SHANGHAI).isoformat()
        self.results[instance_id] = {**execution, 'state': 'success' if execution['success'] else 'failed'}
        self.status['last_execution'] = execution
        self._heartbeat()
