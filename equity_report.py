"""
独立的按日权益 / 收益可视化报告。

与 backtest 主逻辑完全隔离：只消费 daily_df / metrics / config，
生成自包含 HTML（纯 SVG + CSS，不依赖 matplotlib / plotly）。
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
        metrics=metrics,
        initial_capital=initial_capital,
        monthly=monthly,
        win_stats=win_stats,
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
    n = len(xs)
    if n == 0:
        return ""
    span = max(y_max - y_min, 1e-12)
    pts = []
    for i, (x, y) in enumerate(zip(xs, ys)):
        if y is None or (isinstance(y, float) and np.isnan(y)):
            continue
        px = x0 + (i / max(n - 1, 1)) * w
        py = y0 + h - ((float(y) - y_min) / span) * h
        pts.append(f"{px:.2f},{py:.2f}")
    return " ".join(pts)


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
    n = len(xs)
    if n == 0:
        return ""
    span = max(y_max - y_min, 1e-12)
    coords = []
    for i, y in enumerate(ys):
        if y is None or (isinstance(y, float) and np.isnan(y)):
            continue
        px = x0 + (i / max(n - 1, 1)) * w
        py = y0 + h - ((float(y) - y_min) / span) * h
        coords.append((px, py))
    if not coords:
        return ""
    by = y0 + h - ((baseline - y_min) / span) * h
    d = [f"M {coords[0][0]:.2f} {by:.2f}"]
    for px, py in coords:
        d.append(f"L {px:.2f} {py:.2f}")
    d.append(f"L {coords[-1][0]:.2f} {by:.2f} Z")
    return " ".join(d)


def _y_ticks(y_min: float, y_max: float, count: int = 5) -> list[float]:
    if y_max <= y_min:
        y_max = y_min + 1.0
    step = (y_max - y_min) / (count - 1)
    return [y_min + i * step for i in range(count)]


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

    return f'''
<svg viewBox="0 0 {W} {H}" class="chart" role="img" aria-label="Equity curve">
  {''.join(legend)}
  {''.join(grid)}
  <line x1="{x0}" y1="{y0+h}" x2="{x0+w}" y2="{y0+h}" class="axis"/>
  <line x1="{x0}" y1="{y0}" x2="{x0}" y2="{y0+h}" class="axis"/>
  {bh_line}
  <polyline points="{strat_pts}" fill="none" stroke="var(--accent)" stroke-width="2.4"
            stroke-linejoin="round" stroke-linecap="round"/>
  {''.join(x_labels)}
</svg>
'''


def _svg_drawdown_chart(dates: Sequence[datetime], drawdowns: Sequence[float]) -> str:
    W, H = 920, 200
    pad_l, pad_r, pad_t, pad_b = 64, 24, 20, 40
    x0, y0 = pad_l, pad_t
    w = W - pad_l - pad_r
    h = H - pad_t - pad_b

    y_min = min(min(drawdowns), -0.01)
    y_max = max(max(drawdowns), 0.0)
    # 回撤为负值，上方留一点余量
    if y_max < 0.5:
        y_max = 0.5
    xs = list(range(len(dates)))
    area = _area_path(xs, drawdowns, x0, y0, w, h, y_min, y_max, baseline=0.0)
    line = _polyline(xs, drawdowns, x0, y0, w, h, y_min, y_max)

    grid = []
    for ty in _y_ticks(y_min, y_max, 4):
        py = y0 + h - ((ty - y_min) / max(y_max - y_min, 1e-12)) * h
        grid.append(
            f'<line x1="{x0}" y1="{py:.2f}" x2="{x0+w}" y2="{py:.2f}" class="grid"/>'
            f'<text x="{x0-10}" y="{py+4:.2f}" class="tick" text-anchor="end">{ty:.1f}%</text>'
        )

    n = max(len(dates) - 1, 1)
    x_labels = []
    for i in np.linspace(0, len(dates) - 1, min(6, len(dates)), dtype=int):
        px = x0 + (i / n) * w
        x_labels.append(
            f'<text x="{px:.2f}" y="{y0+h+22}" class="tick" text-anchor="middle">'
            f'{dates[i].strftime("%m/%d")}</text>'
        )

    zero_y = y0 + h - ((0.0 - y_min) / max(y_max - y_min, 1e-12)) * h

    return f'''
<svg viewBox="0 0 {W} {H}" class="chart" role="img" aria-label="Drawdown">
  {''.join(grid)}
  <line x1="{x0}" y1="{zero_y:.2f}" x2="{x0+w}" y2="{zero_y:.2f}" class="axis"/>
  <path d="{area}" fill="var(--danger-soft)" opacity="0.9"/>
  <polyline points="{line}" fill="none" stroke="var(--danger)" stroke-width="1.8"
            stroke-linejoin="round"/>
  {''.join(x_labels)}
</svg>
'''


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

    return f'''
<svg viewBox="0 0 {W} {H}" class="chart" role="img" aria-label="Daily returns">
  {''.join(grid)}
  {''.join(bars)}
  {''.join(x_labels)}
</svg>
'''


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
    """
    回撤图标题旁说明：本图为日终路径；主指标若含日内则单独注明。
    """
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
) -> str:
    win_stats = dict(win_stats or {})
    equity_svg = _svg_equity_chart(dates, capital, bh_capital, strategy_label, bh_label)
    dd_svg = _svg_drawdown_chart(dates, drawdowns)
    bars_svg = _svg_daily_bars(dates, daily_ret)
    cards = _metric_cards(metrics, initial_capital, capital[-1], win_stats=win_stats)
    monthly_html = _monthly_table(monthly, win_stats=win_stats)
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
.chart {{
  width: 100%;
  height: auto;
  display: block;
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

    <section class="panel">
      <header class="panel-head">
        <h2>权益曲线</h2>
        <span>策略 vs Buy &amp; Hold · 日终资金</span>
      </header>
      {equity_svg}
    </section>

    <section class="panel">
      <header class="panel-head">
        <h2>回撤（日终权益）</h2>
        <span>{html.escape(dd_cap) if dd_cap else "相对历史峰值"}</span>
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

    <p class="footer">Quantra equity report · 纯 SVG 渲染，不依赖 matplotlib</p>
  </div>
</body>
</html>
'''
