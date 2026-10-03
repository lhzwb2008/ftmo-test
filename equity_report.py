"""
独立的按日权益 / 收益可视化报告。

与 backtest 主逻辑完全隔离：只消费 daily_df / metrics / config，
生成自包含 HTML（纯 SVG + 轻量悬停提示，不依赖 matplotlib / plotly）。
"""

from __future__ import annotations

import html
import os
import webbrowser
from datetime import datetime
from typing import Any, Mapping, Optional, Sequence

import numpy as np
import pandas as pd


def render_equity_report(
    daily_df: pd.DataFrame,
    metrics: Mapping[str, Any],
    config: Optional[Mapping[str, Any]] = None,
    buy_hold_df: Optional[pd.DataFrame] = None,
    trades_df: Optional[pd.DataFrame] = None,
    *,
    open_browser: bool = True,
    output_path: Optional[str] = None,
) -> str:
    """
    生成按日权益报告 HTML，可选自动打开浏览器。

    参数:
        daily_df: 索引为 Date，至少含 capital / daily_return
        metrics: calculate_performance_metrics（及回测后补充）的结果
        config: 回测配置（用于标题与路径）
        buy_hold_df: 可选，含 capital 列时叠加 Buy & Hold 曲线
        trades_df: 可选，用于识别「有交易的日子」，日胜率仅在这些日上统计
        open_browser: 是否用系统默认浏览器打开
        output_path: 输出路径；默认 reports/equity_report_<ticker>_<ts>.html

    返回:
        写入的 HTML 文件绝对路径
    """
    config = dict(config or {})
    if daily_df is None or len(daily_df) == 0:
        raise ValueError("daily_df 为空，无法生成权益报告")

    ticker = str(config.get('ticker', 'Strategy'))
    initial_capital = float(config.get('initial_capital', daily_df['capital'].iloc[0]))
    leverage = config.get('leverage', 1)

    dates = [pd.Timestamp(d).to_pydatetime() for d in daily_df.index]
    capital = [float(x) for x in daily_df['capital'].tolist()]
    daily_ret = [float(x) for x in daily_df['daily_return'].fillna(0).tolist()]

    bh_capital: Optional[list[float]] = None
    if buy_hold_df is not None and not buy_hold_df.empty and 'capital' in buy_hold_df.columns:
        bh = buy_hold_df.reindex(daily_df.index)
        if bh['capital'].notna().any():
            # 对齐策略交易日；缺失处用前值填充，便于同图对比
            bh_capital = [float(x) if pd.notna(x) else np.nan for x in bh['capital'].ffill().tolist()]

    peaks = np.maximum.accumulate(np.asarray(capital, dtype=float))
    drawdowns = ((np.asarray(capital) - peaks) / peaks * 100.0).tolist()

    start_s = dates[0].strftime('%Y-%m-%d')
    end_s = dates[-1].strftime('%Y-%m-%d')
    strategy_label = f"{ticker} Strategy"
    if leverage and leverage != 1:
        strategy_label = f"{ticker} Strategy ({leverage}x)"

    monthly = _monthly_rows(daily_df)
    active_flags = _active_trade_day_flags(daily_df, trades_df)
    win_stats = _win_rate_stats(daily_ret, monthly, active_flags=active_flags)
    current_dd = _current_drawdown_stats(dates, capital)
    metrics_view = dict(metrics)
    metrics_view.update(current_dd)
    recent_days = _recent_active_trade_days(
        daily_df, active_flags, trades_df, initial_capital, n=5,
    )
    dd_path = _drawdown_trade_path(trades_df, initial_capital)

    page = _build_html(
        title=f"{ticker} · Equity Report",
        subtitle=f"{start_s} → {end_s}",
        strategy_label=strategy_label,
        bh_label=f"{ticker} Buy & Hold",
        dates=dates,
        capital=capital,
        daily_ret=daily_ret,
        drawdowns=drawdowns,
        bh_capital=bh_capital,
        metrics=metrics_view,
        initial_capital=initial_capital,
        monthly=monthly,
        win_stats=win_stats,
        recent_days=recent_days,
        dd_path=dd_path,
    )

    if output_path is None:
        out_dir = config.get('equity_report_dir', 'reports')
        os.makedirs(out_dir, exist_ok=True)
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        output_path = os.path.join(out_dir, f"equity_report_{ticker}_{ts}.html")
    else:
        parent = os.path.dirname(output_path)
        if parent:
            os.makedirs(parent, exist_ok=True)

    abs_path = os.path.abspath(output_path)
    with open(abs_path, 'w', encoding='utf-8') as f:
        f.write(page)

    print(f"\n权益报告已生成: {abs_path}")
    if open_browser:
        webbrowser.open(f"file://{abs_path}")
    return abs_path


def _monthly_rows(daily_df: pd.DataFrame) -> list[dict[str, Any]]:
    monthly = daily_df.resample('ME').first()[['capital']].rename(columns={'capital': 'month_start'})
    monthly['month_end'] = daily_df.resample('ME').last()['capital']
    monthly['monthly_return'] = monthly['month_end'] / monthly['month_start'] - 1
    rows = []
    for idx, row in monthly.iterrows():
        rows.append({
            'label': pd.Timestamp(idx).strftime('%Y-%m'),
            'start': float(row['month_start']),
            'end': float(row['month_end']),
            'ret': float(row['monthly_return']),
        })
    return rows


def _active_trade_day_flags(
    daily_df: pd.DataFrame,
    trades_df: Optional[pd.DataFrame],
) -> list[bool]:
    """与 daily_df 对齐：当日是否发生过至少一笔交易。"""
    n = len(daily_df)
    if trades_df is not None and len(trades_df) > 0 and 'Date' in trades_df.columns:
        trade_days = {
            pd.Timestamp(d).normalize()
            for d in trades_df['Date'].tolist()
            if pd.notna(d)
        }
        return [pd.Timestamp(idx).normalize() in trade_days for idx in daily_df.index]
    # 无成交明细时退化为「日收益非零」近似（净值为 0 的有交易日会被漏掉）
    rets = daily_df['daily_return'].fillna(0)
    return [abs(float(r)) > 1e-15 for r in rets.tolist()]


def _win_rate_stats(
    daily_ret: Sequence[float],
    monthly: Sequence[Mapping[str, Any]],
    active_flags: Optional[Sequence[bool]] = None,
) -> dict[str, Any]:
    """
    日胜率：仅统计有交易的日子（日收益 > 0 / 有交易日数）。
    另返回交易日占比 = 有交易日 / 全部回测交易日。
    """
    n_calendar = len(daily_ret)
    if active_flags is None or len(active_flags) != n_calendar:
        active_flags = [True] * n_calendar

    active_rets = [r for r, a in zip(daily_ret, active_flags) if a]
    n_active = len(active_rets)
    win_days = sum(1 for r in active_rets if r > 0)

    n_months = len(monthly)
    win_months = sum(1 for m in monthly if m.get('ret', 0) > 0)
    return {
        'daily_win_rate': (win_days / n_active) if n_active else 0.0,
        'daily_wins': win_days,
        'daily_total': n_active,  # 分母：有交易日
        'calendar_days': n_calendar,
        'active_days': n_active,
        'active_day_ratio': (n_active / n_calendar) if n_calendar else 0.0,
        'monthly_win_rate': (win_months / n_months) if n_months else 0.0,
        'monthly_wins': win_months,
        'monthly_total': n_months,
    }


