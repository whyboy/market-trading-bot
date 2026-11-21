"""常驻交易进程；可选执行一次模拟调仓，暂不包含定时调度。"""
import argparse
import fcntl
import os
import signal
import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'src'))
from market_trading_bot.runtime.RuntimeStore import RuntimeStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pap', help='启动后执行一次指定模拟账户调仓')
    parser.add_argument('--config', default='config/etf_rotation_strategy.json')
    args = parser.parse_args()
    stop = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stop.set())
    store = RuntimeStore(ROOT)
    store.database.parent.mkdir(parents=True, exist_ok=True)
    lock = (store.database.parent / 'worker.lock').open('a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit('已有交易进程运行，不能重复启动')
    status = {'run_id': str(uuid.uuid4()), 'pid': os.getpid(),
              'started_at': datetime.now(timezone.utc).isoformat(), 'state': 'idle',
              'last_execution': None, 'scheduler_enabled': False}

    def heartbeat():
        while not stop.is_set():
            store.write({**status, 'heartbeat_at': datetime.now(timezone.utc).isoformat()})
            stop.wait(5)

    # 单个部署只启动一个 worker；心跳线程保证策略执行时仍可观察存活情况。
    thread = threading.Thread(target=heartbeat)
    thread.start()
    try:
        if args.pap:
            status['state'] = 'running'
            execution = {'instance_id': args.pap, 'started_at': datetime.now(timezone.utc).isoformat()}
            try:
                from market_trading_bot.runner import run_pap
                run_pap(ROOT, ROOT / args.config, args.pap)
                execution['success'] = True
            except Exception as exc:
                execution.update(success=False, error=str(exc))
            execution['finished_at'] = datetime.now(timezone.utc).isoformat()
            status.update(state='idle', last_execution=execution)
        stop.wait()
    finally:
        stop.set()
        thread.join()
        store.write({**status, 'state': 'stopped', 'heartbeat_at': datetime.now(timezone.utc).isoformat()})


if __name__ == '__main__':
    main()
