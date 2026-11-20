"""运行信号预览，或为指定模拟实例执行一次调仓。"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'src'))


def main() -> int:
    """输出信号或模拟调仓结果；成功返回 0，失败输出错误并返回 1。"""
    parser = argparse.ArgumentParser(description='ETF策略信号预览与模拟调仓')
    parser.add_argument('--config', default='config/etf_rotation_strategy.json', help='策略JSON路径，相对仓库根目录')
    parser.add_argument('--pap', metavar='INSTANCE_ID', help='为指定pap实例模拟调仓并记账；默认仅预览')
    args = parser.parse_args()
    from market_trading_bot.runner import preview_signals, run_pap
    try:
        result = run_pap(ROOT, ROOT / args.config, args.pap) if args.pap is not None else preview_signals(ROOT, ROOT / args.config)
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str, allow_nan=False))
        return 0
    except Exception as exc:
        print(f'运行失败: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
