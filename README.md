# market-trading-bot

ETF 动量轮动工具，支持实时信号预览和手动模拟调仓。模拟账户使用 SQLite 原子记账，可恢复、可防止同日重复成交，并导出账户交易汇总 CSV。

当前仅实现 `etf_rotation` 策略和 `pap` 模拟模式。定时任务、实盘执行、收盘日报、推送及历史回测尚未实现。

## 快速开始

要求 Python 3.12+。

```bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

预览信号，不下单、不写账户：

```bash
.venv/bin/python main.py
```

执行指定模拟实例并记账：

```bash
.venv/bin/python main.py --pap etf_rotation_pap
```

指定其他策略配置：

```bash
.venv/bin/python main.py --config config/example.json
```

成功时结果以 JSON 输出到 stdout；日志和错误输出到 stderr，失败退出码为 `1`。

## 策略与执行规则

- 动量使用最近 24 个交易日的前复权收盘价和当前价，共 25 个价格点；评分为年化收益乘拟合优度。
- 波动率使用最近 19 个交易日收盘价和当前价形成的 19 个对数收益率，按 250 日年化。
- 选择动量排名第一的 ETF。目标权重为 `目标年化波动率 / 标的年化波动率`，限制在 0% 至 100%。
- 目标数量按调仓前净资产计算，并向下取整到 `lot_size`；先卖后买，现金不足时缩减买入数量。
- 买价加滑点、卖价减滑点并向不利方向取整到 0.001 元；佣金取成交额比例与最低佣金的较大值。
- `t_plus_one=true` 的标的当日买入不可卖，下一交易日解锁。

行情来自 AKShare/东方财富。非交易日、非连续交易时段、未来或过期报价、无效价格和历史数据不足都会停止运行，不会生成模拟成交。

## 配置

全局配置位于 `config/global.json`：

| 配置 | 用途 |
|---|---|
| `market` | 行情源、请求超时与间隔、报价最大延迟、历史回溯天数 |
| `storage.data_dir` | SQLite 账本与 CSV 目录 |
| `logging` | 日志目录、级别和保留日期数 |
| `pushplus` | 预留配置，当前不发送推送 |

策略配置位于 `config/etf_rotation_strategy.json`：

| 配置 | 用途 |
|---|---|
| `instances` | 模拟实例、开户资金和交易费用 |
| `parameters.etf_pool` | ETF 池 |
| `parameters.momentum_price_points` | 动量价格点数量 |
| `parameters.volatility_price_points` | 波动率价格点数量 |
| `parameters.target_annual_volatility` | 目标年化波动率 |
| `instruments` | 每个标的的交易单位和 T+1 规则 |
| `schedule` | 预留调度时间，当前不会自动执行 |

`initial_cash` 仅在首次开户时使用并保存到账本；后续修改配置不会改变账户基准。PushPlus Token 等凭据不要提交到仓库。

## 账户数据

每个实例的数据保存在 `data/<instance_id>/`：

- `ledger.sqlite3`：权威账本，保存全部运行结果、账户状态、信号、订单和成交。
- `account_trades.csv`：可重建的查看副本，仅记录发生实际成交的日期。

账户更新和当日运行结果在同一 SQLite 事务中提交。同一实例同一北京时间日期只提交一次；并发或重复运行会返回首次结果。CSV 导出失败不会回滚已提交账本，再次运行可补齐导出。

请备份整个实例目录，不要通过修改 CSV 调整账户。更换 ETF 池前必须先处理已有持仓，否则缺少持仓行情时运行会停止。

### CSV 字段

| 字段 | 含义 |
|---|---|
| `date` | 交易日期 |
| `buy_etf` / `buy_quantity` / `buy_price` | 买入标的、实际数量和成交价 |
| `sell_etf` / `sell_quantity` / `sell_price` | 卖出标的、实际数量和成交价 |
| `commission` | 当次佣金合计 |
| `holding_etf` / `holding_quantity` / `holding_price` | 交易后持仓、数量和估值价 |
| `market_value` / `cash` / `total_assets` | 持仓市值、现金和总资产 |
| `initial_cash` | 实际开户资金 |
| `cumulative_pnl` | 总资产减开户资金 |
| `cumulative_return_pct` | 累计收益率，单位为百分比 |
| `net_value` | 总资产除以开户资金 |

多笔成交或持仓使用分号按顺序对齐。CSV 的资产值采用调仓时估值，且不记录无成交日期，因此不是每日净值曲线。完整明细以 SQLite 为准。

## 测试

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
```

测试使用临时账本和模拟行情，不访问外部行情或正式账户。

## 日志

日志写入 `logs/<strategy>/YYYY-MM-DD.log`，默认保留最近 30 个有日志的日期。日志包含运行状态、行情区间、报价、策略排名、目标权重和模拟成交摘要，不记录 Token。

## 已知限制

- 模拟成交不校验盘口深度、停牌、涨跌停、分红或拆合份，也不代表实盘成交能力。
- 当前不支持出入金、日内追加调仓、多策略共享账户或自动重试行情请求。
- 免费行情可能延迟或限流；前复权历史会随数据源更新，不适合作为可复现回测数据。

交易规则参考：[上交所黄金 ETF 指引](https://www.sse.com.cn/lawandrules/sselawsrules2025/fund/trading/c/c_20250519_10779388.shtml)、[上交所跨境 ETF 说明](https://edu.sse.com.cn/etfgame/gametopic/c/4940398.shtml)、[深交所场内基金交易指南](https://investor.szse.cn/warning/activities/fundIntroduction/t20210104_584079.html)。新增标的前需核实并配置其交易规则。
