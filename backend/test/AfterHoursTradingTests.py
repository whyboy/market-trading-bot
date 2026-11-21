"""验证非交易时段使用最近收盘价运行交易、估值和日报。"""
import importlib
import json
import math
import pandas as pd
import tempfile
import unittest
from datetime import datetime, timedelta
from market_trading_bot.market.AKShareMarket import AKShareMarket, SHANGHAI
from market_trading_bot.TaskScheduler import TaskScheduler
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1] / 'dev'
MARKET = importlib.import_module('market_trading_bot.market.AKShareMarket')


class AfterHoursTradingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        directory = self.root / 'market_trading_bot/config'
        directory.mkdir(parents=True)
        settings = json.loads((ROOT / 'market_trading_bot/config/global.json').read_text())
        settings['market']['request_interval_seconds'] = 0
        settings['schedule'] = {'calendar_time': '08:01', 'valuation_time': '19:31'}
        self.settings = settings
        self.config = json.loads((ROOT / 'market_trading_bot/config/etf_rotation_strategy.json').read_text())
        self.config['schedule'] = {'trade_time': '19:30', 'report_time': '19:32'}
        (directory / 'global.json').write_text(json.dumps(settings))
        (directory / 'etf_rotation_strategy.json').write_text(json.dumps(self.config))
        base = datetime.now(SHANGHAI) + timedelta(days=8)
        self.current = (base + timedelta(days=(5 - base.weekday()) % 7)).replace(hour=8, minute=0, second=0, microsecond=0)
        self.days = pd.bdate_range(end=self.current.date() + timedelta(days=2), periods=45).date.tolist()
        self.daily = pd.DataFrame({'日期': self.days, '收盘': [100 * math.exp(i * .001) for i in range(len(self.days))]})
        self.clock = patch.object(TaskScheduler, 'now', side_effect=lambda: self.current)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.market_clock = patch.object(MARKET, 'datetime', wraps=datetime)
        market_clock = self.market_clock.start()
        market_clock.now.side_effect = lambda *args: self.current
        self.addCleanup(self.market_clock.stop)
        for item in (
            patch.object(MARKET.ak, 'calendar_trade_date_hist_sina', return_value=pd.DataFrame({'trade_date': self.days})),
            patch.object(AKShareMarket, '_daily', side_effect=self.history),
        ):
            item.start()
            self.addCleanup(item.stop)

    def history(self, **kwargs):
        dates = pd.to_datetime(self.daily['日期']).dt.strftime('%Y%m%d')
        return self.daily[(dates >= kwargs['start_date']) & (dates <= kwargs['end_date'])].copy()

    def test_latest_close_in_weekend_before_open_lunch_and_after_close(self):
        saturday = self.current
        friday = saturday - timedelta(days=1)
        monday = saturday + timedelta(days=2)
        market = AKShareMarket(self.settings['market'])
        for current, expected in ((saturday.replace(hour=19), friday.date()),
                                  (monday.replace(hour=8), friday.date()),
                                  (monday.replace(hour=12), friday.date()),
                                  (friday.replace(hour=16), friday.date())):
            with self.subTest(current=current):
                self.current = current
                _, snapshots = market.load(tuple(self.config['parameters']['etf_pool']), 24)
                for snapshot in snapshots.values():
                    self.assertEqual(snapshot.quote_time.date(), expected)
                    self.assertLess(snapshot.history[-1].date, expected)

    def test_configured_weekend_chain_uses_friday_close(self):
        with patch('market_trading_bot.notification.PushPlusClient.PushPlusClient.send', return_value='receipt') as send:
            scheduler = TaskScheduler(self.root)
            scheduler.start()
            scheduler.timer.pause()
            try:
                self.assertFalse(scheduler.calendar_context['is_trading_day'])
                calendar = scheduler.timer.get_job('_calendar').trigger
                valuation = scheduler.timer.get_job('_daily_valuation').trigger
                report = scheduler.timer.get_job('_daily_report_19:32').trigger
                self.assertEqual(calendar.get_next_fire_time(None, self.current).time().isoformat(), '08:01:00')
                self.assertEqual(valuation.get_next_fire_time(None, self.current).time().isoformat(), '19:31:00')
                self.assertEqual(report.get_next_fire_time(None, self.current).time().isoformat(), '19:32:00')
                self.current = self.current.replace(hour=19, minute=30)
                scheduler._start_trades('etf_rotation')
                task = scheduler.tasks[('etf_rotation', 'etf_rotation_pap')]
                task.thread.join(timeout=5)
                self.assertTrue(task.result['success'])
                self.assertTrue(task.output['signal']['ranking'])
                self.assertEqual(datetime.fromisoformat(task.output['signal']['ranking'][0]['quote_time']).date(), self.current.date() - timedelta(days=1))
                self.current = self.current.replace(minute=31)
                scheduler._start_valuation()
                scheduler.valuation_task.thread.join(timeout=5)
                self.assertTrue(scheduler.valuation_task.completed)
                self.current = self.current.replace(minute=32)
                scheduler._start_summary(report_time='19:32')
                scheduler.summary_task.thread.join(timeout=5)
                self.assertTrue(scheduler.summary_task.completed)
                self.assertEqual(send.call_count, 2)
                self.assertIn('每日收益总结', send.call_args.args[0])
            finally:
                scheduler.stop()
