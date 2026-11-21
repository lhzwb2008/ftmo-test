"""
range1（昨日振幅 (High-Low)/Close ≤ 4%）门控：有无对比 + 过拟合判断。
不重跑回测，直接读 Quantra/experiments/overfit_study.py 的缓存（Quantra/reports/overfit_work/results.pkl）：
  - sweep|range1上限|0.04 与 sweep|range1上限|None：除 range1 外参数相同（G 版底座：K2=1.04、午后K1=0.9、er5≥0.1、sigma≥0.0003、
    追踪止盈 0.6%/65%），1 倍杠杆，按名义金额每边 0.25bp 扣成本，2020-01-03 ~ 2026-10-16。
  - 其余 sweep|range1上限|x 用作阈值邻域。
用法：python3 experiments/range1_band_study/range1_gate/range1_gate_study.py   → 打印并写 range1_gate/output/report.txt
"""
import os
import sys
import warnings
from pathlib import Path

def _find_roots(here):
    """向上找含 backtest.py 的仓库根目录；Quantra 若不是当前仓库，则取同级 ../Quantra。"""
    root = next(p for p in here.parents if (p / "backtest.py").exists())
    quantra = root if (root / "experiments" / "overfit_study.py").exists() else root.parent / "Quantra"
    return root, quantra


HERE = Path(__file__).resolve().parent
ROOT, QUANTRA = _find_roots(HERE)
os.chdir(ROOT)
sys.path.insert(0, str(QUANTRA / "experiments"))
sys.path.insert(0, str(QUANTRA))
warnings.filterwarnings("ignore")

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import overfit_study as ofs  # noqa: E402

OUT = HERE / "output" / "report.txt"
TH = 0.04
PERIODS = [
    ("2020", "2020-01-01", "2020-12-31"),
    ("2021", "2021-01-01", "2021-12-31"),
    ("2022", "2022-01-01", "2022-12-31"),
    ("2023", "2023-01-01", "2023-12-31"),
    ("2024", "2024-01-01", "2024-12-31"),
    ("2025", "2025-01-01", "2025-12-31"),
    ("2026-01~10-16", "2026-01-01", "2026-12-31"),
    ("2020-2023", "2020-01-01", "2023-12-31"),
    ("2024~2026-10", "2024-01-01", "2026-12-31"),
    ("全部", "2020-01-01", "2026-12-31"),
]


def tstat(x):
    x = pd.Series(x).dropna()
    return x.mean() / (x.std() / np.sqrt(len(x))) if len(x) > 1 and x.std() > 0 else np.nan


def daily_features():
    m = pd.read_csv(ofs.MERGED_CSV, parse_dates=["DateTime"])
    m["Date"] = m["DateTime"].dt.normalize()
    d = m.groupby("Date").agg(O=("Open", "first"), H=("High", "max"), L=("Low", "min"), C=("Close", "last"))
    rng = (d["H"] - d["L"]) / d["C"]
    f = pd.DataFrame(index=d.index)
    f["range1"] = rng.shift(1)
    f["rel1"] = rng.shift(1) / rng.shift(2).rolling(20, min_periods=20).mean()  # 昨日振幅 / 再往前 20 日均振幅
    f["prev_ret"] = (d["C"] / d["C"].shift(1) - 1).shift(1)  # 昨日涨跌
    f["day_range"] = rng
    f["day_oc"] = d["C"] / d["O"] - 1
    return f