def _current_drawdown_stats(
    dates: Sequence[datetime],
    capital: Sequence[float],
) -> dict[str, Any]:
    """从历史权益最高点到报告末日终的当前回撤（日终口径）。"""
    empty = {
        'current_drawdown_pct': 0.0,
        'current_drawdown_amount': 0.0,
        'current_peak_date': None,
        'current_drawdown_trading_days': 0,
    }
    if not capital:
        return empty
    cap = np.asarray(capital, dtype=float)
    ath = float(np.nanmax(cap))
    if not np.isfinite(ath) or ath <= 0:
        return empty
    peak_idx = int(np.max(np.flatnonzero(cap >= ath - 1e-9)))
    end = float(cap[-1])
    amount = end - ath
    pct = (-amount / ath) if ath else 0.0
    return {
        'current_drawdown_pct': max(pct, 0.0),
        'current_drawdown_amount': amount,
        'current_peak_date': dates[peak_idx],
        'current_drawdown_trading_days': max(len(cap) - 1 - peak_idx, 0),
    }


def _recent_active_trade_days(
    daily_df: pd.DataFrame,
    active_flags: Sequence[bool],
    trades_df: Optional[pd.DataFrame],
    initial_capital: float,
    n: int = 5,
) -> list[dict[str, Any]]:
    """最近 n 个有成交日：日终盈亏（美元）与当日收益率。"""
    if daily_df is None or len(daily_df) == 0:
        return []
    caps = daily_df['capital'].astype(float)
    rets = daily_df['daily_return'].fillna(0).astype(float)
    prev = caps.shift(1)
    prev.iloc[0] = float(initial_capital)
    pnls = caps - prev

    counts: dict[Any, int] = {}
    has_trades = trades_df is not None and len(trades_df) > 0 and 'Date' in trades_df.columns
    if has_trades:
        for d in trades_df['Date'].tolist():
            if pd.isna(d):
                continue
            k = pd.Timestamp(d).date()
            counts[k] = counts.get(k, 0) + 1

    rows: list[dict[str, Any]] = []
    for idx, flag, ret, pnl in zip(daily_df.index, active_flags, rets.tolist(), pnls.tolist()):
        if not flag:
            continue
        k = pd.Timestamp(idx).date()
        rows.append({
            'date': pd.Timestamp(idx),
            'pnl': float(pnl),
            'ret': float(ret),
            'trades': counts.get(k, 0) if has_trades else None,
        })
    return list(reversed(rows[-n:]))


def _drawdown_trade_path(
    trades_df: Optional[pd.DataFrame],
    initial_capital: float,
) -> dict[str, Any]:
    """
    按每笔平仓后的资金画回撤：横轴是成交顺序，一天可有多个点。
    起点为初始资金。无成交时返回空，由图表退回日终路径。
    """
    empty: dict[str, Any] = {
        'xs': [], 'dds': [], 'tips': [], 'spans': [], 'x_labels': [], 'n_trades': 0,
    }
    if trades_df is None or len(trades_df) == 0 or 'pnl' not in trades_df.columns:
        return empty

    df = trades_df.copy()
    if 'exit_time' in df.columns:
        df['_t'] = pd.to_datetime(df['exit_time'], errors='coerce')
    elif 'Date' in df.columns:
        df['_t'] = pd.to_datetime(df['Date'], errors='coerce')
    else:
        df['_t'] = pd.NaT
    df = df.sort_values('_t', kind='mergesort', na_position='last').reset_index(drop=True)

    xs: list[float] = [0.0]
    eqs: list[float] = [float(initial_capital)]
    tips: list[str] = [
        '<div class="tip-date">起点</div>'
        f'<div><span class="tip-k">权益</span> {_fmt_money(initial_capital)}</div>'
        '<div><span class="tip-k">回撤</span> 0.00%</div>'
    ]
    times: list[Optional[pd.Timestamp]] = [None]

    eq = float(initial_capital)
    for i, row in df.iterrows():
        pnl = float(row['pnl']) if pd.notna(row['pnl']) else 0.0
        eq += pnl
        xs.append(float(i) + 1.0)
        eqs.append(eq)
        ts = row['_t']
        times.append(ts if pd.notna(ts) else None)
        when = pd.Timestamp(ts).strftime('%Y-%m-%d %H:%M') if pd.notna(ts) else f'#{int(i)+1}'
        side = row['side'] if 'side' in df.columns and pd.notna(row['side']) else ''
        side_cn = {'Long': '多', 'Short': '空'}.get(str(side), str(side) if side else '')
        reason = ''
        if 'exit_reason' in df.columns and pd.notna(row.get('exit_reason')):
            reason = str(row['exit_reason'])
        lines = [
            f'<div class="tip-date">{html.escape(when)} · {html.escape(side_cn)} #{int(i)+1}</div>',
            f'<div><span class="tip-k">盈亏</span> {html.escape(_fmt_signed_money(pnl))}</div>',
            f'<div><span class="tip-k">权益</span> {html.escape(_fmt_money(eq))}</div>',
        ]
        if reason:
            lines.append(f'<div><span class="tip-k">出场</span> {html.escape(str(reason))}</div>')
        tips.append("".join(lines))

    dds: list[float] = []
    peak = eqs[0]
    for e in eqs:
        if e > peak:
            peak = e
        dds.append(((e - peak) / peak * 100.0) if peak else 0.0)

    for i, dd in enumerate(dds):
        if i == 0:
            continue
        tips[i] += f'<div><span class="tip-k">回撤</span> {dd:.2f}%</div>'

    spans = [(float(x) - 0.5, float(x) + 0.5) for x in xs]

    x_labels: list[tuple[float, str]] = []
    n = len(xs)
    if n:
        pick = np.unique(np.linspace(0, n - 1, min(6, n), dtype=int))
        for idx in pick:
            lab = '起点'
            if idx > 0 and times[idx] is not None:
                lab = pd.Timestamp(times[idx]).strftime('%m/%d')
            elif idx > 0:
                lab = f'#{idx}'
            x_labels.append((xs[idx], lab))

    return {
        'xs': xs,
        'dds': dds,
        'tips': tips,
        'spans': spans,
        'x_labels': x_labels,
        'n_trades': int(len(df)),
    }


def _fmt_pct(x: Any, digits: int = 1) -> str:
    try:
        if x is None or (isinstance(x, float) and (np.isnan(x) or np.isinf(x))):
            return "—"
        return f"{float(x) * 100:.{digits}f}%"
    except (TypeError, ValueError):
        return "—"


def _fmt_num(x: Any, digits: int = 2) -> str:
    try:
        if x is None or (isinstance(x, float) and (np.isnan(x) or np.isinf(x))):
            return "—"
        if x == float('inf'):
            return "∞"
        return f"{float(x):.{digits}f}"
    except (TypeError, ValueError):
        return "—"


def _fmt_money(x: Any) -> str:
    try:
        return f"${float(x):,.0f}"
    except (TypeError, ValueError):
        return "—"


def _fmt_signed_money(x: Any) -> str:
    try:
        v = float(x)
        if v >= 0:
            return f"+${v:,.2f}"
        return f"-${abs(v):,.2f}"
    except (TypeError, ValueError):
        return "—"


