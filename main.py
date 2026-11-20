"""从仓库任意工作目录运行信号预览，不提交交易或写入账户。"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'src'))


def main() -> int:
    """运行信号预览并输出 JSON；成功返回 0，失败输出错误并返回 1。"""
    parser = argparse.ArgumentParser(description='ETF策略信号预览（不交易）')
    parser.add_argument('--config', default='config/etf_rotation_strategy.json', help='策略JSON路径，相对仓库根目录')
    args = parser.parse_args()
    from market_trading_bot.runner import preview
    try:
        result = preview(ROOT, ROOT / args.config)
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str, allow_nan=False))
        return 0
    except Exception as exc:
        print(f'信号预览失败: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