def main():
    s = ofs.load_series()
    on = s["sweep|range1上限|0.04"]["ret"]
    off = s["sweep|range1上限|None"]["ret"]
    n_off = s["sweep|range1上限|None"]["n"]
    idx = off.index
    on = on.reindex(idx)
    f = daily_features().reindex(idx)
    blk = f["range1"] > TH
    diff = on - off
    out = []
    p = out.append

    p(f"数据 {idx.min().date()} ~ {idx.max().date()}，{len(idx)} 个交易日；1x 杠杆、扣成本")
    p(f"校验：非拦截日 on-off 绝对差合计 {diff[~blk].abs().sum() * 1e4:.2f}bp（应≈0）；拦截日 on 收益合计 {on[blk].abs().sum() * 1e4:.2f}bp（应=0）")

    p("\n=== 1. 有门控(on) vs 无门控(off)，按年 ===")
    p(f"{'时段':<15}{'on年化':>8}{'off年化':>8}{'on夏普':>7}{'off夏普':>8}{'on回撤':>7}{'off回撤':>8}{'拦截天':>6}{'拦截日off合计':>12}{'均值bp':>8}{'t':>6}")
    for name, a, b in PERIODS:
        w = (idx >= a) & (idx <= b)
        so, sf = ofs.stats(on[w]), ofs.stats(off[w])
        rb = off[w & blk]
        p(f"{name:<15}{so['cagr']:>8.1%}{sf['cagr']:>8.1%}{so['sharpe']:>7.2f}{sf['sharpe']:>8.2f}{so['mdd']:>7.1%}{sf['mdd']:>8.1%}"
          f"{len(rb):>6}{rb.sum():>12.2%}{rb.mean() * 1e4:>8.1f}{tstat(rb):>6.2f}")

    p("\n=== 2. 无门控时按昨日振幅分组（门控只是把 >4% 那几组的收益置 0） ===")
    bins = [0, 0.01, 0.015, 0.02, 0.025, 0.03, 0.035, 0.04, 0.05, 0.07, 1]
    g = pd.DataFrame({"r": off, "n": n_off.reindex(idx), "b": pd.cut(f["range1"], bins)})
    p(f"{'range1':<16}{'天数':>5}{'有交易':>6}{'均值bp':>8}{'t':>7}{'盈利天占比':>10}{'合计':>8}{'日均笔数':>8}")
    for k, x in g.groupby("b", observed=True):
        tr = x[x["n"] > 0]
        p(f"{str(k):<16}{len(x):>5}{len(tr):>6}{x['r'].mean() * 1e4:>8.1f}{tstat(x['r']):>7.2f}"
          f"{(tr['r'] > 0).mean():>10.1%}{x['r'].sum():>8.1%}{tr['n'].mean():>8.2f}")

    p("\n=== 3. 阈值邻域（Quantra 已跑的扫描，全部 2020-01~2026-10） ===")
    p(f"{'上限':<8}{'年化':>8}{'夏普':>7}{'回撤':>7}{'2020-23夏普':>11}{'2024-26夏普':>11}{'拦截占比':>8}")
    for th in ["0.02", "0.024", "0.027", "0.029", "0.031", "0.035", "0.04", "None"]:
        r = s[f"sweep|range1上限|{th}"]["ret"].reindex(idx)
        a1, a2, a3 = ofs.stats(r), ofs.stats(r[idx < "2024"]), ofs.stats(r[idx >= "2024"])
        frac = 0 if th == "None" else (f["range1"] > float(th)).mean()
        p(f"{th:<8}{a1['cagr']:>8.1%}{a1['sharpe']:>7.2f}{a1['mdd']:>7.1%}{a2['sharpe']:>11.2f}{a3['sharpe']:>11.2f}{frac:>8.1%}")

    p("\n=== 4. 拦截日收益集中度（无门控） ===")
    rb = off[blk].sort_values()
    p(f"拦截 {len(rb)} 天，合计 {rb.sum():.2%}；去掉最好 3 天 {rb.iloc[:-3].sum():.2%}；去掉最差 3 天 {rb.iloc[3:].sum():.2%}；"
      f"去掉最差 5 天 {rb.iloc[5:].sum():.2%}")
    p("最差 8 天: " + ", ".join(f"{d.date()} {v:+.2%}" for d, v in rb.head(8).items()))
    p("最好 8 天: " + ", ".join(f"{d.date()} {v:+.2%}" for d, v in rb.tail(8).items()))
    p(f"拦截日盈利天占比 {(rb > 0).mean():.1%}，非拦截有交易日盈利天占比 {(off[~blk & (n_off.reindex(idx) > 0)] > 0).mean():.1%}")

    p("\n=== 5. 拦截日里什么情况亏/赚（无门控收益均值 bp / 天数） ===")
    x = pd.DataFrame({"r": off[blk], "dr": f["day_range"][blk], "oc": f["day_oc"][blk].abs(),
                      "pr": f["prev_ret"][blk], "rel": f["rel1"][blk]})
    x["当日振幅"] = pd.qcut(x["dr"], 3, labels=["低", "中", "高"])
    x["当日|收-开|"] = pd.qcut(x["oc"], 3, labels=["低(震荡)", "中", "高(单边)"])
    x["昨日方向"] = np.where(x["pr"] > 0, "昨日涨", "昨日跌")
    x["相对振幅"] = pd.cut(x["rel"], [0, 1.5, 2.2, 100], labels=["≤1.5倍(本来就高波动)", "1.5-2.2倍", ">2.2倍(突发)"])
    for col in ["当日振幅", "当日|收-开|", "昨日方向", "相对振幅"]:
        t = x.groupby(col, observed=True)["r"].agg(["mean", "count", "sum"])
        p(f"-- {col}: " + "；".join(f"{k} {v['mean'] * 1e4:+.1f}bp/{int(v['count'])}天/合计{v['sum']:+.2%}" for k, v in t.iterrows()))

    p("\n=== 6. 绝对振幅 vs 相对振幅（全部日，无门控均值 bp，括号天数） ===")
    t = pd.DataFrame({"r": off, "a": pd.cut(f["range1"], [0, 0.02, 0.03, 0.04, 1], labels=["<2%", "2-3%", "3-4%", ">4%"]),
                      "rel": pd.cut(f["rel1"], [0, 0.8, 1.2, 1.6, 2.2, 100], labels=["<0.8", "0.8-1.2", "1.2-1.6", "1.6-2.2", ">2.2"])}).dropna()
    mean = t.pivot_table(index="a", columns="rel", values="r", aggfunc="mean", observed=False) * 1e4
    cnt = t.pivot_table(index="a", columns="rel", values="r", aggfunc="count", observed=False)
    p((mean.round(1).astype(str) + "(" + cnt.astype(int).astype(str) + ")").to_string())

    p("\n=== 7. 样本外：2005-2019（从未用于调参）。用缓存里有 2005 起数据的 B 版（er5≥0.1，无 range1），"
      "把 range1>4% 的日子收益置 0 做归因 ===")
    rb_all = s["ver|B +er5≥0.1(实盘 03-27)"]["ret"]
    fb = daily_features().reindex(rb_all.index)
    blk_b = fb["range1"] > TH
    p(f"{'时段':<12}{'天数':>5}{'拦截天':>6}{'占比':>6}{'拦截日均bp':>10}{'t':>6}{'拦截日合计':>9}{'夏普off':>8}{'夏普on':>7}{'回撤off':>8}{'回撤on':>7}")
    for a, b in [("2005", "2007-12-31"), ("2008", "2009-12-31"), ("2010", "2014-12-31"), ("2015", "2019-12-31"),
                 ("2005", "2019-12-31"), ("2020", "2026-12-31"), ("2005", "2026-12-31")]:
        w = (rb_all.index >= a) & (rb_all.index <= b)
        rr, bb = rb_all[w], blk_b[w]
        so, sf = ofs.stats(rr.where(~bb, 0.0)), ofs.stats(rr)
        x = rr[bb]
        p(f"{a + '~' + b[:4]:<12}{len(rr):>5}{int(bb.sum()):>6}{bb.mean():>6.1%}{x.mean() * 1e4:>10.1f}{tstat(x):>6.2f}{x.sum():>9.1%}"
          f"{sf['sharpe']:>8.2f}{so['sharpe']:>7.2f}{sf['mdd']:>8.1%}{so['mdd']:>7.1%}")
    x = rb_all[(rb_all.index < "2020") & blk_b].sort_values()
    p(f"2005-2019 拦截日合计 {x.sum():.2%}，去最差 3 天 {x.iloc[3:].sum():.2%}，去最差 5 天 {x.iloc[5:].sum():.2%}；"
      f"同期全部日均 {rb_all[rb_all.index < '2020'].mean() * 1e4:.2f}bp")
    d = pd.DataFrame({"r": x, "pr": fb["prev_ret"].reindex(x.index)})
    p(f"2005-2019 拦截日：昨日跌 {d[d.pr < 0].r.mean() * 1e4:.1f}bp/{(d.pr < 0).sum()}天，昨日涨 {d[d.pr > 0].r.mean() * 1e4:.1f}bp/{(d.pr > 0).sum()}天")

    text = "\n".join(out)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
