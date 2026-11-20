"""协调信号预览与手动模拟调仓，按实例恢复和提交账本。"""
import json
from .AKShareMarket import AKShareMarket, SHANGHAI
from .PaperExecutor import PaperExecutor
from .PaperLedger import PaperLedger
from .strategies.ETFRotationStrategy import ETFRotationStrategy
from .StrategyLogger import strategy_logging
from dataclasses import asdict
from datetime import datetime
from pathlib import Path


def preview_signals(root: Path, strategy_path: Path) -> dict:
    """加载配置和行情，计算目标仓位并记录日志，仅预览信号。

    :param root: 仓库根目录，用于定位全局配置和日志目录。
    :param strategy_path: 策略 JSON 文件路径。
    :return: 含行情源、价格口径、排名和目标权重的字典。
    :raises ValueError: 时区或策略类型不受支持。
    :raises MarketDataError: 行情获取或校验失败。
    """
    return _run(root, strategy_path)


def run_paper(root: Path, strategy_path: Path, instance_id: str) -> dict:
    """为启用的 pap 实例模拟调仓；同一北京时间日期只提交一次。

    :param instance_id: 策略配置中的实例 ID，不允许实盘或禁用实例。
    :return: 已提交账户、订单、成交及 CSV 导出状态。
    """
    return _run(root, strategy_path, instance_id)


def _run(root: Path, strategy_path: Path, instance_id: str | None = None) -> dict:
    """协调配置、行情、策略及可选的模拟记账。"""
    with (root / 'config/global.json').open() as stream:
        settings = json.load(stream)
    with strategy_path.open() as stream:
        configuration = json.load(stream)
    operation = 'paper_rebalance' if instance_id is not None else 'signal_preview'
    with strategy_logging(root, configuration['strategy'], settings['logging']) as logger:
        logger.info('运行开始 operation=%s strategy=%s instance=%s', operation, configuration['strategy'], instance_id)
        try:
            if settings['timezone'] != 'Asia/Shanghai':
                raise ValueError('A股策略时区必须为Asia/Shanghai')
            if configuration['strategy'] != 'etf_rotation':
                raise ValueError('当前仅实现etf_rotation策略')
            ledger = None
            if instance_id is not None:
                matches = [item for item in configuration['instances'] if item['id'] == instance_id]
                if len(matches) != 1 or matches[0]['mode'] != 'pap' or matches[0]['enabled'] is not True:
                    raise ValueError('必须指定唯一、启用的pap实例')
                instance = matches[0]
                executor = PaperExecutor(instance['execution'], configuration['instruments'])
                ledger = PaperLedger(root / settings['storage']['data_dir'], instance_id)
                completed = ledger.completed(datetime.now(SHANGHAI).date().isoformat())
                if completed:
                    logger.info('跳过重复调仓 instance=%s date=%s', instance_id, completed['date'])
                    return _paper_output(ledger, completed, logger)
            strategy = ETFRotationStrategy(configuration['parameters'])
            market = AKShareMarket(settings['market'], logger=logger)
            as_of, snapshots = market.load(strategy.symbols, strategy.history_points)
            logger.info('策略计算开始 symbols=%s', ','.join(strategy.symbols))
            result = strategy.calculate(snapshots, as_of)
            for rank, item in enumerate(result.ranking, start=1):
                logger.info('策略排名 rank=%d symbol=%s score=%.8f annual_volatility=%.8f target_weight=%.6f',
                            rank, item.symbol, item.score, item.annual_volatility, result.target_weights[item.symbol])
            logger.info('策略计算成功 target_weights=%s', result.target_weights)
            if ledger is not None:
                committed = ledger.rebalance(result, executor, instance['initial_cash'])
                logger.info('模拟记账成功 instance=%s date=%s fills=%d cash=%s',
                            instance_id, committed['date'], len(committed['fills']), committed['cash'])
                return _paper_output(ledger, committed, logger)
            logger.info('交易状态 executed=false reason=仅信号预览，未执行买卖')
            logger.info('运行成功 as_of=%s', as_of.isoformat())
            return {'operation': 'signal_preview', 'provider': settings['market']['provider'],
                    'price_basis': 'qfq_daily_and_unadjusted_intraday', **asdict(result)}
        except Exception:
            logger.exception('运行失败 operation=%s；模拟提交状态以账本为准', operation)
            raise


def _paper_output(ledger: PaperLedger, result: dict, logger) -> dict:
    """导出查看副本；导出失败仍返回已提交结果，重跑可补齐 CSV。"""
    output = {'operation': 'paper_rebalance', 'instance_id': ledger.directory.name, **result}
    try:
        ledger.export_csv()
        output['csv_exported'] = True
    except OSError:
        logger.exception('CSV导出失败；模拟账本已提交，重跑可恢复导出')
        output['csv_exported'] = False
    return output