def _fmt_date(x: Any) -> Optional[str]:
    if x is None:
        return None
    try:
        return pd.Timestamp(x).strftime('%Y-%m-%d')
    except Exception:
        return str(x)


def _fmt_top_days(rows: Optional[Sequence[Mapping[str, Any]]], digits: int = 2) -> Optional[str]:
    """格式化 Top-N 日期列表为多行文案。"""
    if not rows:
        return None
    lines = []
    for i, r in enumerate(rows, 1):
        d = _fmt_date(r.get('date')) or "?"
        pct = _fmt_pct(r.get('pct'), digits)
        lines.append(f"{i}) {d} {pct}")
    return "\n".join(lines)


def _safe_metric(metrics: Mapping[str, Any], key: str, default: Any = None) -> Any:
    v = metrics.get(key, default)
    if isinstance(v, float) and (np.isnan(v) or np.isinf(v)):
        if np.isinf(v) and v > 0:
            return float('inf')
        return default
    return v


def _polyline(
    xs: Sequence[float],
    ys: Sequence[float],
    x0: float,
    y0: float,
    w: float,
    h: float,
    y_min: float,
    y_max: float,
) -> str:
    pts = _xy_points(xs, ys, x0, y0, w, h, y_min, y_max)
    return " ".join(f"{px:.2f},{py:.2f}" for px, py in pts)


def _area_path(
    xs: Sequence[float],
    ys: Sequence[float],
    x0: float,
    y0: float,
    w: float,
    h: float,
    y_min: float,
    y_max: float,
    baseline: float = 0.0,
) -> str:
    coords = _xy_points(xs, ys, x0, y0, w, h, y_min, y_max)
    if not coords:
        return ""
    yspan = max(y_max - y_min, 1e-12)
    by = y0 + h - ((baseline - y_min) / yspan) * h
    d = [f"M {coords[0][0]:.2f} {by:.2f}"]
    for px, py in coords:
        d.append(f"L {px:.2f} {py:.2f}")
    d.append(f"L {coords[-1][0]:.2f} {by:.2f} Z")
    return " ".join(d)


def _xy_points(
    xs: Sequence[float],
    ys: Sequence[float],
    x0: float,
    y0: float,
    w: float,
    h: float,
    y_min: float,
    y_max: float,
) -> list[tuple[float, float]]:
    pairs = [
        (float(x), float(y))
        for x, y in zip(xs, ys)
        if y is not None and not (isinstance(y, float) and np.isnan(y))
    ]
    if not pairs:
        return []
    x_min = min(p[0] for p in pairs)
    x_max = max(p[0] for p in pairs)
    xspan = max(x_max - x_min, 1e-12)
    yspan = max(y_max - y_min, 1e-12)
    out = []
    for x, y in pairs:
        px = x0 + (x - x_min) / xspan * w
        py = y0 + h - ((y - y_min) / yspan) * h
        out.append((px, py))
    return out


def _y_ticks(y_min: float, y_max: float, count: int = 5) -> list[float]:
    if y_max <= y_min:
        y_max = y_min + 1.0
    step = (y_max - y_min) / (count - 1)
    return [y_min + i * step for i in range(count)]


def _chart_wrap(svg_inner: str, W: int, H: int, aria: str) -> str:
    """包一层容器，供鼠标悬停浮层定位。"""
    return (
        f'<div class="chart-wrap">'
        f'<svg viewBox="0 0 {W} {H}" class="chart" role="img" aria-label="{html.escape(aria)}">'
        f'{svg_inner}'
        f'</svg>'
        f'<div class="chart-tooltip" hidden></div>'
        f'</div>'
    )


def _hit_strips(
    n: int,
    x0: float,
    y0: float,
    w: float,
    h: float,
    tip_htmls: Sequence[str],
    *,
    centered: bool = False,
) -> str:
    """
    每个交易日一条透明竖条，便于悬停取最近点。
    tip_htmls[i] 为已转义的 HTML 片段（日期 + 数值）。
    centered=True 时按柱状图中心对齐（日收益图）。
    """
    if n <= 0 or len(tip_htmls) != n:
        return ""
    strips = []
    for i, tip in enumerate(tip_htmls):
        if centered:
            cx = x0 + (i + 0.5) / n * w
            half = (w / n) * 0.5
            rx = cx - half
            rw = max(half * 2, 2.0)
        else:
            # 线型图：第 i 点在 x0 + i/(n-1)*w；条宽覆盖相邻中点
            if n == 1:
                rx, rw = x0, w
            else:
                left = 0.0 if i == 0 else (i - 0.5) / (n - 1)
                right = 1.0 if i == n - 1 else (i + 0.5) / (n - 1)
                rx = x0 + left * w
                rw = max((right - left) * w, 2.0)
        strips.append(
            f'<rect class="hit" x="{rx:.2f}" y="{y0:.2f}" width="{rw:.2f}" height="{h:.2f}" '
            f'data-tip="{html.escape(tip, quote=True)}"/>'
        )
    return "".join(strips)


def _hit_strips_mapped(
    x0: float,
    y0: float,
    w: float,
    h: float,
    x_min: float,
    x_max: float,
    spans: Sequence[tuple[float, float]],
    tip_htmls: Sequence[str],
) -> str:
    """按数据 x 区间画悬停条（成交折线：每笔一条）。"""
    if len(spans) != len(tip_htmls) or not spans:
        return ""
    xspan = max(float(x_max) - float(x_min), 1e-12)
    strips = []
    for (xa, xb), tip in zip(spans, tip_htmls):
        rx = x0 + (float(xa) - x_min) / xspan * w
        rw = max((float(xb) - float(xa)) / xspan * w, 2.0)
        strips.append(
            f'<rect class="hit" x="{rx:.2f}" y="{y0:.2f}" width="{rw:.2f}" height="{h:.2f}" '
            f'data-tip="{html.escape(tip, quote=True)}"/>'
        )
    return "".join(strips)


