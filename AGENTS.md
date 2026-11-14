# AGENTS.md

## Cursor Cloud specific instructions

### Overview

This is a Python-based algorithmic trading system for QQQ (US ETF). It uses a two-tier signal pipeline:

1. **Python Signal Generators** (`simulate_*.py`) — connect to the Longport OpenAPI for market data, compute trading signals (BUY/SELL/CLOSE), and write them to per-broker SQLite databases.
2. **MetaTrader 5 Expert Advisors** (`SQLiteSignalEA_*.mq5`) — read signals from SQLite and execute trades (Windows-only, cannot run on Linux).

### Dependencies

Install with: `pip install -r requirements.txt`

Required packages: `pandas`, `numpy`, `pytz`, `python-dotenv`, `longport`, `ib_insync`（仅 `simulate_ibkr.py`）

### Linting

```
flake8 --max-line-length=200 *.py
```

The codebase has existing style issues (whitespace, unused imports). Use `--max-line-length=200` since the code uses long lines.

### Running

- **Standalone utility scripts** (`check_db.py`, `test_three_db_signals.py`, `clear_db_data.py`, `delete_db.py`) work without any API credentials.
- **Trading simulators** (`simulate_ftmo.py`, `simulate_fundednext.py`, `simulate_the5ers.py`, `simulate_icmarkets.py`, `simulate_ttp.py`, `simulate_blueberry.py`, `simulate_goat.py`, `simulate_aqua.py`, `simulate_aqua_paylater.py`, `simulate_wsfunded.py`, `simulate_the5ers_summer.py`) require Longport API credentials via a `.env` file or environment variables:
  - `LONGPORT_APP_KEY`
  - `LONGPORT_APP_SECRET`
  - `LONGPORT_ACCESS_TOKEN`
- **IBKR Gateway 直连** (`simulate_ibkr.py`)：Longport QQQ 信号 + `ib_insync` 交易 `MNQ`（Micro Nasdaq-100 期货，非 IBUST100 CFD）；开仓按 IBKR **日内**初始保证金满仓（使用 100% 净值；净值必须高于当日官表日内初始才开，否则跳过，每个交易日开盘打一行；Error 201 则按 90%/85%/70% 减张重试；官表拉不到不用估算值下单；收盘前平仓，不要按隔夜 12% 估）。已切实盘（Live 端口 4001），盈亏基数写死 `$4,500`（`START_CAPITAL`）；仓位仍按 Gateway 实时净值。同步请求设 `IB_REQUEST_TIMEOUT_SEC=15`，避免 Gateway 23:45 自动重启时 `reqPositions` 永久卡死。需本机已登录 IB Gateway 且有 CME/MNQ 权限。合约月默认每个交易日自动选主力（`MNQ_EXPIRY=''`，到期前 `MNQ_ROLL_DAYS_BEFORE_EXPIRY=8` 天切下一季，账户仍持有旧月仓位时不切换）；`MNQ_EXPIRY` 填 YYYYMM 则固定该月。IB 连接参数写死在脚本常量（当前 `127.0.0.1:4001` Live；Paper 用 `4002`）；`.env` 仅需 Longport 凭证。不写 SQLite、不跑 MT5。
- **Data fetcher** (`data_fetch_from_longport.py`) also requires Longport credentials.
- **期货 prop（Windows + NinjaTrader 8 ATI）**：共用 `simulate_futures_core.py`（`NT8_INSTRUMENT` 每季度换月手改）。`simulate_futures_fundednext.py` = FundedNext Flex（挑战/funded 张数：50K 2/2 + 挑战日盈帽 $900，100K 3/3，150K 3/2）；`simulate_futures_lucid.py` = LucidFlex 50K（启动时选日亏关=3 张 / 开=1 张，挑战日盈帽 $1,080）。Tradeify 禁止同一 bot 跨公司使用，已弃用并删除入口（research 里的 Tradeify 方案只做对比）。测算结果与要买的产品写在各入口文件顶部注释。
- **测算 / 研究脚本**在 `research/`（prop 出金测算、期货 prop 测算、FTMO+IBKR 组合回测等），从仓库根目录运行 `python3 research/xxx.py`；脚本会自动把工作目录切到仓库根目录，数据和输出仍在 `reports/`。

### Strategy parameters

