# market-trading-bot

ETF 动量轮动工具，支持实时信号预览、手动模拟调仓和网页运行监控。模拟账户使用 SQLite 原子记账，可恢复、可防止同日重复成交，并导出账户交易汇总 CSV。

当前仅实现 `etf_rotation` 策略和 `pap` 模拟模式。定时任务、收盘日报和推送尚未实现。

## 策略简介

目标运行节奏是每个 A 股交易日 **14:30 调仓，15:15 生成并推送收盘日报**；定时任务接入前需手动执行。

策略每天对 ETF 池计算动量和波动率：用最近 24 个交易日收盘价加当前价做回归，以“年化收益 × 拟合优度”作为动量得分，选择得分最高的 ETF；再按“15% 目标年化波动率 ÷ 标的波动率”确定仓位，最高 100%。执行时按调仓前净资产计算目标数量，先卖后买，并应用交易单位、T+1、佣金、滑点和现金约束。

行情无效、过期或数据不足时停止运行，不生成模拟成交。

## 代码结构

```text
market-trading-bot/
├── main.py                         # 命令行入口
├── config/
│   ├── global.json                 # 行情、存储和日志配置
│   └── etf_rotation_strategy.json  # 策略、实例和交易规则
├── src/market_trading_bot/
│   ├── strategies/
│   │   └── ETFRotationStrategy.py  # ETF 动量轮动策略
│   ├── AKShareMarket.py            # 行情与交易日历
│   ├── PapExecutor.py              # 模拟成交
│   ├── PapAccountStore.py          # SQLite 账本与 CSV 导出
│   ├── StrategyLogger.py           # 运行日志
│   ├── models.py                   # 领域数据结构
│   ├── report.py                   # 预留日报模块
│   └── runner.py                   # 运行流程协调
├── tests/                          # 离线测试
├── data/                           # 各实例账户数据
├── reports/                        # 预留报告目录
└── requirements.txt                # Python 依赖
```

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

## 配置

全局配置位于 `config/global.json`：

| 配置 | 用途 |
|---|---|
| `market` | 行情源、请求超时与间隔、报价最大延迟、历史回溯天数 |
| `storage.data_dir` | SQLite 账本与 CSV 目录 |
| `logging` | 日志目录、级别和保留日期数 |
| `pushplus` | 预留配置，当前不发送推送 |

策略配置位于 `config/etf_rotation_strategy.json`：

| 配置 | 默认值 | 用途 |
|---|---|---|
| `strategy` | `etf_rotation` | 策略类型 |
| `instances` | `etf_rotation_pap`；开户资金 100,000 元；佣金率 0.02%；最低佣金 5 元；滑点 0 | 模拟账户与交易费用 |
| `parameters.etf_pool` | `518880.XSHG`、`513100.XSHG`、`159915.XSHE`、`510180.XSHG` | ETF 池 |
| `parameters.momentum_price_points` | `25` | 动量价格点数量 |
| `parameters.volatility_price_points` | `20` | 波动率价格点数量 |
| `parameters.target_annual_volatility` | `0.15` | 目标年化波动率 15% |
| `parameters.annualization_days` | `250` | 年化交易日数量 |
| `parameters.select_num` | `1` | 选择排名第一的 ETF |
| `instruments` | 每手 100 份；黄金 ETF、纳指 ETF 为 T+0，创业板 ETF、上证 180 ETF 为 T+1 | 交易单位和卖出规则 |
| `schedule.trade_time` | `14:30` | 调仓时间，当前需手动执行 |
| `schedule.report_time` | `15:15` | 收盘日报时间，当前尚未实现 |

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

## 日志

日志写入 `logs/<strategy>/YYYY-MM-DD.log`，默认保留最近 30 个有日志的日期。日志包含运行状态、行情区间、报价、策略排名、目标权重和模拟成交摘要，不记录 Token。

## 网页运行监控（第一版）

Vue 3 前端 + FastAPI 后端，支持运行状态、策略与账户概览、按日期查看和刷新日志。
账户展示最近调仓估值，非实时估值；暂不支持定时调度和日报。

### 端口与启动

以下命令从项目根目录执行，需要 Python 3.12+、Node.js 20.19+ 或 22.12+。

首次安装：

```bash
.venv/bin/python -m pip install -r requirements.txt
npm ci --prefix frontend
```

开发时分别在两个终端启动：

```bash
# 后端：8001，自动启动后台交易线程
.venv/bin/python -m uvicorn market_trading_bot.web.app:app --app-dir src --host 127.0.0.1 --port 8001

# 前端：8000，浏览器打开 http://127.0.0.1:8000
npm run dev --prefix frontend -- --port 8000 --strictPort
```

前端将 `/api` 请求代理到后端 `8001`。后台线程待命时每 5 秒更新心跳，执行交易时暂停心跳，完成后记录结果并恢复心跳；线程随服务启停。
待命超过 20 秒未更新心跳判定失联；交易执行超时由 `runtime.trade_timeout_seconds` 配置，默认 300 秒。
超时只标记异常，不会强制中断或重试交易。
后端仅启动一个实例（不要增加 `--workers`）；启动和重启不会自动调仓，手动调仓沿用 `main.py --pap etf_rotation_pap`。

部署时执行 `npm run build --prefix frontend`，只需启动一个后端服务，
通过 `http://127.0.0.1:8001` 同时访问页面与 API，无需启动前端开发服务。

### API

以下均为只读 `GET` 接口：

| 路径 | 功能 |
|---|---|
| `/api/runtime` | 后台交易线程启动时间、运行时长与心跳 |
| `/api/strategies` | 策略与账户实例列表 |
| `/api/accounts/{instance_id}` | 最近账户资产与持仓 |
| `/api/strategies/{strategy_id}/logs` | 日志日期列表，按日期倒序 |
| `/api/strategies/{strategy_id}/logs/{date}?offset=0` | 指定日期日志，返回内容及下一页偏移量 |

日志日期格式为 `YYYY-MM-DD`；日志按策略归档。手动调仓结果通过账户与日志查看。后台线程提供内部 `submit_trade()` 入口，供后续调度接入，页面仍为只读。
