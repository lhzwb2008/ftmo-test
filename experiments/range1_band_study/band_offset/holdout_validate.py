"""
最终对比：现行实盘（a=1, b=0）vs 训练期选出的组合（a=0.8, b=0.2%），都带 er5/range1≤4%/sigma 门控。
训练期 2005-2024 结果读 output/results.pkl（band_offset_study.py 已跑）；留出期 2025-01-02 ~ 2026-11-20 在这里只跑一次。
留出数据 = Quantra qqq_2004_2026.csv 的 2024-06 起部分 + ../data/server_tail_20261019_20261120.csv（服务器分钟缓存，补到 11-20）。
用法：python3 experiments/range1_band_study/band_offset/holdout_validate.py
"""
import os
import pickle
import sys
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import band_offset_study as bos  # noqa: E402  （会 chdir 到仓库根目录）

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

HOLD_START, HOLD_END = date(2025, 1, 1), date(2026, 11, 20)
HOLD_CSV = HERE / "output" / "holdout_2024h2_20261120.csv"
HOLD_CACHE = HERE / "output" / "holdout.pkl"
OUT = HERE / "output" / "holdout_report.txt"
CFGS = {
    "现行 a=1,b=0": "a=1.0|b=0.0|range1",
    "新 a=0.8,b=0.2%": "a=0.8|b=0.002|range1",
}


def build_holdout_csv():
    if HOLD_CSV.exists():
        return
    m = pd.read_csv(bos.SRC, parse_dates=["DateTime"])
    m = m[m["DateTime"] >= "2024-06-01"]
    t = pd.read_csv(HERE.parent / "data" / "server_tail_20261019_20261120.csv", parse_dates=["DateTime"])
    a = pd.concat([m, t[["DateTime", "Open", "High", "Low", "Close", "Volume"]]])
    a = a.drop_duplicates("DateTime", keep="first").sort_values("DateTime")
    a = a[a["DateTime"] < pd.Timestamp(HOLD_END) + pd.Timedelta(days=1)]
    a.to_csv(HOLD_CSV, index=False)


def run_holdout():
    if HOLD_CACHE.exists():
        return pickle.loads(HOLD_CACHE.read_bytes())
    build_holdout_csv()
    allc = bos.configs()
    res = {}
    for label, key in CFGS.items():
        _n, _s, r, t, secs = bos.run_task(key, allc[key], (HOLD_START, HOLD_END, str(HOLD_CSV)))
        res[key] = (r, t)
        print(f"留出期 {label} 跑完 {secs:.0f}s", flush=True)
    HOLD_CACHE.write_bytes(pickle.dumps(res))
    return res


def trade_stats(t):
    if len(t) == 0:
        return dict(n=0, win=np.nan, avg=np.nan)
    ret = (t["exit_price"] / t["entry_price"] - 1) * np.where(t["side"] == "Long", 1, -1) - 2 * bos.COST_PER_SIDE
    return dict(n=len(t), win=(ret > 0).mean(), avg=ret.mean() * 1e4)


def row(r, t, a, b):
    w = (r.index >= a) & (r.index <= b)
    rr = r[w]
    tt = t[(pd.to_datetime(t["d"]) >= a) & (pd.to_datetime(t["d"]) <= b)] if len(t) else t
    st = bos.stats(rr)
    ts = trade_stats(tt)
    yrs = len(rr) / 252
    first = pd.to_datetime(tt["entry_time"]).groupby(tt["d"]).min() if len(tt) else pd.Series(dtype="datetime64[ns]")
    early = ((first.dt.hour * 60 + first.dt.minute) <= 600).mean() if len(first) else np.nan
    return dict(总收益=(1 + rr).prod() - 1, 年化=st["cagr"], 夏普=st["sharpe"], 最大回撤=st["mdd"],
                笔每年=ts["n"] / yrs, 胜率=ts["win"], 均笔bp=ts["avg"], 首笔10点前=early)


def main():
    train = bos.load()
    hold = run_holdout()
    periods = [
        ("训练 2005-2009", "2005-01-01", "2009-12-31", "train"),
        ("训练 2010-2014", "2010-01-01", "2014-12-31", "train"),
        ("训练 2015-2019", "2015-01-01", "2019-12-31", "train"),
        ("训练 2020-2024", "2020-01-01", "2024-12-31", "train"),
        ("训练 2005-2024", "2005-01-01", "2024-12-31", "train"),
        ("留出 2025", "2025-01-01", "2025-12-31", "hold"),
        ("留出 2026-01~11-20", "2026-01-01", "2026-11-20", "hold"),
        ("留出 2025-01~2026-11-20", "2025-01-01", "2026-11-20", "hold"),
    ]
    rows = []
    for pname, a, b, src in periods:
        for label, key in CFGS.items():
            r, t = (train if src == "train" else hold)[key]
            rows.append({"时段": pname, "配置": label, **row(r, t, a, b)})
    df = pd.DataFrame(rows).set_index(["时段", "配置"])
    fmt = {"总收益": "{:.1%}", "年化": "{:.1%}", "夏普": "{:.2f}", "最大回撤": "{:.1%}", "笔每年": "{:.0f}",
           "胜率": "{:.1%}", "均笔bp": "{:.1f}", "首笔10点前": "{:.0%}"}
    show = df.copy()
    for c, f in fmt.items():
        show[c] = show[c].map(lambda x, f=f: f.format(x) if pd.notna(x) else "-")
    lines = ["1x 杠杆、按名义金额每边 0.25bp 扣成本、无日内止损；两组都带 er5≥0.1 / range1≤4% / sigma≥0.0003 门控", show.to_string()]
    # 留出期按月
    rc, _ = hold[CFGS["现行 a=1,b=0"]]
    rn, _ = hold[CFGS["新 a=0.8,b=0.2%"]]
    mon = pd.DataFrame({"现行": (1 + rc).groupby(rc.index.to_period("M")).prod() - 1,
                        "新": (1 + rn).groupby(rn.index.to_period("M")).prod() - 1})
    mon["差"] = mon["新"] - mon["现行"]
    lines += ["\n留出期按月收益", mon.to_string(float_format=lambda x: f"{x:+.2%}"),
              f"新配置好于现行的月份 {(mon['差'] > 0).sum()}/{len(mon)}"]
    text = "\n".join(lines)
    OUT.write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