def _svg_equity_chart(
    dates: Sequence[datetime],
    capital: Sequence[float],
    bh_capital: Optional[Sequence[float]],
    strategy_label: str,
    bh_label: str,
) -> str:
    W, H = 920, 340
    pad_l, pad_r, pad_t, pad_b = 64, 24, 28, 44
    x0, y0 = pad_l, pad_t
    w = W - pad_l - pad_r
    h = H - pad_t - pad_b

    series = list(capital)
    if bh_capital:
        series = series + [v for v in bh_capital if v is not None and not (isinstance(v, float) and np.isnan(v))]
    y_min = min(series) * 0.995
    y_max = max(series) * 1.005
    xs = list(range(len(dates)))

    strat_pts = _polyline(xs, capital, x0, y0, w, h, y_min, y_max)
    bh_pts = ""
    if bh_capital:
        bh_pts = _polyline(xs, bh_capital, x0, y0, w, h, y_min, y_max)

    # 网格与刻度
    grid = []
    for ty in _y_ticks(y_min, y_max):
        py = y0 + h - ((ty - y_min) / max(y_max - y_min, 1e-12)) * h
        grid.append(
            f'<line x1="{x0}" y1="{py:.2f}" x2="{x0+w}" y2="{py:.2f}" class="grid"/>'
            f'<text x="{x0-10}" y="{py+4:.2f}" class="tick" text-anchor="end">${ty:,.0f}</text>'
        )

    # X 轴日期标签（约 6 个）
    n = max(len(dates) - 1, 1)
    x_labels = []
    for i in np.linspace(0, len(dates) - 1, min(6, len(dates)), dtype=int):
        px = x0 + (i / n) * w
        x_labels.append(
            f'<text x="{px:.2f}" y="{y0+h+22}" class="tick" text-anchor="middle">'
            f'{dates[i].strftime("%m/%d")}</text>'
        )

    legend = [
        f'<circle cx="{x0}" cy="14" r="4" fill="var(--accent)"/>'
        f'<text x="{x0+10}" y="18" class="legend">{html.escape(strategy_label)}</text>'
    ]
    if bh_pts:
        legend.append(
            f'<circle cx="{x0+220}" cy="14" r="4" fill="var(--muted)"/>'
            f'<text x="{x0+230}" y="18" class="legend">{html.escape(bh_label)}</text>'
        )

    bh_line = (
        f'<polyline points="{bh_pts}" fill="none" stroke="var(--muted)" '
        f'stroke-width="1.75" stroke-dasharray="5 4" opacity="0.85"/>'
        if bh_pts else ""
    )

    tips = []
    for i, d in enumerate(dates):
        lines = [
            f'<div class="tip-date">{d.strftime("%Y-%m-%d")}</div>',
            f'<div><span class="tip-k">策略</span> {_fmt_money(capital[i])}</div>',
        ]
        if bh_capital is not None and i < len(bh_capital):
            bv = bh_capital[i]
            if bv is not None and not (isinstance(bv, float) and np.isnan(bv)):
                lines.append(
                    f'<div><span class="tip-k">B&H</span> {_fmt_money(bv)}</div>'
                )
        tips.append("".join(lines))
    hits = _hit_strips(len(dates), x0, y0, w, h, tips)

    inner = f'''
  {''.join(legend)}
  {''.join(grid)}
  <line x1="{x0}" y1="{y0+h}" x2="{x0+w}" y2="{y0+h}" class="axis"/>
  <line x1="{x0}" y1="{y0}" x2="{x0}" y2="{y0+h}" class="axis"/>
  {bh_line}
  <polyline points="{strat_pts}" fill="none" stroke="var(--accent)" stroke-width="2.4"
            stroke-linejoin="round" stroke-linecap="round"/>
  {''.join(x_labels)}
  {hits}
'''
    return _chart_wrap(inner, W, H, "Equity curve")


def _svg_drawdown_chart(
    dates: Sequence[datetime],
    drawdowns: Sequence[float],
    *,
    path_xs: Optional[Sequence[float]] = None,
    path_dds: Optional[Sequence[float]] = None,
    day_tips: Optional[Sequence[str]] = None,
    day_spans: Optional[Sequence[tuple[float, float]]] = None,
    path_x_labels: Optional[Sequence[tuple[float, str]]] = None,
) -> str:
    W, H = 920, 230
    pad_l, pad_r, pad_t, pad_b = 64, 24, 20, 40
    x0, y0 = pad_l, pad_t
    w = W - pad_l - pad_r
    h = H - pad_t - pad_b

    xs = list(path_xs) if path_xs else list(range(len(dates)))
    dds = list(path_dds) if path_dds is not None else list(drawdowns)
    if not xs or not dds:
        return ""

    y_min = min(min(dds), -0.01)
    y_max = 0.0
    pad = max(abs(y_min) * 0.04, 0.15)
    y_max_plot = y_max + pad
    area = _area_path(xs, dds, x0, y0, w, h, y_min, y_max_plot, baseline=0.0)
    line = _polyline(xs, dds, x0, y0, w, h, y_min, y_max_plot)

    ticks = [y_min, y_min / 2.0, 0.0]
    grid = []
    for ty in ticks:
        py = y0 + h - ((ty - y_min) / max(y_max_plot - y_min, 1e-12)) * h
        grid.append(
            f'<line x1="{x0}" y1="{py:.2f}" x2="{x0+w}" y2="{py:.2f}" class="grid"/>'
            f'<text x="{x0-10}" y="{py+4:.2f}" class="tick" text-anchor="end">{ty:.1f}%</text>'
        )

    x_min, x_max = float(min(xs)), float(max(xs))
    xspan = max(x_max - x_min, 1e-12)
    x_labels = []
    if path_x_labels:
        for xv, lab in path_x_labels:
            px = x0 + (float(xv) - x_min) / xspan * w
            x_labels.append(
                f'<text x="{px:.2f}" y="{y0+h+22}" class="tick" text-anchor="middle">'
                f'{html.escape(lab)}</text>'
            )
    else:
        for i in np.linspace(0, len(dates) - 1, min(6, len(dates)), dtype=int):
            px = x0 + (float(i) - x_min) / xspan * w
            x_labels.append(
                f'<text x="{px:.2f}" y="{y0+h+22}" class="tick" text-anchor="middle">'
                f'{dates[i].strftime("%m/%d")}</text>'
            )

    zero_y = y0 + h - ((0.0 - y_min) / max(y_max_plot - y_min, 1e-12)) * h

    if day_tips and day_spans:
        hits = _hit_strips_mapped(x0, y0, w, h, x_min, x_max, day_spans, day_tips)
    else:
        tips = []
        for i, d in enumerate(dates):
            dd = drawdowns[i] if i < len(drawdowns) else 0.0
            tips.append(
                f'<div class="tip-date">{d.strftime("%Y-%m-%d")}</div>'
                f'<div><span class="tip-k">日终回撤</span> {dd:.2f}%</div>'
            )
        hits = _hit_strips(len(dates), x0, y0, w, h, tips)

    inner = f'''
  {''.join(grid)}
  <line x1="{x0}" y1="{zero_y:.2f}" x2="{x0+w}" y2="{zero_y:.2f}" class="axis"/>
  <path d="{area}" fill="var(--danger-soft)" opacity="0.9"/>
  <polyline points="{line}" fill="none" stroke="var(--danger)" stroke-width="1.6"
            stroke-linejoin="round" stroke-linecap="round"/>
  {''.join(x_labels)}
  {hits}
'''
    return _chart_wrap(inner, W, H, "Drawdown")


