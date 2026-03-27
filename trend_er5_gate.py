"""
与 Quantra/backtest.py 中 trend_er5（Kaufman 5 日效率比）及 entry_trend_filter er5 门控一致。
供各 simulate_*.py 共用，避免重复粘贴。
"""
import numpy as np
import pandas as pd

ENABLE_ENTRY_TREND_FILTER = True
ENTRY_TREND_ER5_MIN = 0.1
MINUTE_HISTORY_CALENDAR_DAYS_FOR_ER5 = 20


def history_days_back(lookback_days: int) -> int:
    """启用 er5 时拉长分钟 K 拉取窗口，保证足够交易日。"""
    b = lookback_days + 5
    if ENABLE_ENTRY_TREND_FILTER:
        return max(b, MINUTE_HISTORY_CALENDAR_DAYS_FOR_ER5)
    return b


def compute_trend_er5_latest(minute_df):
    """与 backtest.compute_daily_trend_features 中 trend_er5 一致；nan 表示不拦截。"""
    if minute_df is None or minute_df.empty:
        return np.nan
    if 'Volume' in minute_df.columns:
        d = minute_df.groupby('Date', as_index=False).agg(
            Close=('Close', 'last'),
            High=('High', 'max'),
            Low=('Low', 'min'),
            DayVol=('Volume', 'sum'),
        )
    else:
        d = minute_df.groupby('Date', as_index=False).agg(
            Close=('Close', 'last'),
            High=('High', 'max'),
            Low=('Low', 'min'),
        )
    d = d.sort_values('Date').reset_index(drop=True)
    c = d['Close']
    abs_diff = c.diff().abs()
    den = abs_diff.shift(1).rolling(5, min_periods=5).sum()
    num = (c.shift(1) - c.shift(6)).abs()
    d['trend_er5'] = (num / den.replace(0, np.nan)).where(den.notna() & (den > 0))
    v = d.iloc[-1]['trend_er5']
    return float(v) if pd.notna(v) else np.nan


def apply_er5_gate_to_signal(signal, minute_df, log_verbose, now_str):
    """
    噪声/VWAP 已得到 signal 后调用：未通过 er5 门控则置 0。
    log_verbose 为 True 时打印拦截原因。
    """
    if signal == 0 or not ENABLE_ENTRY_TREND_FILTER:
        return signal
    er5_val = compute_trend_er5_latest(minute_df)
    if pd.isna(er5_val) or er5_val >= ENTRY_TREND_ER5_MIN:
        return signal
    if log_verbose:
        print(
            f"[{now_str}] 趋势门控: trend_er5={er5_val:.4f} < {ENTRY_TREND_ER5_MIN}，跳过开仓"
        )
    return 0
