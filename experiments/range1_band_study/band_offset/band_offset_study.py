"""
噪声边界补偿：边界 = 参考价 × (1 ± (a × sigma + b))，a = K1 = K2（sigma 缩放），b = sigma_offset（固定加宽）。
其余参数 = 当前实盘 R 版（er5≥0.1、range1≤4%、sigma≥0.0003、追踪止盈 0.6%/65%、开盘冷清半仓），1x 杠杆、无日内止损。
只用 2024-12-31 及之前的数据（2025-01-01 起留作最终验证，本脚本不读取）。
数据：Quantra/reports/overfit_work/qqq_2004_2026.csv（Quantra experiments/overfit_study.py 生成；2024 前旧数据已按比值缩放 + longport），按 3 年切块、带 200 天预热。
成本：零成本回测后按名义金额每边 0.25bp 扣除（与 Quantra overfit_study 一致）。
用法：python3 experiments/range1_band_study/band_offset/band_offset_study.py run [--workers 4]   # 可中断续跑，缓存 band_offset/output/results.pkl
      python3 experiments/range1_band_study/band_offset/band_offset_study.py report
"""
import argparse
import contextlib
import io
import os
import pickle
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path

def _find_roots(here):
    """向上找含 backtest.py 的仓库根目录；Quantra 若不是当前仓库，则取同级 ../Quantra。"""
    root = next(p for p in here.parents if (p / "backtest.py").exists())
    quantra = root if (root / "experiments" / "overfit_study.py").exists() else root.parent / "Quantra"
    return root, quantra


HERE = Path(__file__).resolve().parent
ROOT, QUANTRA = _find_roots(HERE)
os.chdir(ROOT)
sys.path.insert(0, str(HERE))
sys.path.append(str(ROOT))  # plot_trading_day 等依赖仍从仓库根目录导入
os.environ.setdefault("MPLBACKEND", "Agg")

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

SRC = QUANTRA / "reports" / "overfit_work" / "qqq_2004_2026.csv"
WORK = HERE / "output"
CACHE = WORK / "results.pkl"
REPORT = WORK / "report.txt"
TRAIN_END = date(2024, 12, 31)  # 2025 起为留出数据，严禁读取
CHUNK_STARTS = [date(y, 1, 1) for y in range(2005, 2025, 3)]  # 2005,2008,...,2023
COST_PER_SIDE = 0.000025

FILTER_ON = [{"metric": "er5", "min": 0.1}, {"metric": "range1", "max": 0.04}, {"metric": "sigma", "min": 0.0003}]
FILTER_NO_RANGE1 = [{"metric": "er5", "min": 0.1}, {"metric": "sigma", "min": 0.0003}]
# 只测少数几组有含义的点，不做细网格（避免调参过拟合）；7 组 × 7 块 = 49 个任务
POINTS = [
    (1.0, 0.0, True),     # 现行实盘
    (1.0, 0.002, True),   # 只加固定宽度
    (0.8, 0.0, True),     # 只缩放 sigma
    (0.8, 0.002, True),   # 缩放 + 加宽（提议）
    (0.8, 0.004, True),   # 缩放 + 加更宽
    (1.0, 0.0, False),    # 现行去掉 range1
    (0.8, 0.002, False),  # 提议去掉 range1：看加宽能否替代 range1
]
# 第一次细网格中途停掉前已跑完的点，缓存里现成，不再新增任务
EXTRA_CACHED = [(0.6, 0.0, True), (0.6, 0.001, True), (0.6, 0.002, True)]


def configs():
    out = {}
    for a, b, r1 in POINTS + EXTRA_CACHED:
        out[f"a={a}|b={b}|{'range1' if r1 else 'no_range1'}"] = {
            "K1": a, "K2": a, "sigma_offset": b, "entry_trend_filter": FILTER_ON if r1 else FILTER_NO_RANGE1}
    return out


def base_config(path, start, end):
    return dict(
        data_path=path, ticker="QQQ", initial_capital=100000.0, lookback_days=1, start_date=start, end_date=end,
        check_interval_minutes=15, enable_transaction_fees=False, slippage_per_share=0.0,
        trading_start_time=(9, 40), trading_end_time=(15, 40), max_positions_per_day=10,
        print_daily_trades=False, print_trade_details=False, show_equity_report=False, leverage=1,
        enable_leverage_liquidation_monitor=False, enable_intraday_stop_loss=False, enable_per_trade_stop_loss=False,
        enable_k_side_adjustment=False, use_vwap=False,
        quiet_open_half_position=True, quiet_open_rel_threshold=0.65, quiet_open_lookback_days=20, quiet_open_position_mult=0.5,
        enable_trailing_take_profit=True, trailing_tp_activation_pct=0.006, trailing_tp_callback_pct=0.65,
        stop_entries_after_trailing_tp=False,
    )