def _svg_daily_bars(dates: Sequence[datetime], daily_ret: Sequence[float]) -> str:
    W, H = 920, 220
    pad_l, pad_r, pad_t, pad_b = 64, 24, 16, 40
    x0, y0 = pad_l, pad_t
    w = W - pad_l - pad_r
    h = H - pad_t - pad_b

    rets_pct = [r * 100 for r in daily_ret]
    amp = max(abs(min(rets_pct)), abs(max(rets_pct)), 0.1)
    y_min, y_max = -amp * 1.15, amp * 1.15
    zero_y = y0 + h - ((0.0 - y_min) / (y_max - y_min)) * h

    n = len(rets_pct)
    gap = 0.25
    bar_w = (w / max(n, 1)) * (1 - gap)

    bars = []
    tips = []
    for i, r in enumerate(rets_pct):
        cx = x0 + (i + 0.5) / n * w
        py = y0 + h - ((r - y_min) / (y_max - y_min)) * h
        top = min(py, zero_y)
        height = abs(py - zero_y)
        color = "var(--gain)" if r >= 0 else "var(--danger)"
        bars.append(
            f'<rect x="{cx - bar_w/2:.2f}" y="{top:.2f}" width="{bar_w:.2f}" '
            f'height="{max(height, 0.5):.2f}" fill="{color}" opacity="0.85" rx="1"/>'
        )
        sign = "+" if r >= 0 else ""
        tips.append(
            f'<div class="tip-date">{dates[i].strftime("%Y-%m-%d")}</div>'
            f'<div><span class="tip-k">日收益</span> {sign}{r:.2f}%</div>'
        )
    hits = _hit_strips(n, x0, y0, w, h, tips, centered=True)

    grid = []
    for ty in [-amp, 0.0, amp]:
        py = y0 + h - ((ty - y_min) / (y_max - y_min)) * h
        grid.append(
            f'<line x1="{x0}" y1="{py:.2f}" x2="{x0+w}" y2="{py:.2f}" class="grid"/>'
            f'<text x="{x0-10}" y="{py+4:.2f}" class="tick" text-anchor="end">{ty:.1f}%</text>'
        )

    x_labels = []
    for i in np.linspace(0, n - 1, min(6, n), dtype=int):
        px = x0 + (i + 0.5) / n * w
        x_labels.append(
            f'<text x="{px:.2f}" y="{y0+h+22}" class="tick" text-anchor="middle">'
            f'{dates[i].strftime("%m/%d")}</text>'
        )

    inner = f'''
  {''.join(grid)}
  {''.join(bars)}
  {''.join(x_labels)}
  {hits}
'''
    return _chart_wrap(inner, W, H, "Daily returns")


def _metric_cards(
    metrics: Mapping[str, Any],
    initial_capital: float,
    capital_end: float,
    win_stats: Optional[Mapping[str, Any]] = None,
) -> str:
    total = _safe_metric(metrics, 'total_return', 0)
    irr = _safe_metric(metrics, 'irr', 0)
    vol = _safe_metric(metrics, 'volatility', 0)
    sharpe = _safe_metric(metrics, 'sharpe_ratio', 0)
    mdd = _safe_metric(metrics, 'mdd', 0)
    calmar = _safe_metric(metrics, 'calmar_ratio', 0)
    hit = _safe_metric(metrics, 'hit_ratio', 0)
    trades = int(_safe_metric(metrics, 'total_trades', 0) or 0)

    bh_total = _safe_metric(metrics, 'buy_hold_return')
    bh_irr = _safe_metric(metrics, 'buy_hold_irr')
    bh_vol = _safe_metric(metrics, 'buy_hold_volatility')
    bh_sharpe = _safe_metric(metrics, 'buy_hold_sharpe')
    bh_mdd = _safe_metric(metrics, 'buy_hold_mdd')
    bh_calmar = _safe_metric(metrics, 'buy_hold_calmar')

    mdd1d = _safe_metric(metrics, 'max_single_day_intraday_mdd_pct')
    loss1d = _safe_metric(metrics, 'max_single_day_loss_from_start_pct')

    win_stats = dict(win_stats or {})
    d_rate = win_stats.get('daily_win_rate')
    m_rate = win_stats.get('monthly_win_rate')
    active_ratio = win_stats.get('active_day_ratio')
    d_sub = None
    m_sub = None
    active_sub = None
    if win_stats.get('daily_total'):
        d_sub = f"{win_stats['daily_wins']}/{win_stats['daily_total']} 有交易日盈利"
    if win_stats.get('monthly_total'):
        m_sub = f"{win_stats['monthly_wins']}/{win_stats['monthly_total']} 月盈利"
    if win_stats.get('calendar_days'):
        active_sub = f"{win_stats['active_days']}/{win_stats['calendar_days']} 交易日有成交"

    # (label, value, subtitle, signed) — subtitle 可为多行
    cards = [
        ("总回报", _fmt_pct(total), f"B&H {_fmt_pct(bh_total)}" if bh_total is not None else None, total),
        ("年化收益", _fmt_pct(irr), f"B&H {_fmt_pct(bh_irr)}" if bh_irr is not None else None, irr),
        ("波动率", _fmt_pct(vol), f"B&H {_fmt_pct(bh_vol)}" if bh_vol is not None else None, None),
        ("夏普", _fmt_num(sharpe), f"B&H {_fmt_num(bh_sharpe)}" if bh_sharpe is not None else None, sharpe),
        ("最大回撤", _fmt_pct(mdd), _mdd_card_sub(metrics, bh_mdd), -mdd if mdd else None),
        ("当前回撤", *_current_dd_card(metrics)),
        ("Calmar", _fmt_num(calmar), f"B&H {_fmt_num(bh_calmar)}" if bh_calmar is not None else None, calmar),
    ]
    if mdd1d is not None:
        cards.append((
            "单日峰谷回撤",
            _fmt_pct(mdd1d, 2),
            _single_day_card_sub(
                metrics.get('max_single_day_intraday_mdd_date'),
                metrics.get('top_single_day_intraday_mdd_days'),
            ),
            -mdd1d,
        ))
    if loss1d is not None:
        cards.append((
            "单日最大亏损(日初)",
            _fmt_pct(loss1d, 2),
            _single_day_card_sub(
                metrics.get('max_single_day_loss_from_start_date'),
                metrics.get('top_single_day_loss_from_start_days'),
            ),
            -loss1d,
        ))

    max_single_loss = _safe_metric(metrics, 'max_single_loss')
    max_single_loss_pct = _safe_metric(metrics, 'max_single_loss_pct', 0.0)
    if trades > 0 and max_single_loss is not None and float(max_single_loss) < 0:
        msl_sub_parts = []
        msl_date = _fmt_date(metrics.get('max_single_loss_date'))
        msl_side = metrics.get('max_single_loss_side') or ''
        if msl_date:
            msl_sub_parts.append(f"{msl_side} {msl_date}".strip())
        if max_single_loss_pct:
            msl_sub_parts.append(f"逆向价差 {_fmt_pct(max_single_loss_pct, 2)}")
        msl_reason = metrics.get('max_single_loss_exit_reason')
        if msl_reason:
            msl_sub_parts.append(str(msl_reason))
        cards.append((
            "单笔最大亏损",
            f"${float(max_single_loss):,.2f}",
            " · ".join(msl_sub_parts) if msl_sub_parts else None,
            float(max_single_loss),
        ))

    liq = metrics.get('leverage_liquidation') or metrics.get('icmarkets_liquidation')
    if isinstance(liq, Mapping) and liq.get('enabled') and liq.get('n_trades', 0) > 0:
        so_rate = liq.get('stop_out_rate', 0.0)
        so_cnt = liq.get('stop_out_count', 0)
        n_tr = liq.get('n_trades', 0)
        so_sub = f"{so_cnt}/{n_tr} 笔 · 阈值 {_fmt_pct(liq.get('stop_out_mae_thresh'), 2)}"
        cards.append((
            "Stop Out 触及率",
            _fmt_pct(so_rate, 1),
            so_sub,
            -so_rate if so_rate else 0.0,
        ))
        max_mae = liq.get('max_mae_price_pct', 0.0)
        max_mae_eq = liq.get('max_mae_equity_pct', max_mae * float(liq.get('leverage') or 1))
        mae_date = _fmt_date(liq.get('max_mae_date'))
        mae_side = liq.get('max_mae_side') or ''
        mae_sub_parts = []
        if mae_date:
            mae_sub_parts.append(f"{mae_side} {mae_date}".strip())
        mae_sub_parts.append(f"价格MAE {_fmt_pct(max_mae, 2)}")
        wipe_n = liq.get('wipe_count', 0)
        mae_sub_parts.append(f"打穿 {wipe_n}/{n_tr}")
        cards.append((
            "最大权益MAE",
            _fmt_pct(max_mae_eq, 2),
            " · ".join(mae_sub_parts),
            -max_mae_eq if max_mae_eq else None,
        ))
        safe_L = liq.get('max_safe_leverage')
        raw_L = liq.get('max_safe_leverage_raw')
        buf = liq.get('safety_buffer', 0.9)
        if safe_L is None and raw_L is None:
            safe_val = "∞"
            safe_sub = "历史无逆向 MAE"
        else:
            safe_val = "∞" if safe_L is None else f"{safe_L}x"
            raw_s = f"{raw_L:.2f}x" if raw_L is not None else "∞"
            safe_sub = f"原始 {raw_s} × {buf:.0%} 缓冲"
        # 当前杠杆高于建议 → 标红
        cur_L = float(liq.get('leverage') or 0)
        signed_safe = None
        if safe_L is not None and cur_L > safe_L:
            signed_safe = -1.0
        elif safe_L is not None:
            signed_safe = 1.0
        cards.append((
            "建议最高安全杠杆",
            safe_val,
            safe_sub,
            signed_safe,
        ))

    cards.extend([
        ("日胜率", _fmt_pct(d_rate, 1) if d_rate is not None else "—", d_sub, d_rate),
        ("交易日占比", _fmt_pct(active_ratio, 1) if active_ratio is not None else "—", active_sub, None),
        ("月胜率", _fmt_pct(m_rate, 1) if m_rate is not None else "—", m_sub, m_rate),
        ("交易胜率", _fmt_pct(hit, 1), f"{trades} 笔交易" if trades else None, hit),
        ("交易次数", str(trades), None, None),
    ])

    parts = []
    for label, value, sub, signed in cards:
        tone = ""
        if signed is not None and isinstance(signed, (int, float)) and not np.isnan(signed):
            if signed > 0:
                tone = "positive"
            elif signed < 0:
                tone = "negative"
        sub_html = (
            f'<div class="card-sub">{html.escape(sub)}</div>' if sub else ""
        )
        parts.append(f'''
        <div class="card">
          <div class="card-label">{html.escape(label)}</div>
          <div class="card-value {tone}">{html.escape(value)}</div>
          {sub_html}
        </div>''')

    money = f'''
    <div class="card card-wide">
      <div class="card-label">资金</div>
      <div class="card-value">{html.escape(_fmt_money(capital_end))}</div>
      <div class="card-sub">初始 {html.escape(_fmt_money(initial_capital))}</div>
    </div>'''
    return money + "".join(parts)


