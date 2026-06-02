# TODO：新闻窗口 — 仅拦截开仓

> 状态：**未实施**（方案已定，有空再做）  
> 目标：分钟策略不识别新闻时，避免在 prop 新闻窗口内**新开仓**；平仓/止损逻辑保持不动。

---

## 已定原则

1. **只在开仓路径判断**  
   - 满足入场条件、`apply_er5_gate_to_signal` 通过之后、调用 `submit_order` **开新仓之前** 检查。  
   - **不**在平仓、`FORCE_CLOSE`、日内止损/止盈平仓等路径加限制（与 The5ers 等「持仓可过新闻、禁止窗口内下单」一致）。  
   - 参考位置（各 `simulate_*.py` 相同）：`signal != 0` 分支里、`submit_order(symbol, side, ...)` 开仓那一行（约 1510–1515 行附近）。

2. **数据源：免费 + 简单（推荐两阶段）**  
   - **Phase 1（默认）**：根目录 `news_events_usd.json`，每周从 [Forex Factory](https://www.forexfactory.com/calendar) 手抄 **High + USD** 红事件（NFP、CPI、FOMC、GDP、失业率等）。时间与 `get_us_eastern_time()` 一致，用 **US/Eastern**。  
   - **Phase 2（可选）**：`fetch_news_calendar.py` 用 [Finnhub](https://finnhub.io/) 免费 Economic Calendar API（需 `FINNHUB_API_KEY`），每日写回同一 JSON；**运行时 simulate 只读 JSON，不联网**。

3. **共享模块**（仿 `trend_er5_gate.py`）  
   - 新增 `news_blackout.py`：`load_events()`、`is_in_news_blackout(now_et, before_min, after_min) -> bool`  
   - 各 `simulate_*.py` 用户配置区增加开关与缓冲（不改 EA、不改 `write_signal` 全局逻辑）。

---

## 各平台建议缓冲（分钟，在规则 ±2/±5 上略加余量）

| 脚本 | 考核期建议 | `BEFORE` / `AFTER` | 备注 |
|------|------------|-------------------|------|
| `simulate_blueberry.py` | 开启 | 3 / 3 | 规则 ±2，最严 |
| `simulate_the5ers.py` | 开启 | 3 / 3 | High Stakes ±2，soft 扣利润 |
| `simulate_goat.py` | 开启 | 6 / 6 | 规则 ±5，只扣超额利润 |
| `simulate_ftmo.py` | 考核可关 | 3 / 3 | Challenge 无新闻限；通过后 Standard 再开 |
| `simulate_fundednext.py` | 考核可关 | 6 / 6 | Challenge 无规则；Stellar Funded ±5 |
| `simulate_ttp.py` | 按账户规模 | 3 / 3 | $100K/$200K CFD 才限 |
| `simulate_icmarkets.py` | 按需 | — | 非 prop 考核可不开 |

配置示例（实施时写入各 simulate 顶部配置区）：

```python
NEWS_BLACKOUT_ENABLED = True
NEWS_BLACKOUT_BEFORE_MIN = 3
NEWS_BLACKOUT_AFTER_MIN = 3
```

开仓前（示意）：

```python
from news_blackout import is_in_news_blackout
# ... signal = apply_er5_gate_to_signal(...)
if signal != 0:
    if NEWS_BLACKOUT_ENABLED and is_in_news_blackout(
        get_us_eastern_time(), NEWS_BLACKOUT_BEFORE_MIN, NEWS_BLACKOUT_AFTER_MIN
    ):
        print(f"[{now}] 新闻窗口，跳过开仓")
    else:
        order_id = submit_order(...)
```

---

## 实施清单（完成后勾选）

- [ ] 新增 `news_blackout.py`
- [ ] 新增 `news_events_usd.json`（模板 + 本周事件）
- [ ] （可选）新增 `fetch_news_calendar.py` + `.env` 说明 `FINNHUB_API_KEY`
- [ ] `simulate_blueberry.py` — 开仓门禁 + 配置
- [ ] `simulate_the5ers.py` — 同上
- [ ] `simulate_goat.py` — 同上（6/6 缓冲）
- [ ] `simulate_ftmo.py` — 同上（或考核期 `ENABLED=False`）
- [ ] `simulate_fundednext.py` — 同上
- [ ] `simulate_ttp.py` — 同上
- [ ] `simulate_icmarkets.py` — 按需
- [ ] 日志确认：新闻窗内出现「跳过开仓」且无新 BUY 信号进 DB
- [ ] 文档：在 `AGENTS.md` 补一句维护 JSON / 跑 fetch 脚本频率

---

## 已知限制（接受即可）

- Python 只挡**策略发出的开仓信号**；MT5 上已有仓位的 **SL/TP 在新闻瞬间触发** 仍可能发生（Blueberry 等会算违规）。二期若要控，再考虑 EA 读同一 JSON 仅禁新开仓。  
- 7 个 `simulate_*.py` 开仓处各加一段；长期可抽公共函数，非首期目标。

---

## 参考链接

- The5ers：https://help.the5ers.com/is-news-trading-allowed-in-the-high-stakes-program/
- Blueberry：https://help.blueberryfunded.com/en/articles/9715375-do-you-allow-news-trading
- Goat：https://help.goatfundedtrader.com/en/articles/10742084-is-news-trading-allowed
- FTMO：https://ftmo.com/en/faq/can-i-trade-news/
- FundedNext：https://help.fundednext.com/en/articles/10701447-is-news-trading-allowed-at-fundednext