- 策略参数（K1/K2、回看天数、VWAP、追踪止盈、er5/range1/sigma 门控、开盘冷清半仓）统一在 `strategy_params.py`，所有 `simulate_*.py` 与 `trend_er5_gate.py` 从这里读取；与 Quantra `backtest.py` 默认 config 一致。账户相关参数（杠杆、资金、风控比例、交易时间）仍在各 simulate 中。
- 上一版实盘（K2=1.04、午后 K1=0.9、range1<=0.029、无冷清半仓）见 git tag `live-G-20260718`。
- 开盘冷清半仓（`quiet_open.py`）：当日 09:30–09:39 成交量 / 前 20 个交易日同窗口均值 < 0.65 时，当日开仓手数 ×0.5。MT5 路径写入 `signals.size_mult`，EA 在 `CalculateLotSize()` 结果上乘系数并按步长向下取整（不低于最小手数）；`simulate.py` / `simulate_ibkr.py` / `simulate_futures_core.py` 在 Python 端缩小数量（期货 1 张时保持 1 张）。
- 分钟历史窗口 35 个自然日（`history_days_back`），`longport_data_service.py` 每天首轮及每 30 分钟拉一次完整窗口，其余轮次只拉当日。
- 账户初始资金：simulate 启动时把输入的"账户起始资金"写入信号库 `sim_config` 表（key=`account_start_balance`），EA 在 OnInit 及之后每 30 秒读取，用于日亏限额 / 硬止损 / 静态回撤；读不到时回退 EA input `InitialBalance` / `AccountInitialBalance`（Aqua 2-Step EA 除外，见下）。启动日志出现"🔗 账户初始资金已从 simulate 同步"即生效。已开仓位的保护性 SL 只收紧不放松，初始资金调大后对当前持仓不会放宽。
- **免配置（FundedNext、WSFunded、FTMO、Aqua、Aqua Pay Later、Blueberry、GOAT、The5ers、TTP、Summer）**：EA inputs 保持默认。杠杆、起始资金、绑定登录号、单笔硬止损、日亏比例由 simulate 写入 `sim_config`，EA 每几秒重读。simulate 启动先等 EA 心跳，余额和登录号从心跳读；按登录号记在 `sim_profile`。心跳里的 EA 版本不是当前脚本对应的版本时，启动会提醒重新编译并替换 EA。正在交易的旧账户换上新 EA 前，先在非交易时段重载。
  - EA inputs 全部保持默认。杠杆、起始资金、绑定登录号（`account_login`）、FundedNext 单笔硬止损（funded=2.5 / 考核=0；WSFunded 由 EA 按 Ultra 规则自算，不下发）都由 simulate 写入 `sim_config`，EA 每 3~5 秒重读。配置缺项或登录号与终端不一致时 EA 拒绝 BUY/SELL（CLOSE 照常执行）。
  - EA 可以先于 simulate 加载（库不存在会自动建）。EA 用 `OnTimer`（5 秒）写心跳表 `ea_account`（登录号、余额、净值、MT5 第一笔入金、`config_ready`，`updated_at` 为 GMT），周末无 tick 也会写。
  - simulate 启动先等 EA 心跳，余额和登录号从心跳读，不再手输；只问轮次、起始资金（默认 = 第一笔入金）、杠杆、开仓时间，按登录号记在 `sim_profile`，同一账户再启动全部回车即可。写完配置后等 EA 心跳回报 `config_ready`，30 秒没确认只告警。
  - 日内锚点 GV 名带登录号（`DLS_<Magic>_<login>_*`），换账户自动重新起锚；当日出入金（BALANCE/CREDIT 等 deal）30 秒内自动重新起锚，不再需要 `ForceResetDailyRisk`。升级时 GV 名变化会让锚点按当前权益重设，请在非交易时段重载 EA。
  - 考核通过：simulate 把官方通过线（起始资金 × 目标比例，无余量）写入 `challenge_pass_equity`（Funded=0）。EA 开仓后给持仓挂券商端止盈，成交后余额 = 官方线 + 平仓手续费（按开仓那笔估）+ `PASS_TP_SLIP_TICKS`=5 个价位滑点；`UpdatePassTP` 每秒校正（差 1.5 个价位以内不改单），离现价不足最小距离时暂不挂，现价已越过止盈价则直接平仓（计入外部平仓序号，simulate 镜像）。仓位被其它方式平掉时止盈随之消失，无需撤单。没有逐 tick 权益兜底。考核止盈成交（`DEAL_REASON_TP`）、越线平仓、或空仓且余额 ≥ 官方线，任一满足即停止开仓（GV `pass_halted` + 表 `ea_pass_status`），不看成交后余额，也不会自动恢复。simulate 每次启动写新的 `challenge_pass_session` 并删掉 `ea_pass_status`，EA 见会话号变化即清除 `pass_halted`，换阶段只需重启 simulate。启动时余额已达官方通过线则直接退出。