def _monthly_table(
    rows: Sequence[Mapping[str, Any]],
    win_stats: Optional[Mapping[str, Any]] = None,
) -> str:
    if not rows:
        return ""
    body = []
    for r in rows:
        cls = "positive" if r['ret'] >= 0 else "negative"
        body.append(
            f"<tr>"
            f"<td>{html.escape(r['label'])}</td>"
            f"<td class='num'>{r['start']:,.0f}</td>"
            f"<td class='num'>{r['end']:,.0f}</td>"
            f"<td class='num {cls}'>{r['ret']*100:+.2f}%</td>"
            f"</tr>"
        )
    head_sub = "按自然月"
    if win_stats and win_stats.get('monthly_total'):
        head_sub = (
            f"月胜率 {_fmt_pct(win_stats['monthly_win_rate'], 1)} · "
            f"{win_stats['monthly_wins']}/{win_stats['monthly_total']} 月盈利"
        )
    return f'''
    <section class="panel">
      <header class="panel-head">
        <h2>月度回报</h2>
        <span>{html.escape(head_sub)}</span>
      </header>
      <div class="table-wrap">
        <table>
          <thead>
            <tr><th>月份</th><th>月初</th><th>月末</th><th>收益率</th></tr>
          </thead>
          <tbody>
            {''.join(body)}
          </tbody>
        </table>
      </div>
    </section>
    '''


def _single_day_card_sub(
    max_date: Any,
    top_rows: Optional[Sequence[Mapping[str, Any]]],
) -> Optional[str]:
    """单日回撤/亏损卡片副文案：最大日 + Top3。"""
    lines: list[str] = []
    d = _fmt_date(max_date)
    if d:
        lines.append(f"最大日 {d}")
    top = _fmt_top_days(top_rows, digits=2)
    if top:
        lines.append(f"Top3\n{top}")
    return "\n".join(lines) if lines else None


def _current_dd_card(metrics: Mapping[str, Any]) -> tuple[str, Optional[str], float]:
    """当前回撤卡片：(value, subtitle, signed)。"""
    pct = float(_safe_metric(metrics, 'current_drawdown_pct', 0.0) or 0.0)
    amt = float(_safe_metric(metrics, 'current_drawdown_amount', 0.0) or 0.0)
    peak = _fmt_date(metrics.get('current_peak_date'))
    days = int(_safe_metric(metrics, 'current_drawdown_trading_days', 0) or 0)
    lines: list[str] = []
    if peak:
        if days <= 0:
            lines.append(f"峰值即今日 {peak}")
            lines.append("权益位于历史高点")
        else:
            lines.append(f"峰值 {peak}")
            lines.append(f"距今 {days} 个交易日")
            lines.append(_fmt_signed_money(amt))
    signed = -pct if pct > 1e-8 else 0.0
    return _fmt_pct(pct), "\n".join(lines) if lines else None, signed


def _recent_trade_days_table(rows: Sequence[Mapping[str, Any]]) -> str:
    """近五个有成交日的盈亏表。"""
    if not rows:
        return ""
    has_trades = any(r.get('trades') is not None for r in rows)
    total_pnl = sum(float(r.get('pnl', 0) or 0) for r in rows)
    body = []
    for r in rows:
        pnl = float(r.get('pnl', 0) or 0)
        ret = float(r.get('ret', 0) or 0)
        cls = "positive" if pnl >= 0 else "negative"
        ntr = "—" if r.get('trades') is None else str(int(r['trades']))
        trade_td = f"<td class='num'>{html.escape(ntr)}</td>" if has_trades else ""
        body.append(
            f"<tr>"
            f"<td>{html.escape(_fmt_date(r.get('date')) or '?')}</td>"
            f"{trade_td}"
            f"<td class='num {cls}'>{html.escape(_fmt_signed_money(pnl))}</td>"
            f"<td class='num {cls}'>{html.escape(_fmt_pct(ret, 2))}</td>"
            f"</tr>"
        )
    trade_th = "<th>笔数</th>" if has_trades else ""
    n = len(rows)
    head_sub = f"合计 {_fmt_signed_money(total_pnl)} · 最近 {n} 个有成交日"
    return f'''
    <section class="panel">
      <header class="panel-head">
        <h2>近五个有交易日</h2>
        <span>{html.escape(head_sub)}</span>
      </header>
      <div class="table-wrap">
        <table>
          <thead>
            <tr><th>日期</th>{trade_th}<th>盈亏</th><th>当日收益</th></tr>
          </thead>
          <tbody>
            {''.join(body)}
          </tbody>
        </table>
      </div>
    </section>
    '''


