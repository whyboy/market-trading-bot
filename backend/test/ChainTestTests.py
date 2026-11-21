"""验证一次性部署测试链路，外部行情和推送使用替身。"""
import importlib
import json
import math
import pandas as pd
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from fastapi.testclient import TestClient
from market_trading_bot import app as app_module
from market_trading_bot.market.AKShareMarket import SHANGHAI
from market_trading_bot.TaskScheduler import TaskScheduler
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1] / 'dev'
MARKET = importlib.import_module('market_trading_bot.market.AKShareMarket')


class ChainTestTests(unittest.TestCase):
    def test_weekend_one_shot_runs_all_tasks_in_isolated_account(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_dir = root / 'market_trading_bot/config'
            config_dir.mkdir(parents=True)
            settings = json.loads((ROOT / 'market_trading_bot/config/global.json').read_text())
            settings['runtime']['test_mode'] = True
            settings['market']['request_interval_seconds'] = 0
            (config_dir / 'global.json').write_text(json.dumps(settings))
            (config_dir / 'etf_rotation_strategy.json').write_text(
                (ROOT / 'market_trading_bot/config/etf_rotation_strategy.json').read_text())
            base = datetime.now(SHANGHAI) + timedelta(days=8)
            now = (base + timedelta(days=(5 - base.weekday()) % 7)).replace(hour=19, minute=23, second=0, microsecond=0)
            days = pd.bdate_range(end=now.date() + timedelta(days=2), periods=45).date.tolist()
            daily = pd.DataFrame({'日期': days, '收盘': [100 * math.exp(i * .001) for i in range(len(days))]})

            def history(**kwargs):
                dates = pd.to_datetime(daily['日期']).dt.strftime('%Y%m%d')
                return daily[(dates >= kwargs['start_date']) & (dates <= kwargs['end_date'])].copy()

            with patch.object(app_module, 'ROOT', root), \
                 patch.object(TaskScheduler, 'now', return_value=now), \
                 patch.object(MARKET, 'datetime', wraps=datetime) as clock, \
                 patch.object(MARKET.ak, 'calendar_trade_date_hist_sina', return_value=pd.DataFrame({'trade_date': days})), \
                 patch.object(MARKET.ak, 'fund_etf_hist_em', side_effect=history), \
                 patch.object(MARKET.requests, 'get') as spot, \
                 patch('market_trading_bot.notification.PushPlusClient.PushPlusClient.send', return_value='receipt') as send:
                clock.now.return_value = now
                with TestClient(app_module.app) as client:
                    scheduler = app_module.app.state.scheduler
                    scheduler.timer.pause()
                    job = scheduler.timer.get_job('_chain_test')
                    self.assertEqual((job.next_run_time - now).total_seconds(), 600)
                    self.assertIsNone(scheduler.timer.get_job('_daily_report'))
                    self.assertIsNone(scheduler.timer.get_job('etf_rotation'))
                    completed = threading.Event()
                    def run_once():
                        try:
                            scheduler._run_chain_test()
                        finally:
                            completed.set()
                    job.modify(func=run_once, next_run_time=datetime.now(SHANGHAI) + timedelta(seconds=1))
                    scheduler.timer.resume()
                    self.assertTrue(completed.wait(10))
                    instance = scheduler.configs['etf_rotation']['instances'][0]['id']
                    task = scheduler.tasks[('etf_rotation', instance)]
                    self.assertFalse(scheduler.calendar_context['is_trading_day'])
                    self.assertTrue(task.result['success'])
                    self.assertTrue(task.output['fills'])
                    self.assertTrue(task.output['signal']['ranking'])
                    self.assertTrue(scheduler.valuation_task.completed)
                    self.assertTrue(scheduler.summary_task.completed)
                    self.assertEqual(send.call_count, 2)
                    self.assertIn('链路测试：使用', send.call_args.args[1])
                    self.assertTrue(client.get(f'/api/accounts/{instance}').json()['initialized'])
                    dates = client.get('/api/strategies/etf_rotation/logs').json()
                    log = client.get(f'/api/strategies/etf_rotation/logs/{dates[0]}').json()['content']
                    self.assertIn(f'行情日期={now.date() - timedelta(days=1)}', log)
                    self.assertIn('策略计算完成', log)
                    self.assertIn('任务完成', log)
                    spot.assert_not_called()
                    self.assertFalse((root / 'data/etf_rotation_pap').exists())
                    self.assertTrue((root / 'data/chain_test' / instance / 'ledger.sqlite3').exists())

    def test_default_mode_is_disabled(self):
        settings = json.loads((ROOT / 'market_trading_bot/config/global.json').read_text())
        self.assertFalse(settings['runtime']['test_mode'])
