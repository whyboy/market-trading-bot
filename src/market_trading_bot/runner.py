"""手动计算信号；后续在此接入调度、执行和落盘。"""
import json
from .AKShareMarket import AKShareMarket
from .strategies.ETFRotationStrategy import ETFRotationStrategy
from .StrategyLogger import strategy_logging
from dataclasses import asdict
from pathlib import Path


def preview(root: Path, strategy_path: Path) -> dict:
    with (root / 'config/global.json').open() as stream:
        settings = json.load(stream)
    with strategy_path.open() as stream:
        configuration = json.load(stream)
    with strategy_logging(root, configuration['strategy'], settings['logging']) as logger:
        logger.info('运行开始 operation=signal_preview strategy=%s', configuration['strategy'])
        try:
            if settings['timezone'] != 'Asia/Shanghai':
                raise ValueError('A股策略时区必须为Asia/Shanghai')
            if configuration['strategy'] != 'etf_rotation':
                raise ValueError('当前仅实现etf_rotation策略')
            strategy = ETFRotationStrategy(configuration['parameters'])
            market = AKShareMarket(settings['market'], logger=logger)
            as_of, snapshots = market.load(strategy.symbols, strategy.history_points)
            logger.info('策略计算开始 symbols=%s', ','.join(strategy.symbols))
            result = strategy.calculate(snapshots, as_of)
            for rank, item in enumerate(result.ranking, start=1):
                logger.info('策略排名 rank=%d symbol=%s score=%.8f annual_volatility=%.8f target_weight=%.6f',
                            rank, item.symbol, item.score, item.annual_volatility, result.target_weights[item.symbol])
            logger.info('策略计算成功 target_weights=%s', result.target_weights)
            logger.info('交易状态 executed=false reason=仅信号预览，未执行买卖')
            logger.info('运行成功 as_of=%s', as_of.isoformat())
            return {'operation': 'signal_preview', 'provider': settings['market']['provider'],
                    'price_basis': 'qfq_daily_and_unadjusted_intraday', **asdict(result)}
        except Exception:
            logger.exception('运行失败 executed=false 未执行买卖')
            raise