def _mdd_card_sub(metrics: Mapping[str, Any], bh_mdd: Any) -> Optional[str]:
    """最大回撤卡片副文案：谷底日期、口径、回撤最深三日。"""
    lines: list[str] = []

    trough = _fmt_date(metrics.get('max_drawdown_date'))
    peak = _fmt_date(metrics.get('max_drawdown_start_date'))
    recovery = _fmt_date(metrics.get('max_drawdown_end_date'))
    if trough:
        if peak and recovery:
            lines.append(f"谷底 {trough}")
            lines.append(f"{peak}→{trough}→{recovery}")
        elif peak:
            lines.append(f"谷底 {trough}（峰值 {peak}，未恢复）")
        else:
            lines.append(f"谷底 {trough}")

    mdd = _safe_metric(metrics, 'mdd')
    mdd_eod = _safe_metric(metrics, 'mdd_eod_close_only')
    caliber: list[str] = []
    if mdd_eod is not None and mdd is not None and abs(float(mdd) - float(mdd_eod)) > 1e-4:
        caliber.append(f"含日内 · 日终 {_fmt_pct(mdd_eod)}")
    else:
        caliber.append("日终权益")
    if bh_mdd is not None:
        caliber.append(f"B&H {_fmt_pct(bh_mdd)}")
    if caliber:
        lines.append(" · ".join(caliber))

    top = _fmt_top_days(metrics.get('top_precise_drawdown_days'), digits=1)
    if top:
        lines.append(f"最深三日\n{top}")

    return "\n".join(lines) if lines else None


def _eod_drawdown_period(
    dates: Sequence[datetime],
    drawdowns_pct: Sequence[float],
) -> dict[str, Any]:
    """根据日终回撤序列定位峰值 / 谷底 / 恢复日（与图一致）。"""
    if not dates or not drawdowns_pct:
        return {}
    arr = np.asarray(drawdowns_pct, dtype=float)
    trough_i = int(np.nanargmin(arr))
    mdd_eod = float(-arr[trough_i] / 100.0)  # 正数比例
    # 谷底前最后一个回撤≈0 的点视为峰值（权益创新高）
    peak_i = 0
    for i in range(trough_i, -1, -1):
        if abs(arr[i]) < 1e-9:
            peak_i = i
            break
    recovery_i = None
    for i in range(trough_i + 1, len(arr)):
        if abs(arr[i]) < 1e-9:
            recovery_i = i
            break
    return {
        'mdd_eod': mdd_eod,
        'peak_date': dates[peak_i],
        'trough_date': dates[trough_i],
        'recovery_date': dates[recovery_i] if recovery_i is not None else None,
    }


def _drawdown_caption(
    metrics: Mapping[str, Any],
    dates: Sequence[datetime],
    drawdowns_pct: Sequence[float],
) -> str:
    """日终是否收复的说明；图本身按成交连线。"""
    eod = _eod_drawdown_period(dates, drawdowns_pct)
    parts: list[str] = []
    if eod:
        s = eod['peak_date'].strftime('%Y-%m-%d')
        b = eod['trough_date'].strftime('%Y-%m-%d')
        eod_pct = eod['mdd_eod']
        if eod.get('recovery_date') is not None:
            e = eod['recovery_date'].strftime('%Y-%m-%d')
            days = (eod['recovery_date'] - eod['peak_date']).days
            parts.append(f"日终 {eod_pct*100:.1f}%：{s}→{b}→{e}（{days}天）")
        else:
            parts.append(f"日终 {eod_pct*100:.1f}%：{s}→{b}（未恢复）")

    mdd = _safe_metric(metrics, 'mdd')
    mdd_eod_metric = _safe_metric(metrics, 'mdd_eod_close_only')
    precise_bottom = metrics.get('max_drawdown_date')
    if (
        mdd is not None
        and mdd_eod_metric is not None
        and abs(float(mdd) - float(mdd_eod_metric)) > 1e-4
        and precise_bottom is not None
    ):
        try:
            pb = pd.Timestamp(precise_bottom).strftime('%Y-%m-%d')
        except Exception:
            pb = str(precise_bottom)
        parts.append(f"主指标 {_fmt_pct(mdd)} 含日内（谷底 {pb}）")

    return " · ".join(parts) if parts else "相对历史峰值（日终）"


def _drawdown_chart_subtitle(dd_path: Mapping[str, Any], dd_cap: str) -> str:
    n = int(dd_path.get('n_trades') or 0)
    parts: list[str] = []
    if n:
        parts.append(f"按每笔平仓后权益 · {n} 笔 · 横轴为成交顺序")
    else:
        parts.append("日终权益")
    if dd_cap:
        parts.append(dd_cap)
    return " · ".join(parts)