- WSFunded Ultra（`simulate_wsfunded.py` / `SQLiteSignalEA_wsfunded.mq5`，`trading_signals_wsfunded.db`，Magic `20261110`）：开仓必须同时挂单笔止损 = min(起始资金, 当日锚点) × 5% × 50% × (1−缓冲)，挂不出就放弃该信号。只接一张账户，禁止复制到其它 WSFunded 账户。
- The5ers Summer Plan 2-Step（`simulate_the5ers_summer.py` / `SQLiteSignalEA_the5ers_summer.mq5`，`trading_signals_the5ers_summer.db`，Magic `20261112`）：基于免配置版（最小手模式之前的 FundedNext），与在跑的 High Stakes `simulate_the5ers.py` 互不影响。启动选方案（10/5 第一轮 10%、8/5 第一轮 8%）和轮次；杠杆默认按测算给考核 3x / Pro 2x（`research/the5ers_summer_study.py`）。启动方案默认 8/5。EA 日亏限额 = min(起始资金, 当日锚点) × 3%，在 95% 处全平并停当日开仓；总亏地板不设保护；单笔硬止损默认不挂（`HARD_SL_PCT`=0，官方不强制止损，要挂改这个常量）。一台机器只跑一张 Summer 账户，New 和 Classic 分机器跑。Pro 出金的 50% 一致性没有自动处理，申请前看后台。
- 部署顺序：`longport_data_service.py` → `simulate_*.py`（启动时重建 signals 表，带 `size_mult` 列）→ 在 MetaEditor 重新编译并重载 EA（Leverage 等 inputs 不变）。新 EA 读旧表时自动回退为满仓，旧 EA 读新表忽略 `size_mult`，顺序颠倒不会出错，只是冷清减仓不生效。

### Key gotchas

- The `.mq5` files are MetaTrader 5 Expert Advisors and **cannot be compiled or run on Linux**. They require a Windows machine with MT5 installed.
- The main simulation scripts (`simulate_*.py`) connect to the Longport API at **module import time** (line 198 in `simulate_ftmo.py`: `QUOTE_CTX, TRADE_CTX = create_contexts()`). This means you cannot import or run them without valid API credentials.
- **Longport access tokens expire**. If you get `401003 token expired`, the token must be regenerated from the [Longport OpenAPI console](https://open.longportapp.com/). The SDK's `config.refresh_access_token()` will also fail if the token is fully expired. Update `LONGPORT_ACCESS_TOKEN` after regeneration.
- SQLite database files (`*.db`) are gitignored.
- All comments and log messages are in Chinese (Simplified Chinese).
- On Linux, SQLite DBs are created in the current working directory. On Windows, they go to the MT5 common files directory.
- **Aqua Standard vs Pay Later**: `simulate_aqua.py` / `SQLiteSignalEA_aqua.mq5` = 2-Step Standard（`trading_signals_aqua_{n}.db`, Magic `20260808+n`）。`simulate_aqua_paylater.py` / `SQLiteSignalEA_aqua_paylater.mq5` = One-Step Pay Later（`trading_signals_aqua_paylater_{n}.db`, Magic `20260818+n`）。两套不可混用。启动先选实例编号，EA 的 `InstanceId` 必须一致。Pay Later 再选 `challenge` 或 `funded`：Challenge 日亏 0、Funded 日亏 3%、两边硬止损 0.95%、Funded 日盈帽默认 $1100，由 EA 按真实权益（权益 − 日锚点）逐 tick 判定并全平，写回 `ea_daily_status.halted=2`（1=日亏线）；simulate 只镜像该标志，不用 QQQ 模拟盈亏自行触发（EA_BUILD `2026-11-14a`）。Aqua 2-Step funded 单笔硬止损 0.9%（Wave Stop 1%），考核不挂。这些都由 simulate 写入 `sim_config`，配置未就绪时 EA 不开仓。
- The MQ5 EAs rely on `OnTick()` to poll the database. If a broker (e.g. IC Markets) delivers ticks late at market open, signals may be delayed. A potential fix is adding `OnTimer()` as a fallback.
- IC Markets EA has a known issue: it frequently stops and restarts (visible in MT5 logs as repeated "EA已停止" → reinitialization cycles). Root cause is under investigation.

### Quick verification

```bash
python3 -c "
from longport.openapi import Config, QuoteContext
ctx = QuoteContext(Config.from_env())
q = ctx.quote(['QQQ.US'])[0]
print(f'QQQ.US = \${q.last_done}')
"
```