def make_chunks():
    WORK.mkdir(parents=True, exist_ok=True)
    chunks = []
    need = [s for s in CHUNK_STARTS if not (WORK / f"chunk_{s.year}.csv").exists()]
    df = None
    if need:
        df = pd.read_csv(SRC, parse_dates=["DateTime"])
        df = df[df["DateTime"] < pd.Timestamp(TRAIN_END + timedelta(days=1))]
    for i, s in enumerate(CHUNK_STARTS):
        e = CHUNK_STARTS[i + 1] - timedelta(days=1) if i + 1 < len(CHUNK_STARTS) else TRAIN_END
        p = WORK / f"chunk_{s.year}.csv"
        if not p.exists():
            part = df[(df["DateTime"] >= pd.Timestamp(s - timedelta(days=200))) & (df["DateTime"] < pd.Timestamp(e + timedelta(days=1)))]
            part.to_csv(p, index=False)
        chunks.append((s, e, str(p)))
    return chunks


def run_task(name, cfg, chunk):
    from backtest_offset import run_backtest
    s, e, path = chunk
    config = {**base_config(path, s, e), **cfg}
    t0 = time.time()
    with contextlib.redirect_stdout(io.StringIO()):
        daily, _m, trades, _ = run_backtest(config)
    gross = daily["daily_return"].astype(float)
    gross.index = pd.to_datetime(gross.index)
    start_cap = daily["capital"].astype(float) / (1 + gross.values)
    if len(trades):
        t = trades.copy()
        t["d"] = pd.to_datetime(t["entry_time"]).dt.normalize()
        notional = (t["position_size"] * (t["entry_price"] + t["exit_price"])).groupby(t["d"]).sum().reindex(gross.index, fill_value=0.0)
        t = t[["d", "entry_time", "side", "entry_price", "exit_price", "exit_reason"]]
    else:
        notional = pd.Series(0.0, index=gross.index)
        t = pd.DataFrame()
    return name, str(s), gross - notional / start_cap * COST_PER_SIDE, t, time.time() - t0


def cmd_run(workers):
    chunks = make_chunks()
    cache = pickle.loads(CACHE.read_bytes()) if CACHE.exists() else {}
    todo = [(n, c, ch) for n, c in configs().items() for ch in chunks if (n, str(ch[0])) not in cache]
    print(f"待跑 {len(todo)} 个任务，{workers} 进程", flush=True)
    t0 = time.time()
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(run_task, *x) for x in todo]
        for i, fut in enumerate(as_completed(futs), 1):
            n, s, r, t, secs = fut.result()
            cache[(n, s)] = {"ret": r, "trades": t}
            if i % 4 == 0 or i == len(todo):
                CACHE.write_bytes(pickle.dumps(cache))
                print(f"[{i}/{len(todo)}] {secs:.0f}s/块 累计 {time.time() - t0:.0f}s", flush=True)


def load():
    cache = pickle.loads(CACHE.read_bytes())
    out = {}
    for n in configs():
        parts = [cache[(n, str(s))] for s in CHUNK_STARTS if (n, str(s)) in cache]
        if len(parts) < len(CHUNK_STARTS):
            continue
        r = pd.concat([p["ret"] for p in parts]).sort_index()
        r = r[~r.index.duplicated()]
        t = pd.concat([p["trades"] for p in parts if len(p["trades"])], ignore_index=True)
        out[n] = (r, t)
    return out


def stats(r):
    r = r.dropna()
    eq = (1 + r).cumprod()
    yrs = len(r) / 252
    sd = r.std()
    mdd = (1 - eq / eq.cummax()).max()
    cagr = eq.iloc[-1] ** (1 / yrs) - 1
    return dict(cagr=cagr, sharpe=r.mean() / sd * np.sqrt(252) if sd > 0 else np.nan, mdd=mdd)


WINDOWS = [("2005-09", "2005-01-01", "2009-12-31"), ("2010-14", "2010-01-01", "2014-12-31"),
           ("2015-19", "2015-01-01", "2019-12-31"), ("2020-24", "2020-01-01", "2024-12-31"), ("全部", "2005-01-01", "2024-12-31")]


def cmd_report():
    res = load()
    out = []
    p = out.append
    p(f"训练数据 2005-01 ~ 2024-12（2025 起未读取）。已完成配置 {len(res)}/{len(configs())}")
    rows = []
    for n, (r, t) in res.items():
        row = {"cfg": n}
        for w, a, b in WINDOWS:
            st = stats(r[(r.index >= a) & (r.index <= b)])
            row[f"夏普{w}"] = st["sharpe"]
            if w == "全部":
                row["年化"], row["回撤"] = st["cagr"], st["mdd"]
        row["笔/年"] = len(t) / 20
        if len(t):
            et = pd.to_datetime(t["entry_time"])
            first = et.groupby(t["d"]).min()
            row["首笔≤10:00占比"] = ((first.dt.hour * 60 + first.dt.minute) <= 600).mean()
            row["有交易天占比"] = len(first) / len(r)
        rows.append(row)
    df = pd.DataFrame(rows).set_index("cfg")
    pd.set_option("display.width", 250)
    pd.set_option("display.max_columns", 30)
    p("\n=== 各配置（按配置顺序；a=sigma 缩放，b=固定加宽） ===")
    order = [n for n in configs() if n in df.index]
    p(df.loc[order].to_string(float_format=lambda x: f"{x:.3f}"))
    text = "\n".join(out)
    REPORT.write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["run", "report"])
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()
    cmd_run(a.workers) if a.cmd == "run" else cmd_report()