def _build_html(
    *,
    title: str,
    subtitle: str,
    strategy_label: str,
    bh_label: str,
    dates: Sequence[datetime],
    capital: Sequence[float],
    daily_ret: Sequence[float],
    drawdowns: Sequence[float],
    bh_capital: Optional[Sequence[float]],
    metrics: Mapping[str, Any],
    initial_capital: float,
    monthly: Sequence[Mapping[str, Any]],
    win_stats: Optional[Mapping[str, Any]] = None,
    recent_days: Optional[Sequence[Mapping[str, Any]]] = None,
    dd_path: Optional[Mapping[str, Any]] = None,
) -> str:
    win_stats = dict(win_stats or {})
    dd_path = dict(dd_path or {})
    equity_svg = _svg_equity_chart(dates, capital, bh_capital, strategy_label, bh_label)
    dd_svg = _svg_drawdown_chart(
        dates,
        drawdowns,
        path_xs=dd_path.get('xs'),
        path_dds=dd_path.get('dds'),
        day_tips=dd_path.get('tips'),
        day_spans=dd_path.get('spans'),
        path_x_labels=dd_path.get('x_labels'),
    )
    bars_svg = _svg_daily_bars(dates, daily_ret)
    cards = _metric_cards(metrics, initial_capital, capital[-1], win_stats=win_stats)
    monthly_html = _monthly_table(monthly, win_stats=win_stats)
    recent_html = _recent_trade_days_table(recent_days or [])
    dd_cap = _drawdown_caption(metrics, dates, drawdowns)
    generated = datetime.now().strftime('%Y-%m-%d %H:%M')

    daily_ret_caption = "相对当日初资金"
    if win_stats.get('daily_total'):
        daily_ret_caption = (
            f"有交易日胜率 {_fmt_pct(win_stats['daily_win_rate'], 1)} · "
            f"{win_stats['daily_wins']}/{win_stats['daily_total']} 盈利"
        )
        if win_stats.get('calendar_days'):
            daily_ret_caption += (
                f" · 交易日占比 {_fmt_pct(win_stats['active_day_ratio'], 1)} "
                f"({win_stats['active_days']}/{win_stats['calendar_days']})"
            )

    return f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{html.escape(title)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin/>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600&family=Newsreader:opsz,wght@6..72,500;6..72,600&display=swap" rel="stylesheet"/>
<style>
:root {{
  --bg: #eceff3;
  --surface: #ffffff;
  --ink: #14171c;
  --ink-soft: #5c6570;
  --line: #d8dde4;
  --accent: #1f4e79;
  --muted: #8a94a1;
  --gain: #0f7a4e;
  --danger: #b42318;
  --danger-soft: #f5d0cb;
  --radius: 14px;
}}
* {{ box-sizing: border-box; }}
html, body {{
  margin: 0;
  padding: 0;
  background: var(--bg);
  color: var(--ink);
  font-family: "IBM Plex Sans", "PingFang SC", "Noto Sans SC", sans-serif;
  -webkit-font-smoothing: antialiased;
}}
.page {{
  max-width: 1080px;
  margin: 0 auto;
  padding: 36px 28px 64px;
}}
.hero {{
  display: flex;
  justify-content: space-between;
  align-items: flex-end;
  gap: 24px;
  margin-bottom: 28px;
}}
.hero h1 {{
  font-family: Newsreader, "Songti SC", serif;
  font-weight: 600;
  font-size: 2.35rem;
  letter-spacing: -0.02em;
  margin: 0 0 8px;
  line-height: 1.15;
}}
.hero p {{
  margin: 0;
  color: var(--ink-soft);
  font-size: 0.95rem;
}}
.hero-meta {{
  text-align: right;
  color: var(--ink-soft);
  font-size: 0.82rem;
  line-height: 1.55;
  white-space: nowrap;
}}
.metrics {{
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: 12px;
  margin-bottom: 22px;
}}
.card {{
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: var(--radius);
  padding: 14px 16px 12px;
  min-height: 92px;
}}
.card-wide {{ grid-column: span 1; }}
.card-label {{
  font-size: 0.72rem;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  color: var(--ink-soft);
  margin-bottom: 8px;
}}
.card-value {{
  font-size: 1.45rem;
  font-weight: 600;
  letter-spacing: -0.02em;
  font-variant-numeric: tabular-nums;
}}
.card-value.positive {{ color: var(--gain); }}
.card-value.negative {{ color: var(--danger); }}
.card-sub {{
  margin-top: 6px;
  font-size: 0.78rem;
  color: var(--muted);
  font-variant-numeric: tabular-nums;
  white-space: pre-line;
  line-height: 1.45;
}}
.panel {{
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: calc(var(--radius) + 2px);
  padding: 18px 18px 8px;
  margin-bottom: 16px;
}}
.panel-head {{
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  gap: 16px;
  margin-bottom: 4px;
  padding: 0 4px;
}}
.panel-head h2 {{
  margin: 0;
  font-size: 1.05rem;
  font-weight: 600;
}}
.panel-head span {{
  color: var(--ink-soft);
  font-size: 0.8rem;
}}
.chart-wrap {{
  position: relative;
}}
.chart {{
  width: 100%;
  height: auto;
  display: block;
}}
.hit {{
  fill: transparent;
  pointer-events: all;
  cursor: crosshair;
}}
.chart-tooltip {{
  position: absolute;
  z-index: 5;
  min-width: 132px;
  max-width: 240px;
  padding: 8px 10px;
  border-radius: 8px;
  background: rgba(20, 23, 28, 0.92);
  color: #f4f6f8;
  font-size: 0.78rem;
  line-height: 1.45;
  font-variant-numeric: tabular-nums;
  pointer-events: none;
  box-shadow: 0 6px 18px rgba(20, 23, 28, 0.18);
  transform: translate(-50%, calc(-100% - 10px));
}}
.chart-tooltip[hidden] {{ display: none; }}
.chart-tooltip .tip-date {{
  font-weight: 600;
  margin-bottom: 4px;
  letter-spacing: 0.01em;
}}
.chart-tooltip .tip-k {{
  color: #aeb6c0;
  margin-right: 6px;
}}
.grid {{ stroke: var(--line); stroke-width: 1; }}
.axis {{ stroke: #b8c0ca; stroke-width: 1; }}
.tick {{
  fill: var(--ink-soft);
  font-size: 11px;
  font-family: "IBM Plex Sans", sans-serif;
}}
.legend {{
  fill: var(--ink-soft);
  font-size: 12px;
  font-family: "IBM Plex Sans", sans-serif;
}}
.table-wrap {{ overflow-x: auto; padding: 8px 4px 16px; }}
table {{
  width: 100%;
  border-collapse: collapse;
  font-variant-numeric: tabular-nums;
}}
th, td {{
  text-align: left;
  padding: 10px 8px;
  border-bottom: 1px solid var(--line);
  font-size: 0.9rem;
}}
th {{
  color: var(--ink-soft);
  font-weight: 500;
  font-size: 0.75rem;
  text-transform: uppercase;
  letter-spacing: 0.04em;
}}
td.num {{ text-align: right; }}
.positive {{ color: var(--gain); }}
.negative {{ color: var(--danger); }}
.footer {{
  margin-top: 18px;
  color: var(--muted);
  font-size: 0.78rem;
}}
@media (max-width: 900px) {{
  .metrics {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
  .hero {{ flex-direction: column; align-items: flex-start; }}
  .hero-meta {{ text-align: left; white-space: normal; }}
}}
</style>
</head>
<body>
  <div class="page">
    <header class="hero">
      <div>
        <h1>{html.escape(title)}</h1>
        <p>{html.escape(subtitle)} · 按交易日复利权益</p>
      </div>
      <div class="hero-meta">
        <div>{html.escape(strategy_label)}</div>
        <div>生成于 {html.escape(generated)}</div>
      </div>
    </header>

    <section class="metrics">
      {cards}
    </section>

    {recent_html}

    <section class="panel">
      <header class="panel-head">
        <h2>权益曲线</h2>
        <span>策略 vs Buy &amp; Hold · 日终资金 · 悬停查看当日</span>
      </header>
      {equity_svg}
    </section>

    <section class="panel">
      <header class="panel-head">
        <h2>回撤</h2>
        <span>{html.escape(_drawdown_chart_subtitle(dd_path, dd_cap))}</span>
      </header>
      {dd_svg}
    </section>

    <section class="panel">
      <header class="panel-head">
        <h2>日收益率</h2>
        <span>{html.escape(daily_ret_caption)}</span>
      </header>
      {bars_svg}
    </section>

    {monthly_html}

    <p class="footer">Quantra equity report · SVG + 悬停提示，不依赖 matplotlib</p>
  </div>
<script>
(function () {{
  document.querySelectorAll('.chart-wrap').forEach(function (wrap) {{
    var tip = wrap.querySelector('.chart-tooltip');
    var svg = wrap.querySelector('svg.chart');
    if (!tip || !svg) return;

    function place(evt) {{
      var rect = wrap.getBoundingClientRect();
      var x = evt.clientX - rect.left;
      var y = evt.clientY - rect.top;
      var maxX = rect.width - 8;
      var minX = 8;
      tip.style.left = Math.max(minX, Math.min(maxX, x)) + 'px';
      tip.style.top = Math.max(8, y) + 'px';
    }}

    svg.querySelectorAll('rect.hit').forEach(function (hit) {{
      hit.addEventListener('mouseenter', function (evt) {{
        tip.innerHTML = hit.getAttribute('data-tip') || '';
        tip.hidden = false;
        place(evt);
      }});
      hit.addEventListener('mousemove', place);
      hit.addEventListener('mouseleave', function () {{
        tip.hidden = true;
      }});
    }});
  }});
}})();
</script>
</body>
</html>
'''