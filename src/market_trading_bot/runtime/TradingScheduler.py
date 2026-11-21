"""统一加载策略配置并管理各策略的 TradingTask。"""
import json
from .RuntimeStore import RuntimeStore
from .TradingTask import TradingTask
from datetime import datetime, timezone
from pathlib import Path


class TradingScheduler:
    def __init__(self, root: Path):
        self.root = root
        self.tasks = {}

    def start(self):
        try:
            for path in sorted((self.root / 'config').glob('*_strategy.json')):
                config = json.loads(path.read_text())
                if config.get('enable', True):
                    task = TradingTask(self.root, path)
                    task.start()
                    self.tasks[config['strategy']] = task
        except BaseException:
            self.stop()
            raise

    def stop(self):
        # 先通知所有任务退出，避免等待某个交易时其他策略仍启动新任务。
        for task in self.tasks.values():
            task.stop_event.set()
        for task in self.tasks.values():
            task.stop()

    def get_status(self, strategy_id: str):
        status = RuntimeStore(self.root, strategy_id).read()
        if status is None:
            return {'strategy': strategy_id, 'state': 'unknown', 'online': False}
        now = datetime.now(timezone.utc)
        age = max(0, (now - datetime.fromisoformat(status['heartbeat_at'])).total_seconds())
        task = self.tasks.get(strategy_id)
        if task is not None:
            status.update(task.snapshot())
        execution = status.get('current_execution')
        running = status['state'] == 'running' and execution is not None
        execution_age = max(0, (now - datetime.fromisoformat(execution['started_at'])).total_seconds()) if running else None
        timed_out = running and execution_age > status.get('trade_timeout_seconds', 300)
        online = (not timed_out if running else age <= 20) and status['state'] != 'stopped'
        health = 'trade_timeout' if timed_out else 'heartbeat_timeout' if not online and status['state'] != 'stopped' else status['state']
        if task is not None:
            if not task.thread.is_alive():
                online = False
                health = 'stopped'
        return {**status, 'online': online, 'health': health, 'execution_age_seconds': execution_age,
                'heartbeat_age_seconds': age,
                'uptime_seconds': max(0, (now - datetime.fromisoformat(status['started_at'])).total_seconds()) if online else None}
