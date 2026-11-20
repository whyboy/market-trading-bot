"""手动计算信号；后续在此接入调度、执行和落盘。"""
import json
from .AKShareMarket import AKShareMarket
from .strategies.ETFRotationStrategy import ETFRotationStrategy
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


def preview(root: Path, strategy_path: Path, at: str | None = None) -> dict:
    with (root / 'config/global.json').open() as stream:
        settings = json.load(stream)
    with strategy_path.open() as stream:
        configuration = json.load(stream)
    if settings['timezone'] != 'Asia/Shanghai':
        raise ValueError('A股策略时区必须为Asia/Shanghai')
    if configuration['strategy'] != 'etf_rotation':
        raise ValueError('当前仅实现etf_rotation策略')
    timezone = ZoneInfo(settings['timezone'])
    as_of = datetime.fromisoformat(at) if at else datetime.now(timezone)
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=timezone)
    as_of = as_of.astimezone(timezone).replace(second=0, microsecond=0)
    strategy = ETFRotationStrategy(configuration['parameters'])
    market = AKShareMarket(settings['market'])
    snapshots = market.load(strategy.symbols, as_of, strategy.history_points)
    result = strategy.calculate(snapshots, as_of)
    return {'operation': 'signal_preview', 'provider': settings['market']['provider'],
            'price_basis': 'qfq_daily_and_unadjusted_intraday', **asdict(result)}
