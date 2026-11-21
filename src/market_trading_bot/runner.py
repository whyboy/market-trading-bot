"""协调定时模拟调仓，按实例恢复和提交账本。"""
import json
import sqlite3
from .AKShareMarket import AKShareMarket, SHANGHAI
from .PapAccountStore import PapAccountStore
from .PapExecutor import PapExecutor
from .strategies.ETFRotationStrategy import ETFRotationStrategy
from .StrategyLogger import strategy_logging
from dataclasses import asdict
from datetime import datetime
from pathlib import Path


def run_pap(root: Path, strategy_path: Path, instance_id: str) -> dict:
    """为启用的 pap 实例模拟调仓；同一北京时间日期只提交一次。

    :param root: 仓库根目录，用于定位全局配置、日志和账户目录。
    :param strategy_path: 策略 JSON 文件路径。
    :param instance_id: 策略配置中唯一、启用的 pap 实例 ID。
    :return: 已提交账户、订单、成交及 CSV 导出状态。
    """
    with (root / 'config/global.json').open() as stream:
        settings = json.load(stream)
    with strategy_path.open() as stream:
        configuration = json.load(stream)
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
            account_store = PapAccountStore(root / settings['storage']['data_dir'], instance_id)
            completed = account_store.completed(datetime.now(SHANGHAI).date().isoformat())
            if completed:
                logger.info('跳过重复调仓 instance=%s date=%s', instance_id, completed['date'])
                return _pap_output(account_store, completed, logger)
            strategy = ETFRotationStrategy(configuration['parameters'])
            market = AKShareMarket(settings['market'], logger=logger)
            as_of, snapshots = market.load(strategy.symbols, strategy.history_points)
            logger.info('策略计算开始 symbols=%s', ','.join(strategy.symbols))
            result = strategy.calculate(snapshots, as_of)
            for rank, item in enumerate(result.ranking, start=1):
                logger.info('策略排名 rank=%d symbol=%s score=%.8f annual_volatility=%.8f target_weight=%.6f',
                            rank, item.symbol, item.score, item.annual_volatility, result.target_weights[item.symbol])
            logger.info('策略计算成功 target_weights=%s', result.target_weights)
            committed = account_store.rebalance(result, executor, instance['initial_cash'])
            logger.info('模拟记账成功 instance=%s date=%s fills=%d cash=%s',
                        instance_id, committed['date'], len(committed['fills']), committed['cash'])
            return _pap_output(account_store, committed, logger)
        except Exception:
            logger.exception('运行失败 operation=%s；模拟提交状态以账本为准', operation)
            raise


def _pap_output(account_store: PapAccountStore, result: dict, logger) -> dict:
    """导出查看副本；导出失败仍返回已提交结果，重跑可补齐 CSV。"""
    output = {'operation': 'pap_rebalance', 'instance_id': account_store.directory.name, **result}
    try:
        account_store.export_csv()
        output['csv_exported'] = True
    except (OSError, sqlite3.Error):
        logger.exception('CSV导出失败；模拟账本已提交，重跑可恢复导出')
        output['csv_exported'] = False
    return output
