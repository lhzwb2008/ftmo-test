import pandas as pd
import numpy as np
from math import floor
import matplotlib.pyplot as plt
from datetime import datetime, time, timedelta, date
import random
import os
from plot_trading_day import plot_trading_day
from equity_report import render_equity_report

def calculate_vwap(turnovers, volumes, prices):
    """
    Calculate VWAP using cumulative turnover / cumulative volume
    """
    total_volume = sum(volumes)
    if total_volume > 0:
        return sum(turnovers) / total_volume
    else:
        return prices[-1]

def calculate_vwap_with_hl_average(highs, lows, volumes):
    """
    使用High和Low的平均值计算VWAP的近似值
    平均价格 = (High + Low) / 2
    """
    total_volume = sum(volumes)
    if total_volume > 0:
        # 计算每个时间点的High和Low平均价格
        hl_average_prices = [(h + l) / 2 for h, l in zip(highs, lows)]
        # 计算近似成交额
        turnovers = [avg_price * v for avg_price, v in zip(hl_average_prices, volumes)]
        return sum(turnovers) / total_volume
    else:
        # 如果没有成交量，返回最后一个时间点的HL平均价
        return (highs[-1] + lows[-1]) / 2 if highs and lows else 0

def calculate_vwap_with_turnover(day_df, current_index):
    """
    使用真实的Turnover数据计算VWAP，与simulate.py保持一致
    """
    # 获取当天到当前时间点的所有数据
    current_day_data = day_df.iloc[:current_index + 1].copy()
    
    # 按时间排序确保正确累计
    current_day_data = current_day_data.sort_values('DateTime')
    
    # 计算累计成交量和成交额
    cumulative_volume = current_day_data['Volume'].cumsum()
    cumulative_turnover = current_day_data['Turnover'].cumsum()
    
    # 计算VWAP: 累计成交额 / 累计成交量
    if cumulative_volume.iloc[-1] > 0:
        vwap = cumulative_turnover.iloc[-1] / cumulative_volume.iloc[-1]
    else:
        # 处理成交量为0的情况，使用当前收盘价
        vwap = current_day_data['Close'].iloc[-1]
    
    return vwap


def compute_daily_trend_features(minute_df):
    """
    从分钟数据聚合日收盘，计算多种趋势相关标量（仅内存使用，不写回 CSV）。

    列说明：
    - weekly_trend_strength: |mom5|/rvol5（与旧版一致）
    - trend_er5: Kaufman 式 5 日效率比，0~1，越高越像单边趋势
    - trend_linreg5_r2: 最近 5 个日收盘对时间的线性回归 R²，越高越像直线趋势
    - trend_dist_ma20: 前收盘相对 MA20 的偏离 (close_prev/ma20-1)
    - trend_range5: 前 5 个交易日日均 (High-Low)/Close，衡量日内波动相对价位
    - trend_rsi5: 日收盘 RSI(5)，shift(1) 对齐「开盘前可知」
    - trend_vol_ratio: 前一日全日成交量 / 前 20 日日均量
    - trend_mom5: 5 日动量（前收盘 / 6 日前收盘 - 1）
    - trend_dist_high20: 前收盘相对 20 日高点的距离 (close/high20-1)
    - trend_dist_low20: 前收盘相对 20 日低点的距离 (close/low20-1)
    - trend_price_rank60: 前收盘在 60 日高低区间中的位置 [0,1]，价格水位
    - trend_range1: 昨日日内振幅 (High-Low)/Close，shift(1) 对齐开盘前可知
    """
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
        d['DayVol'] = np.nan
    d = d.sort_values('Date').reset_index(drop=True)
    c = d['Close']
    d['close_prev'] = c.shift(1)
    d['mom5'] = d['close_prev'] / c.shift(6) - 1
    dr = c.pct_change()
    d['rvol5'] = dr.rolling(5, min_periods=5).std().shift(1)
    rvol = d['rvol5']
    d['weekly_trend_strength'] = (
        d['mom5'].abs() / rvol.replace(0, np.nan)
    ).where(rvol.notna() & (rvol > 0))

    abs_diff = c.diff().abs()
    den = abs_diff.shift(1).rolling(5, min_periods=5).sum()
    num = (c.shift(1) - c.shift(6)).abs()
    d['trend_er5'] = (num / den.replace(0, np.nan)).where(den.notna() & (den > 0))

    d['ma20'] = c.rolling(20, min_periods=20).mean().shift(1)
    d['trend_dist_ma20'] = (
        (d['close_prev'] / d['ma20'] - 1)
        .where(d['ma20'].notna() & (d['ma20'] > 0))
    )

    arr = c.values
    n = len(c)
    r2 = np.full(n, np.nan)
    x = np.arange(5, dtype=float)
    for i in range(5, n):
        y = arr[i - 5:i]
        if np.any(np.isnan(y)):
            continue
        y_mean = y.mean()
        x_mean = x.mean()
        b = np.sum((x - x_mean) * (y - y_mean)) / (np.sum((x - x_mean) ** 2) + 1e-12)
        a = y_mean - b * x_mean
        yhat = a + b * x
        ss_res = np.sum((y - yhat) ** 2)
        ss_tot = np.sum((y - y_mean) ** 2)
        r2[i] = 1 - ss_res / ss_tot if ss_tot > 1e-18 else 0.0
    d['trend_linreg5_r2'] = r2

    rng = (d['High'] - d['Low']) / c.replace(0, np.nan)
    d['trend_range5'] = rng.rolling(5, min_periods=5).mean().shift(1)
    d['trend_range1'] = rng.shift(1)  # 昨日振幅；开盘前可知，无未来函数

    delta = c.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    ag = gain.rolling(5, min_periods=5).mean()
    al = loss.rolling(5, min_periods=5).mean()
    rs_rsi = ag / al.replace(0, np.nan)
    d['trend_rsi5'] = (100 - (100 / (1 + rs_rsi))).shift(1)

    dv = d['DayVol']
    if dv.notna().any():
        vol_ma20 = dv.rolling(20, min_periods=20).mean().shift(1)
        d['trend_vol_ratio'] = (dv.shift(1) / vol_ma20.replace(0, np.nan)).where(vol_ma20.notna() & (vol_ma20 > 0))
    else:
        d['trend_vol_ratio'] = np.nan

    d['trend_mom5'] = d['mom5']

    high20 = c.rolling(20, min_periods=20).max().shift(1)
    low20 = c.rolling(20, min_periods=20).min().shift(1)
    d['trend_dist_high20'] = (d['close_prev'] / high20 - 1).where(high20.notna() & (high20 > 0))
    d['trend_dist_low20'] = (d['close_prev'] / low20 - 1).where(low20.notna() & (low20 > 0))

    roll_min60 = c.rolling(60, min_periods=20).min().shift(1)
    roll_max60 = c.rolling(60, min_periods=20).max().shift(1)
    span60 = roll_max60 - roll_min60
    d['trend_price_rank60'] = (
        (d['close_prev'] - roll_min60) / span60.replace(0, np.nan)
    ).where(span60.notna() & (span60 > 0)).clip(0, 1)

    return d[[
        'Date', 'weekly_trend_strength', 'trend_er5', 'trend_linreg5_r2', 'trend_dist_ma20',
        'trend_range5', 'trend_range1', 'trend_rsi5', 'trend_vol_ratio', 'trend_mom5',
        'trend_dist_high20', 'trend_dist_low20', 'trend_price_rank60',
    ]]


def _resolve_k_metric_column(metric):
    """K 侧调整规则中的 metric 名 -> price_df 列名。"""
    return {
        'weekly_sn': 'weekly_trend_strength',
        'er5': 'trend_er5',
        'linreg5_r2': 'trend_linreg5_r2',
        'dist_ma20': 'trend_dist_ma20',
        'dist_ma20_abs': 'trend_dist_ma20',
        'rsi5': 'trend_rsi5',
        'vol_ratio': 'trend_vol_ratio',
        'range5': 'trend_range5',
        'mom5': 'trend_mom5',
        'mom5_pos': 'trend_mom5',
        'dist_high20': 'trend_dist_high20',
        'dist_low20': 'trend_dist_low20',
        'price_rank60': 'trend_price_rank60',
        'intraday_ret': 'intraday_ret',
        'intraday_range_pos': 'intraday_range_pos',
        'sigma_vs_day_median': 'sigma_vs_day_median',
        'minutes_from_open': 'minutes_from_open',
        'sigma': 'sigma',
    }.get(metric, metric)


def _eval_k_condition_series(df, rule):
    """单条 K 调整规则 -> 布尔 Series（True 用 k_if_true）。"""
    metric = rule['metric']
    col = _resolve_k_metric_column(metric)
    if col not in df.columns:
        raise ValueError(f"缺少 K 调整列 {col}（metric={metric}）")

    v = df[col]
    if rule.get('gt_zero'):
        ok = v > 0
    elif 'min' in rule:
        ok = v >= rule['min']
    elif 'max' in rule:
        ok = v <= rule['max']
    elif 'min_abs' in rule:
        ok = v.abs() >= rule['min_abs']
    else:
        raise ValueError("k_side_adjustment 规则需要 min、max、min_abs 或 gt_zero 之一")
    return ok


def compute_k_multiplier_series(df, rule, default_k=1.0):
    """
    根据规则生成逐行 K 乘数。
    rule: {'metric', 'min'|'max'|'min_abs'|'gt_zero', 'k_if_true', 'k_if_false'}
    指标为 NaN 时回退 default_k（通常为 config 中的 K1/K2 基准）。
    """
    if rule is None:
        return pd.Series(float(default_k), index=df.index)

    col = _resolve_k_metric_column(rule['metric'])
    k_true = float(rule.get('k_if_true', rule.get('k_pass', 0.9)))
    k_false = float(rule.get('k_if_false', rule.get('k_fail', 1.1)))
    ok = _eval_k_condition_series(df, rule)
    nan_mask = df[col].isna()
    k = np.where(ok, k_true, k_false)
    k = np.where(nan_mask, float(default_k), k)
    return pd.Series(k, index=df.index)


def apply_k_bounds(price_df, config):
    """计算 K1_eff/K2_eff 及上下边界；支持 k_side_adjustment 按规则动态调整。"""
    k1_base = float(config.get('K1', 1))
    k2_base = float(config.get('K2', 1))
    adj = config.get('k_side_adjustment') if config.get('enable_k_side_adjustment', False) else None

    if adj is None:
        price_df['K1_eff'] = k1_base
        price_df['K2_eff'] = k2_base
    else:
        long_rule = adj.get('long')
        short_rule = adj.get('short')
        price_df['K1_eff'] = (
            compute_k_multiplier_series(price_df, long_rule, k1_base)
            if long_rule else k1_base
        )
        price_df['K2_eff'] = (
            compute_k_multiplier_series(price_df, short_rule, k2_base)
            if short_rule else k2_base
        )

    price_df['UpperBound'] = price_df['upper_ref'] * (1 + price_df['K1_eff'] * price_df['sigma'])
    price_df['LowerBound'] = price_df['lower_ref'] * (1 - price_df['K2_eff'] * price_df['sigma'])
    return price_df


def resolve_entry_trend_filter(config):
    """解析开仓趋势门控：优先 entry_trend_filter，其次兼容 enable_weekly_trend_strength_entry。"""
    f = config.get('entry_trend_filter')
    if f is not None:
        return f
    if config.get('enable_weekly_trend_strength_entry'):
        return {'metric': 'weekly_sn', 'min': config.get('min_weekly_trend_strength', 1.0)}
    return None


def _single_entry_trend_pass_series(df, filt):
    """
    单条门控规则 -> 布尔 Series。
    metric: weekly_sn | er5 | linreg5_r2 | dist_ma20_abs
    """
    metric = filt['metric']
    col = {
        'weekly_sn': 'weekly_trend_strength',
        'er5': 'trend_er5',
        'linreg5_r2': 'trend_linreg5_r2',
        'dist_ma20_abs': 'trend_dist_ma20',
        'rsi5': 'trend_rsi5',
        'vol_ratio': 'trend_vol_ratio',
        'range5': 'trend_range5',
        'range1': 'trend_range1',
        'sigma': 'sigma',
    }.get(metric, metric)
    if col not in df.columns:
        raise ValueError(f"缺少趋势列 {col}，请确认已 merge compute_daily_trend_features")

    v = df[col]
    if 'min' in filt:
        ok = v >= filt['min']
    elif 'max' in filt:
        ok = v <= filt['max']
    elif 'min_abs' in filt:
        ok = v.abs() >= filt['min_abs']
    else:
        raise ValueError("entry_trend_filter 需要 min、max 或 min_abs 之一")

    ok = ok | v.isna()
    return ok


def compute_entry_trend_pass_series(df, config):
    """
    根据 config['entry_trend_filter'] 生成与 df 等长的布尔 Series。
    - 单 dict：一条规则
    - list[dict]：多条规则同时满足（AND）
    特征为 NaN 时不拦截（与历史行为一致）。
    """
    filt = resolve_entry_trend_filter(config)
    if filt is None:
        return pd.Series(True, index=df.index)
    rules = filt if isinstance(filt, list) else [filt]
    ok = pd.Series(True, index=df.index)
    for r in rules:
        ok = ok & _single_entry_trend_pass_series(df, r)
    return ok


def compute_weekly_trend_strength_by_date(minute_df):
    """兼容旧名：仅返回 weekly_trend_strength。"""
    d = compute_daily_trend_features(minute_df)
    return d[['Date', 'weekly_trend_strength']]


def simulate_day(day_df, prev_close, allowed_times, position_size, config, day_start_capital=None):
    """
    模拟单日交易，使用噪声空间策略 + VWAP
    
    参数:
        day_df: 包含日内数据的DataFrame
        prev_close: 前一日收盘价
        allowed_times: 允许交易的时间列表
        position_size: 仓位大小
        config: 配置字典，包含所有交易参数
    """
    # 从配置中提取参数
    transaction_fee_per_share = config.get('transaction_fee_per_share', 0.01)
    enable_transaction_fees = config.get('enable_transaction_fees', True)  # 新增手续费开关
    trading_end_time = config.get('trading_end_time', (15, 50))
    max_positions_per_day = config.get('max_positions_per_day', float('inf'))
    print_details = config.get('print_trade_details', False)
    debug_time = config.get('debug_time', None)
    use_vwap = config.get('use_vwap', True)  # 新增VWAP开关参数
    # 滑点配置 - 简化为直接的买卖价差
    slippage_per_share = config.get('slippage_per_share', 0.02)  # 每股滑点，买入时多付，卖出时少收
    
    # 🛡️ 日内止损配置（与 the5ers / simulate 的 daily_loss_monitor 对齐）
    # - from_day_start: 当日已实现+未实现亏损相对日初资金 ≥ 限额（旧口径）
    # - peak_to_trough: 当日权益峰→谷回撤金额 ≥ 限额（卡住报告里的「单日峰谷回撤」）
    # - both: 任一触发即强平（更严，推荐与 prop 双口径一致）
    enable_intraday_stop_loss = config.get('enable_intraday_stop_loss', False)
    intraday_stop_loss_pct = config.get('intraday_stop_loss_pct', 0.04)  # 未指定 max_daily_loss_amount 时：pct * 当日起始资金
    intraday_stop_loss_mode = str(config.get('intraday_stop_loss_mode', 'both')).lower()
    max_daily_loss_amount_cfg = config.get('max_daily_loss_amount')  # None 则按 pct 推导；<=0 与 simulate 一样视为不启用金额阈值
    initial_capital = config.get('initial_capital', 100000)  # 初始资金，用于计算日内损失
    
    # 🎯 动态追踪止盈配置 - 新增功能
    enable_trailing_take_profit = config.get('enable_trailing_take_profit', False)  # 是否启用动态追踪止盈
    trailing_tp_activation_pct = config.get('trailing_tp_activation_pct', 0.005)  # 激活追踪止盈的最低浮盈百分比，默认0.5%
    trailing_tp_callback_pct = config.get('trailing_tp_callback_pct', 0.5)  # 保护的利润比例，默认保护50%的浮盈

    # 🛡️ 单笔止损：相对开仓价固定百分比；K 内用 Low/High 检测（与日内止损一致）
    enable_per_trade_stop_loss = config.get('enable_per_trade_stop_loss', False)
    per_trade_stop_loss_pct = config.get('per_trade_stop_loss_pct', 0.03)

    # 开仓趋势门控（可选）：由 run_backtest 写入 entry_trend_pass；无门控时为 True
    def apply_slippage(price, is_buy, is_entry):
        """
        应用滑点到交易价格 - 简化版本
        参数:
            price: 原始价格
            is_buy: 是否为买入操作
            is_entry: 是否为开仓操作
        返回:
            调整后的价格
        """
        if slippage_per_share == 0:
            return price
        
        # 简化逻辑：买入时价格上升，卖出时价格下降
        if is_buy:
            return price + slippage_per_share  # 买入多付
        else:
            return price - slippage_per_share  # 卖出少收
    
    def _resolve_max_daily_loss_amount():
        """与 simulate 一致：固定美元；未配置时用 intraday_stop_loss_pct * 当日起始权益。"""
        if max_daily_loss_amount_cfg is not None:
            return float(max_daily_loss_amount_cfg)
        if enable_intraday_stop_loss:
            return float(intraday_stop_loss_pct) * float(day_start_capital)
        return 0.0

    def _unrealized_pnl_at_mark(mark_px):
        if position == 0 or np.isnan(entry_price):
            return 0.0
        if position == 1:
            return float(position_size) * (float(mark_px) - float(entry_price))
        return float(position_size) * (float(entry_price) - float(mark_px))

    def _current_daily_pnl_total(mark_px):
        """当日已实现 + 未实现（与 simulate: DAILY_PNL + unrealized 一致）。"""
        return float(current_day_pnl) + _unrealized_pnl_at_mark(mark_px)

    def _worst_mark_price(high_px, low_px, close_px):
        """本根 K 内对当前持仓最不利的标记价；无仓时用 Close。"""
        if position == 1:
            return float(low_px)
        if position == -1:
            return float(high_px)
        return float(close_px)

    def _mark_price_for_equity(target_equity):
        """
        反推使「日初+已实现+未实现 ≈ target_equity」的标记价（忽略手续费，供限额触及时定价）。
        无仓或仓位为 0 时返回 nan。
        """
        if position == 0 or position_size <= 0 or np.isnan(entry_price):
            return np.nan
        target_unrealized = float(target_equity) - float(day_start_capital) - float(current_day_pnl)
        if position == 1:
            return float(entry_price) + target_unrealized / float(position_size)
        return float(entry_price) - target_unrealized / float(position_size)

    def _after_trade_close_add_realized(pnl):
        """平仓后累加已实现；按配置检查日初亏损 / 峰谷回撤是否触限。"""
        nonlocal current_day_pnl, intraday_stop_triggered
        nonlocal intraday_capital_peak, intraday_capital_high, intraday_capital_low, intraday_max_drawdown
        current_day_pnl += pnl
        equity = float(day_start_capital) + float(current_day_pnl)
        if equity > intraday_capital_peak:
            intraday_capital_peak = equity
        if equity > intraday_capital_high:
            intraday_capital_high = equity
        if equity < intraday_capital_low:
            intraday_capital_low = equity
        dd = intraday_capital_peak - equity
        if dd > intraday_max_drawdown:
            intraday_max_drawdown = dd

        m = _resolve_max_daily_loss_amount()
        if (
            enable_intraday_stop_loss
            and not intraday_stop_triggered
            and m > 0
        ):
            breach_start = (
                _check_day_start
                and current_day_pnl < 0
                and abs(current_day_pnl) >= m
            )
            breach_peak = _check_peak_trough and dd >= m
            if breach_start or breach_peak:
                intraday_stop_triggered = True
                if print_details:
                    why = []
                    if breach_peak:
                        why.append('峰谷')
                    if breach_start:
                        why.append('日初')
                    print(
                        f"🛡️ 日内止损（已实现/{'+'.join(why)}）: "
                        f"当日盈亏=${current_day_pnl:.2f}, 峰谷=${dd:.2f}, 限额=${m:.2f}"
                    )
    
    position = 0  # 0: 无仓位, 1: 多头, -1: 空头
    entry_price = np.nan
    trailing_stop = np.nan
    trade_entry_time = None
    trades = []
    positions_opened_today = 0  # 今日开仓计数器
    
    # 🎯 动态追踪止盈相关变量
    max_profit_price = np.nan  # 持仓期间的最优价格（多头：最高价，空头：最低价）
    trailing_tp_activated = False  # 追踪止盈是否已激活
    dynamic_take_profit_level = np.nan  # 动态止盈线
    
    # 🛡️ 日内止损监控变量
    if day_start_capital is None:
        day_start_capital = initial_capital  # 如果没有传入，使用初始资金
    current_day_pnl = 0  # 当日累计已实现盈亏（与 simulate 的 DAILY_PNL 对应）
    intraday_stop_triggered = False  # 是否已触发日内止损（与 DAILY_STOP_TRIGGERED 对应）

    # 与 simulate 一致：<=0 则不按金额启用日内止损（仍可由 enable_intraday_stop_loss 总开关关闭）
    _max_daily_loss_amt = _resolve_max_daily_loss_amount()
    _daily_stop_active = (
        enable_intraday_stop_loss and _max_daily_loss_amt > 0
    )
    _stop_mode = intraday_stop_loss_mode
    if _stop_mode in ('day_start', 'from_day_start', 'start'):
        _check_day_start = True
        _check_peak_trough = False
    elif _stop_mode in ('peak_to_trough', 'peak', 'mdd'):
        _check_day_start = False
        _check_peak_trough = True
    else:
        # both / 未知 → 双口径
        _check_day_start = True
        _check_peak_trough = True

    # 📊 日内资金回撤追踪
    intraday_capital_peak = day_start_capital  # 日内资金峰值
    intraday_max_drawdown = 0  # 日内最大回撤金额
    intraday_capital_low = day_start_capital  # 日内最低资金
    intraday_capital_high = day_start_capital  # 日内最高资金
    
    # 调试时间点标记，确保只打印一次
    debug_printed = False
    
    end_time_str = f"{trading_end_time[0]:02d}:{trading_end_time[1]:02d}"
    
    def _execute_intraday_stop(row, exit_mark, reason_tag, detail_msg):
        """强平并标记当日停开。返回 True 表示已处理（调用方应 continue）。"""
        nonlocal position, current_day_pnl, intraday_stop_triggered
        nonlocal trailing_stop, max_profit_price, trailing_tp_activated, dynamic_take_profit_level
        nonlocal trade_entry_time

        if position != 0:
            exit_time = row['DateTime']
            exit_price = apply_slippage(exit_mark, is_buy=(position == -1), is_entry=False)
            if enable_transaction_fees:
                transaction_fees = max(position_size * transaction_fee_per_share * 2, 2.16)
            else:
                transaction_fees = 0
            if position == 1:
                pnl = position_size * (exit_price - entry_price) - transaction_fees
            else:
                pnl = position_size * (entry_price - exit_price) - transaction_fees
            if print_details:
                print(detail_msg)
            trades.append({
                'entry_time': trade_entry_time,
                'exit_time': exit_time,
                'side': 'Long' if position == 1 else 'Short',
                'entry_price': entry_price,
                'exit_price': exit_price,
                'pnl': pnl,
                'exit_reason': reason_tag,
                'position_size': position_size,
                'transaction_fees': transaction_fees,
                'vwap_influenced': False,
                'stop_level': _max_daily_loss_amt,
                'upper_bound': upper if position == 1 else np.nan,
                'lower_bound': lower_bound if position == -1 else np.nan,
                'vwap_value': np.nan
            })
            current_day_pnl += pnl
            position = 0
            trailing_stop = np.nan
            max_profit_price = np.nan
            trailing_tp_activated = False
            dynamic_take_profit_level = np.nan
        elif print_details:
            print(detail_msg)
        intraday_stop_triggered = True
        return True

    for idx, row in day_df.iterrows():
        current_time = row['Time']
        price = row['Close']
        high = row['High']
        low = row['Low']
        volume = row['Volume']
        upper = row['UpperBound']
        lower_bound = row['LowerBound']
        sigma = row.get('sigma', 0)
        
        # 📊 计算当前K线的账户资金（用于日内回撤计算 / 峰谷止损）
        # 如果已触发止损，不再更新回撤统计
        stop_now = False
        stop_exit_mark = None
        stop_reason = None
        stop_detail = None
        if not intraday_stop_triggered:
            # 使用K线的High和Low来计算最好和最坏情况
            if position == 1:  # 多头持仓
                best_unrealized = position_size * (high - entry_price)
                worst_unrealized = position_size * (low - entry_price)
            elif position == -1:  # 空头持仓
                best_unrealized = position_size * (entry_price - low)
                worst_unrealized = position_size * (entry_price - high)
            else:  # 无持仓
                best_unrealized = 0
                worst_unrealized = 0
            
            current_best_capital = day_start_capital + current_day_pnl + best_unrealized
            current_worst_capital = day_start_capital + current_day_pnl + worst_unrealized
            
            # 先抬升峰值，再量峰谷（同根 K 内保守假设：先冲高再回落）
            if current_best_capital > intraday_capital_peak:
                intraday_capital_peak = current_best_capital

            # 🛡️ 双口径止损判定；触及时将谷底钳制在限额上，避免同根 K 的 High/Low 把统计抬过阈值
            if _daily_stop_active:
                floor_from_peak = intraday_capital_peak - _max_daily_loss_amt
                floor_from_start = day_start_capital - _max_daily_loss_amt
                breached_peak = _check_peak_trough and current_worst_capital <= floor_from_peak
                breached_start = _check_day_start and current_worst_capital <= floor_from_start
                if breached_peak or breached_start:
                    if breached_peak and breached_start:
                        equity_floor = max(floor_from_peak, floor_from_start)
                        reason_tag = 'Intraday Stop Loss'
                        why = '峰谷+日初'
                    elif breached_peak:
                        equity_floor = floor_from_peak
                        reason_tag = 'Intraday Peak Drawdown Stop'
                        why = '峰谷回撤'
                    else:
                        equity_floor = floor_from_start
                        reason_tag = 'Intraday Stop Loss'
                        why = '日初亏损'
                    # 统计与出场均按触限权益，而不是继续吃满 bar 极端价
                    current_worst_capital = max(current_worst_capital, equity_floor)
                    mark = _mark_price_for_equity(equity_floor)
                    if np.isnan(mark):
                        mark = _worst_mark_price(high, low, price)
                    else:
                        # 标记价夹在本根 K 的 High/Low 内，避免算出 bar 外价格
                        mark = float(min(max(mark, float(low)), float(high)))
                    stop_now = True
                    stop_exit_mark = mark
                    stop_reason = reason_tag
                    stop_detail = (
                        f"🛡️ 日内止损强平（{why}）! 时间: {current_time}, "
                        f"限额=${_max_daily_loss_amt:.2f}, 峰值=${intraday_capital_peak:.2f}, "
                        f"触限权益=${equity_floor:.2f}"
                    )

            current_drawdown = intraday_capital_peak - current_worst_capital
            if current_drawdown > intraday_max_drawdown:
                intraday_max_drawdown = current_drawdown

            if current_best_capital > intraday_capital_high:
                intraday_capital_high = current_best_capital
            if current_worst_capital < intraday_capital_low:
                intraday_capital_low = current_worst_capital

        if stop_now:
            _execute_intraday_stop(row, stop_exit_mark, stop_reason, stop_detail)
            continue

        # # 调试特定时间点
        # if debug_time is not None and current_time >= debug_time and not debug_printed:
        #     date_str = row['DateTime'].strftime('%Y-%m-%d')
        #     print(f"\n===== 调试信息 [{date_str} {current_time}] =====")
        #     print(f"价格: {price:.6f}")
        #     print(f"上边界: {upper:.6f}")
        #     print(f"下边界: {lower:.6f}")
        #     print(f"Sigma值: {sigma:.6f}")
        #     print(f"VWAP: {calculate_vwap(prices, volumes, prices):.6f}")
        #     print("=====================================\n")
        #     debug_printed = True  # 确保只打印一次
        
        # 计算当前VWAP（使用真实的Turnover数据）
        # 获取当前行在DataFrame中的位置索引
        current_index = day_df.index.get_loc(idx)
        
        # 检查是否有Turnover字段
        if 'Turnover' in day_df.columns:
            vwap = calculate_vwap_with_turnover(day_df, current_index)
        else:
            # 如果没有Turnover字段，回退到使用HL平均值的方法
            highs = day_df.iloc[:current_index + 1]['High'].tolist()
            lows = day_df.iloc[:current_index + 1]['Low'].tolist()
            volumes = day_df.iloc[:current_index + 1]['Volume'].tolist()
            vwap = calculate_vwap_with_hl_average(highs, lows, volumes)
        
        # 🛡️ 日内止损检查 - 如果已触发止损，当日不再开仓
        if enable_intraday_stop_loss and intraday_stop_triggered:
            # 已触发日内止损，跳过所有开仓逻辑
            pass
        # 在允许时间内的入场信号（trading_end_time只能平仓不能开仓）
        elif position == 0 and current_time in allowed_times and current_time != end_time_str and positions_opened_today < max_positions_per_day:
            etp = row.get('entry_trend_pass', True)
            trend_ok = True if pd.isna(etp) else bool(etp)
            # 检查潜在多头入场
            if use_vwap:
                # 使用VWAP条件
                long_entry_condition = price > upper and price > vwap
            else:
                # 不使用VWAP条件
                long_entry_condition = price > upper
            long_entry_condition = long_entry_condition and trend_ok
                
            if long_entry_condition:
                # 打印边界计算详情（如果需要）
                if print_details:
                    date_str = row['DateTime'].strftime('%Y-%m-%d')
                    sigma = row.get('sigma', 0)
                    upper_ref = row.get('upper_ref', 0)
                    lower_ref = row.get('lower_ref', 0)
                    day_open = row.get('day_open', 0)
                    
                    print(f"\n交易点位详情 [{date_str} {current_time}] - 多头入场:")
                    print(f"  价格: {price:.2f} > 上边界: {upper:.2f} 且 > VWAP: {vwap:.2f}")
                    print(f"  边界计算详情:")
                    print(f"    - 日开盘价: {day_open:.2f}, 前日收盘价: {prev_close:.2f}")
                    print(f"    - 上边界参考价: max({day_open:.2f}, {prev_close:.2f}) = {upper_ref:.2f}")
                    print(f"    - 下边界参考价: min({day_open:.2f}, {prev_close:.2f}) = {lower_ref:.2f}")
                    print(f"    - Sigma值: {sigma:.6f}")
                    print(f"    - 上边界计算: {upper_ref:.2f} * (1 + {sigma:.6f}) = {upper:.2f}")
                    print(f"    - 下边界计算: {lower_ref:.2f} * (1 - {sigma:.6f}) = {lower_bound:.2f}")
                
                # 允许多头入场
                position = 1
                entry_price = apply_slippage(price, is_buy=True, is_entry=True)  # 多头开仓是买入
                trade_entry_time = row['DateTime']
                positions_opened_today += 1  # 增加开仓计数器
                # 初始止损设置
                if use_vwap:
                    trailing_stop = max(upper, vwap)
                else:
                    trailing_stop = upper
                    
            # 检查潜在空头入场
            if use_vwap:
                # 使用VWAP条件
                short_entry_condition = price < lower_bound and price < vwap
            else:
                # 不使用VWAP条件
                short_entry_condition = price < lower_bound
            short_entry_condition = short_entry_condition and trend_ok
                
            if short_entry_condition:
                # 打印边界计算详情（如果需要）
                if print_details:
                    date_str = row['DateTime'].strftime('%Y-%m-%d')
                    sigma = row.get('sigma', 0)
                    upper_ref = row.get('upper_ref', 0)
                    lower_ref = row.get('lower_ref', 0)
                    day_open = row.get('day_open', 0)
                    
                    print(f"\n交易点位详情 [{date_str} {current_time}] - 空头入场:")
                    print(f"  价格: {price:.2f} < 下边界: {lower_bound:.2f} 且 < VWAP: {vwap:.2f}")
                    print(f"  边界计算详情:")
                    print(f"    - 日开盘价: {day_open:.2f}, 前日收盘价: {prev_close:.2f}")
                    print(f"    - 上边界参考价: max({day_open:.2f}, {prev_close:.2f}) = {upper_ref:.2f}")
                    print(f"    - 下边界参考价: min({day_open:.2f}, {prev_close:.2f}) = {lower_ref:.2f}")
                    print(f"    - Sigma值: {sigma:.6f}")
                    print(f"    - 上边界计算: {upper_ref:.2f} * (1 + {sigma:.6f}) = {upper:.2f}")
                    print(f"    - 下边界计算: {lower_ref:.2f} * (1 - {sigma:.6f}) = {lower_bound:.2f}")
                
                # 允许空头入场
                position = -1
                entry_price = apply_slippage(price, is_buy=False, is_entry=True)  # 空头开仓是卖出
                trade_entry_time = row['DateTime']
                positions_opened_today += 1  # 增加开仓计数器
                # 初始止损设置
                if use_vwap:
                    trailing_stop = min(lower_bound, vwap)
                else:
                    trailing_stop = lower_bound
        
        # 更新止损并检查出场信号
        if position != 0:
            if position == 1:  # 多头仓位
                # 计算当前时刻的止损水平
                if use_vwap:
                    current_stop = max(upper, vwap)
                    vwap_influenced = vwap > upper  # 如果VWAP > 上边界，则VWAP影响了止损
                else:
                    current_stop = upper
                    vwap_influenced = False  # 不使用VWAP时，VWAP不影响止损
                
                # 🎯 动态追踪止盈逻辑 - 多头
                trailing_tp_exit = False
                if enable_trailing_take_profit:
                    # 更新最优价格（使用K线的最高价）
                    if np.isnan(max_profit_price) or high > max_profit_price:
                        max_profit_price = high
                    
                    # 计算当前浮盈百分比（使用最高价）
                    current_profit_pct = (max_profit_price - entry_price) / entry_price
                    
                    # 检查是否激活追踪止盈
                    if not trailing_tp_activated and current_profit_pct >= trailing_tp_activation_pct:
                        trailing_tp_activated = True
                        if print_details:
                            print(f"🎯 追踪止盈激活！时间: {current_time}, 浮盈: {current_profit_pct*100:.2f}%, 最高价: {max_profit_price:.2f}")
                    
                    # 如果追踪止盈已激活，计算动态止盈线
                    if trailing_tp_activated:
                        # 动态止盈线 = 入场价 + (最大浮盈 * 保护比例)
                        # 保护比例为 trailing_tp_callback_pct（例如0.5表示保护50%的利润）
                        protected_profit = (max_profit_price - entry_price) * trailing_tp_callback_pct
                        dynamic_take_profit_level = entry_price + protected_profit
                        
                        # 检查是否触发动态止盈（使用当前收盘价）
                        if price <= dynamic_take_profit_level:
                            trailing_tp_exit = True
                            if print_details:
                                print(f"🎯 动态止盈触发！时间: {current_time}, 价格: {price:.2f} <= 止盈线: {dynamic_take_profit_level:.2f}")
                                print(f"   最高价: {max_profit_price:.2f}, 保护利润: {protected_profit:.2f}")
                
                # 🛡️ 单笔止损 - 多头：相对开仓价下跌超过阈值
                per_trade_sl_exit = False
                per_trade_sl_level = np.nan
                if enable_per_trade_stop_loss and per_trade_stop_loss_pct > 0:
                    per_trade_sl_level = entry_price * (1 - per_trade_stop_loss_pct)
                    per_trade_sl_exit = low <= per_trade_sl_level

                strategy_exit = price < current_stop or trailing_tp_exit
                exit_condition = (strategy_exit and current_time in allowed_times) or per_trade_sl_exit

                # 检查是否出场
                if exit_condition:
                    # 确定出场原因
                    if trailing_tp_exit:
                        exit_reason = 'Trailing Take Profit'
                    elif per_trade_sl_exit:
                        exit_reason = 'Per-Trade Stop Loss'
                    else:
                        exit_reason = 'Stop Loss'

                    exit_raw_price = per_trade_sl_level if per_trade_sl_exit and not trailing_tp_exit else price
                    
                    # 打印出场详情（如果需要）
                    if print_details:
                        date_str = row['DateTime'].strftime('%Y-%m-%d')
                        print(f"\n交易点位详情 [{date_str} {current_time}] - 多头出场 ({exit_reason}):")
                        if trailing_tp_exit:
                            print(f"  价格: {price:.2f} <= 动态止盈线: {dynamic_take_profit_level:.2f}")
                            print(f"  最高价: {max_profit_price:.2f}, 保护比例: {trailing_tp_callback_pct*100:.0f}%")
                        elif per_trade_sl_exit:
                            print(f"  K线最低价: {low:.2f} <= 单笔止损: {per_trade_sl_level:.2f} ({per_trade_stop_loss_pct*100:.1f}%)")
                        else:
                            print(f"  价格: {price:.2f} < 当前止损: {current_stop:.2f}")
                            print(f"  止损计算: max(上边界={upper:.2f}, VWAP={vwap:.2f}) = {current_stop:.2f}")
                        print(f"  买入价: {entry_price:.2f}, 卖出价: {exit_raw_price:.2f}, 股数: {position_size}")
                    
                    # 平仓多头
                    exit_time = row['DateTime']
                    exit_price = apply_slippage(exit_raw_price, is_buy=False, is_entry=False)  # 多头平仓是卖出
                    # 计算交易费用（开仓和平仓）
                    if enable_transaction_fees:
                        transaction_fees = max(position_size * transaction_fee_per_share * 2, 2.16)  # 买入和卖出费用，最低2.16
                    else:
                        transaction_fees = 0  # 关闭手续费
                    pnl = position_size * (exit_price - entry_price) - transaction_fees
                    
                    trades.append({
                        'entry_time': trade_entry_time,
                        'exit_time': exit_time,
                        'side': 'Long',
                        'entry_price': entry_price,
                        'exit_price': exit_price,
                        'pnl': pnl,
                        'exit_reason': exit_reason,
                        'position_size': position_size,
                        'transaction_fees': transaction_fees,
                        'vwap_influenced': vwap_influenced,  # 新增字段
                        'stop_level': per_trade_sl_level if per_trade_sl_exit else current_stop,
                        'upper_bound': upper,
                        'vwap_value': vwap if use_vwap else np.nan,
                        'trailing_tp_activated': trailing_tp_activated,  # 🎯 动态止盈是否激活
                        'max_profit_price': max_profit_price if not np.isnan(max_profit_price) else np.nan,  # 🎯 最高价
                        'dynamic_tp_level': dynamic_take_profit_level if not np.isnan(dynamic_take_profit_level) else np.nan  # 🎯 动态止盈线
                    })
                    
                    # 🛡️ 检查日内止损
                    _after_trade_close_add_realized(pnl)
                    
                    position = 0
                    trailing_stop = np.nan
                    # 🎯 重置动态追踪止盈变量
                    max_profit_price = np.nan
                    trailing_tp_activated = False
                    dynamic_take_profit_level = np.nan
                    
            elif position == -1:  # 空头仓位
                # 计算当前时刻的止损水平
                if use_vwap:
                    current_stop = min(lower_bound, vwap)
                    vwap_influenced = vwap < lower_bound  # 如果VWAP < 下边界，则VWAP影响了止损
                else:
                    current_stop = lower_bound
                    vwap_influenced = False  # 不使用VWAP时，VWAP不影响止损
                
                # 🎯 动态追踪止盈逻辑 - 空头
                trailing_tp_exit = False
                if enable_trailing_take_profit:
                    # 更新最优价格（使用K线的最低价，空头时低价是有利的）
                    if np.isnan(max_profit_price) or low < max_profit_price:
                        max_profit_price = low
                    
                    # 计算当前浮盈百分比（空头：入场价 - 最低价）
                    current_profit_pct = (entry_price - max_profit_price) / entry_price
                    
                    # 检查是否激活追踪止盈
                    if not trailing_tp_activated and current_profit_pct >= trailing_tp_activation_pct:
                        trailing_tp_activated = True
                        if print_details:
                            print(f"🎯 追踪止盈激活！时间: {current_time}, 浮盈: {current_profit_pct*100:.2f}%, 最低价: {max_profit_price:.2f}")
                    
                    # 如果追踪止盈已激活，计算动态止盈线
                    if trailing_tp_activated:
                        # 动态止盈线 = 入场价 - (最大浮盈 * 保护比例)
                        # 空头的止盈线在入场价下方，当价格上涨超过止盈线时触发
                        protected_profit = (entry_price - max_profit_price) * trailing_tp_callback_pct
                        dynamic_take_profit_level = entry_price - protected_profit
                        
                        # 检查是否触发动态止盈（使用当前收盘价）
                        if price >= dynamic_take_profit_level:
                            trailing_tp_exit = True
                            if print_details:
                                print(f"🎯 动态止盈触发！时间: {current_time}, 价格: {price:.2f} >= 止盈线: {dynamic_take_profit_level:.2f}")
                                print(f"   最低价: {max_profit_price:.2f}, 保护利润: {protected_profit:.2f}")
                
                # 🛡️ 单笔止损 - 空头：相对开仓价上涨超过阈值
                per_trade_sl_exit = False
                per_trade_sl_level = np.nan
                if enable_per_trade_stop_loss and per_trade_stop_loss_pct > 0:
                    per_trade_sl_level = entry_price * (1 + per_trade_stop_loss_pct)
                    per_trade_sl_exit = high >= per_trade_sl_level

                strategy_exit = price > current_stop or trailing_tp_exit
                exit_condition = (strategy_exit and current_time in allowed_times) or per_trade_sl_exit

                # 检查是否出场
                if exit_condition:
                    # 确定出场原因
                    if trailing_tp_exit:
                        exit_reason = 'Trailing Take Profit'
                    elif per_trade_sl_exit:
                        exit_reason = 'Per-Trade Stop Loss'
                    else:
                        exit_reason = 'Stop Loss'

                    exit_raw_price = per_trade_sl_level if per_trade_sl_exit and not trailing_tp_exit else price
                    
                    # 打印出场详情（如果需要）
                    if print_details:
                        date_str = row['DateTime'].strftime('%Y-%m-%d')
                        print(f"\n交易点位详情 [{date_str} {current_time}] - 空头出场 ({exit_reason}):")
                        if trailing_tp_exit:
                            print(f"  价格: {price:.2f} >= 动态止盈线: {dynamic_take_profit_level:.2f}")
                            print(f"  最低价: {max_profit_price:.2f}, 保护比例: {trailing_tp_callback_pct*100:.0f}%")
                        elif per_trade_sl_exit:
                            print(f"  K线最高价: {high:.2f} >= 单笔止损: {per_trade_sl_level:.2f} ({per_trade_stop_loss_pct*100:.1f}%)")
                        else:
                            print(f"  价格: {price:.2f} > 当前止损: {current_stop:.2f}")
                            print(f"  止损计算: min(下边界={lower_bound:.2f}, VWAP={vwap:.2f}) = {current_stop:.2f}")
                        print(f"  卖出价: {entry_price:.2f}, 买入价: {exit_raw_price:.2f}, 股数: {position_size}")
                    
                    # 平仓空头
                    exit_time = row['DateTime']
                    exit_price = apply_slippage(exit_raw_price, is_buy=True, is_entry=False)  # 空头平仓是买入
                    # 计算交易费用（开仓和平仓）
                    if enable_transaction_fees:
                        transaction_fees = max(position_size * transaction_fee_per_share * 2, 2.16)  # 买入和卖出费用，最低2.16
                    else:
                        transaction_fees = 0  # 关闭手续费
                    pnl = position_size * (entry_price - exit_price) - transaction_fees
                    
                    trades.append({
                        'entry_time': trade_entry_time,
                        'exit_time': exit_time,
                        'side': 'Short',
                        'entry_price': entry_price,
                        'exit_price': exit_price,
                        'pnl': pnl,
                        'exit_reason': exit_reason,
                        'position_size': position_size,
                        'transaction_fees': transaction_fees,
                        'vwap_influenced': vwap_influenced,  # 新增字段
                        'stop_level': per_trade_sl_level if per_trade_sl_exit else current_stop,
                        'lower_bound': lower_bound,
                        'vwap_value': vwap if use_vwap else np.nan,
                        'trailing_tp_activated': trailing_tp_activated,  # 🎯 动态止盈是否激活
                        'max_profit_price': max_profit_price if not np.isnan(max_profit_price) else np.nan,  # 🎯 最低价
                        'dynamic_tp_level': dynamic_take_profit_level if not np.isnan(dynamic_take_profit_level) else np.nan  # 🎯 动态止盈线
                    })
                    
                    # 🛡️ 检查日内止损
                    _after_trade_close_add_realized(pnl)
                    
                    position = 0
                    trailing_stop = np.nan
                    # 🎯 重置动态追踪止盈变量
                    max_profit_price = np.nan
                    trailing_tp_activated = False
                    dynamic_take_profit_level = np.nan
    
    # 获取交易结束时间字符串，格式为HH:MM
    end_time_str = f"{trading_end_time[0]:02d}:{trading_end_time[1]:02d}"
    
    # 寻找结束时间的数据点（如果存在）
    close_time_rows = day_df[day_df['Time'] == end_time_str]
    
    # 如果有结束时间的数据点且仍有未平仓位，则平仓
    if not close_time_rows.empty and position != 0:
        close_row = close_time_rows.iloc[0]
        exit_time = close_row['DateTime']
        close_price = close_row['Close']
        
        if position == 1:  # 多头仓位
            # 打印出场详情（如果需要）
            if print_details:
                date_str = exit_time.strftime('%Y-%m-%d')
                print(f"\n交易点位详情 [{date_str} {end_time_str}] - 多头收盘平仓:")
                print(f"  入场价: {entry_price:.2f}, 出场价: {close_price:.2f}, 股数: {position_size}")
            
            # 计算交易费用（开仓和平仓）
            if enable_transaction_fees:
                transaction_fees = max(position_size * transaction_fee_per_share * 2, 2.16)  # 买入和卖出费用，最低2.16
            else:
                transaction_fees = 0  # 关闭手续费
            pnl = position_size * (close_price - entry_price) - transaction_fees
            trades.append({
                'entry_time': trade_entry_time,
                'exit_time': exit_time,
                'side': 'Long',
                'entry_price': entry_price,
                'exit_price': close_price,
                'pnl': pnl,
                'exit_reason': 'Intraday Close',
                'position_size': position_size,
                'transaction_fees': transaction_fees,
                'vwap_influenced': False,  # 收盘平仓不受VWAP影响
                'stop_level': np.nan,
                'upper_bound': np.nan,
                'vwap_value': np.nan
            })
            
            # 🛡️ 检查日内止损
            _after_trade_close_add_realized(pnl)
            
            position = 0
            trailing_stop = np.nan
            # 🎯 重置动态追踪止盈变量
            max_profit_price = np.nan
            trailing_tp_activated = False
            dynamic_take_profit_level = np.nan
                
        else:  # 空头仓位
            # 打印出场详情（如果需要）
            if print_details:
                date_str = exit_time.strftime('%Y-%m-%d')
                print(f"\n交易点位详情 [{date_str} {end_time_str}] - 空头收盘平仓:")
                print(f"  入场价: {entry_price:.2f}, 出场价: {close_price:.2f}, 股数: {position_size}")
            
            # 计算交易费用（开仓和平仓）
            if enable_transaction_fees:
                transaction_fees = max(position_size * transaction_fee_per_share * 2, 2.16)  # 买入和卖出费用，最低2.16
            else:
                transaction_fees = 0  # 关闭手续费
            pnl = position_size * (entry_price - close_price) - transaction_fees
            trades.append({
                'entry_time': trade_entry_time,
                'exit_time': exit_time,
                'side': 'Short',
                'entry_price': entry_price,
                'exit_price': close_price,
                'pnl': pnl,
                'exit_reason': 'Intraday Close',
                'position_size': position_size,
                'transaction_fees': transaction_fees,
                'vwap_influenced': False,  # 收盘平仓不受VWAP影响
                'stop_level': np.nan,
                'lower_bound': np.nan,
                'vwap_value': np.nan
            })
            
            # 🛡️ 检查日内止损
            _after_trade_close_add_realized(pnl)
            
            position = 0
            trailing_stop = np.nan
            # 🎯 重置动态追踪止盈变量
            max_profit_price = np.nan
            trailing_tp_activated = False
            dynamic_take_profit_level = np.nan
    
    # 如果仍有未平仓位且没有结束时间数据点，则在一天结束时平仓
    elif position != 0:
        exit_time = day_df.iloc[-1]['DateTime']
        last_price = day_df.iloc[-1]['Close']
        last_time = day_df.iloc[-1]['Time']
        
        if position == 1:  # 多头仓位
            # 打印出场详情（如果需要）
            if print_details:
                date_str = exit_time.strftime('%Y-%m-%d')
                print(f"\n交易点位详情 [{date_str} {last_time}] - 多头市场收盘平仓:")
                print(f"  入场价: {entry_price:.2f}, 出场价: {last_price:.2f}, 股数: {position_size}")
            
            # 应用滑点
            exit_price = apply_slippage(last_price, is_buy=False, is_entry=False)  # 多头平仓是卖出
            # 计算交易费用（开仓和平仓）
            if enable_transaction_fees:
                transaction_fees = max(position_size * transaction_fee_per_share * 2, 2.16)  # 买入和卖出费用，最低2.16
            else:
                transaction_fees = 0  # 关闭手续费
            pnl = position_size * (exit_price - entry_price) - transaction_fees
            trades.append({
                'entry_time': trade_entry_time,
                'exit_time': exit_time,
                'side': 'Long',
                'entry_price': entry_price,
                'exit_price': exit_price,
                'pnl': pnl,
                'exit_reason': 'Market Close',
                'position_size': position_size,
                'transaction_fees': transaction_fees,
                'vwap_influenced': False,  # 市场收盘平仓不受VWAP影响
                'stop_level': np.nan,
                'upper_bound': np.nan,
                'vwap_value': np.nan
            })
            
            # 🛡️ 检查日内止损
            _after_trade_close_add_realized(pnl)
            # 🎯 重置动态追踪止盈变量
            max_profit_price = np.nan
            trailing_tp_activated = False
            dynamic_take_profit_level = np.nan
                
        else:  # 空头仓位
            # 打印出场详情（如果需要）
            if print_details:
                date_str = exit_time.strftime('%Y-%m-%d')
                print(f"\n交易点位详情 [{date_str} {last_time}] - 空头市场收盘平仓:")
                print(f"  入场价: {entry_price:.2f}, 出场价: {last_price:.2f}, 股数: {position_size}")
            
            # 应用滑点
            exit_price = apply_slippage(last_price, is_buy=True, is_entry=False)  # 空头平仓是买入
            # 计算交易费用（开仓和平仓）
            if enable_transaction_fees:
                transaction_fees = max(position_size * transaction_fee_per_share * 2, 2.16)  # 买入和卖出费用，最低2.16
            else:
                transaction_fees = 0  # 关闭手续费
            pnl = position_size * (entry_price - exit_price) - transaction_fees
            trades.append({
                'entry_time': trade_entry_time,
                'exit_time': exit_time,
                'side': 'Short',
                'entry_price': entry_price,
                'exit_price': exit_price,
                'pnl': pnl,
                'exit_reason': 'Market Close',
                'position_size': position_size,
                'transaction_fees': transaction_fees,
                'vwap_influenced': False,  # 市场收盘平仓不受VWAP影响
                'stop_level': np.nan,
                'lower_bound': np.nan,
                'vwap_value': np.nan
            })
            
            # 🛡️ 检查日内止损
            _after_trade_close_add_realized(pnl)
            # 🎯 重置动态追踪止盈变量
            max_profit_price = np.nan
            trailing_tp_activated = False
            dynamic_take_profit_level = np.nan
    
    # 计算日内最大回撤百分比（基于当日起始资金）
    intraday_max_drawdown_pct = intraday_max_drawdown / day_start_capital if day_start_capital > 0 else 0
    intraday_max_loss_from_start_pct = (
        max(0.0, day_start_capital - intraday_capital_low) / day_start_capital
        if day_start_capital > 0 else 0.0
    )

    # 返回交易、日内峰谷回撤、日初最大亏损、日内最低/最高资金
    return (
        trades,
        intraday_max_drawdown_pct,
        intraday_max_loss_from_start_pct,
        intraday_capital_low,
        intraday_capital_high,
    )

def run_backtest(config):
    """
    运行回测 - 噪声空间策略 + VWAP
    
    参数:
        config: 配置字典，包含所有回测参数
        
    返回:
        日度结果DataFrame
        月度结果DataFrame
        交易记录DataFrame
        性能指标字典
    """
    # 从配置中提取参数
    data_path = config.get('data_path')
    ticker = config.get('ticker')
    initial_capital = config.get('initial_capital', 100000)
    lookback_days = config.get('lookback_days', 90)
    start_date = config.get('start_date')
    end_date = config.get('end_date')
    plot_days = config.get('plot_days')
    random_plots = config.get('random_plots', 0)
    plots_dir = config.get('plots_dir', 'trading_plots')
    check_interval_minutes = config.get('check_interval_minutes', 30)
    transaction_fee_per_share = config.get('transaction_fee_per_share', 0.01)
    enable_transaction_fees = config.get('enable_transaction_fees', True)  # 新增手续费开关
    trading_start_time = config.get('trading_start_time', (10, 00))
    trading_end_time = config.get('trading_end_time', (15, 40))
    max_positions_per_day = config.get('max_positions_per_day', float('inf'))
    print_daily_trades = config.get('print_daily_trades', True)
    print_trade_details = config.get('print_trade_details', False)
    debug_time = config.get('debug_time')
    leverage = config.get('leverage', 1)  # 资金杠杆倍数，默认为1
    
    # 如果未提供ticker，从文件名中提取
    if ticker is None:
        # 从文件名中提取ticker
        file_name = os.path.basename(data_path)
        # 移除_market_hours.csv（如果存在）
        ticker = file_name.replace('_market_hours.csv', '')
    
    # 加载和处理数据
    price_df = pd.read_csv(data_path, parse_dates=['DateTime'])
    price_df.sort_values('DateTime', inplace=True)
    
    # 提取日期和时间组件
    price_df['Date'] = price_df['DateTime'].dt.date
    price_df['Time'] = price_df['DateTime'].dt.strftime('%H:%M')

    # 用全样本日收盘计算日频趋势特征（不修改 CSV）；回测窗口截断后再按 Date 合并
    trend_feat_df = compute_daily_trend_features(price_df)
    
    # 按日期范围过滤数据（如果指定）
    if start_date is not None:
        price_df = price_df[price_df['Date'] >= start_date]
    
    if end_date is not None:
        price_df = price_df[price_df['Date'] <= end_date]

    price_df = pd.merge(price_df, trend_feat_df, on='Date', how='left')
    # entry_trend_pass 延后到 sigma / 日内特征算完后再写（支持 sigma、minutes_from_open 等门控）
    
    print(f"加载{ticker}数据: {data_path} ({start_date} ~ {end_date})")
    
    # 检查DayOpen和DayClose列是否存在，如果不存在则创建
    if 'DayOpen' not in price_df.columns or 'DayClose' not in price_df.columns:
        # 对于每一天，获取第一行（9:30 AM开盘价）
        opening_prices = price_df.groupby('Date').first().reset_index()
        opening_prices = opening_prices[['Date', 'Open']].rename(columns={'Open': 'DayOpen'})

        # 对于每一天，获取最后一行（4:00 PM收盘价）
        closing_prices = price_df.groupby('Date').last().reset_index()
        closing_prices = closing_prices[['Date', 'Close']].rename(columns={'Close': 'DayClose'})

        # 将开盘价和收盘价合并回主DataFrame
        price_df = pd.merge(price_df, opening_prices, on='Date', how='left')
        price_df = pd.merge(price_df, closing_prices, on='Date', how='left')
    
    # 使用筛选后数据的DayOpen和DayClose
    # 这些代表9:30 AM开盘价和4:00 PM收盘价
    price_df['prev_close'] = price_df.groupby('Date')['DayClose'].transform('first').shift(1)
    
    # 使用9:30 AM价格作为当天的开盘价
    price_df['day_open'] = price_df.groupby('Date')['DayOpen'].transform('first')
    
    # 为每个交易日计算一次参考价格，并将其应用于该日的所有时间点
    # 这确保了整个交易日使用相同的参考价格
    unique_dates = price_df['Date'].unique()
    
    # 创建临时DataFrame来存储每个日期的参考价格
    date_refs = []
    for d in unique_dates:
        day_data = price_df[price_df['Date'] == d].iloc[0]  # 获取该日第一行数据
        day_open = day_data['day_open']
        prev_close = day_data['prev_close']
        
        # 计算该日的参考价格
        if not pd.isna(prev_close):
            upper_ref = max(day_open, prev_close)
            lower_ref = min(day_open, prev_close)
        else:
            upper_ref = day_open
            lower_ref = day_open
            
        date_refs.append({
            'Date': d,
            'upper_ref': upper_ref,
            'lower_ref': lower_ref
        })
    
    # 创建日期参考价格DataFrame
    date_refs_df = pd.DataFrame(date_refs)
    
    # 检查是否有足够的数据进行处理
    if date_refs_df.empty or len(date_refs) == 0:
        raise ValueError(f"在指定日期范围内没有找到有效的交易数据。请检查日期范围设置。")
    
    # 将参考价格合并回主DataFrame
    price_df = price_df.drop(columns=['upper_ref', 'lower_ref'], errors='ignore')
    
    # 检查Date列是否存在
    if 'Date' not in price_df.columns:
        raise ValueError("price_df中缺少Date列，无法进行合并操作")
    if 'Date' not in date_refs_df.columns:
        raise ValueError("date_refs_df中缺少Date列，无法进行合并操作")
    
    price_df = pd.merge(price_df, date_refs_df, on='Date', how='left')
    
    # 计算每分钟相对开盘的回报（使用day_open保持一致性）
    price_df['ret'] = price_df['Close'] / price_df['day_open'] - 1 

    # 计算噪声区域边界
    # 将时间点转为列
    pivot = price_df.pivot(index='Date', columns='Time', values='ret').abs()
    
    # 重要修复：确保rolling基于实际交易日而不是日历日
    # 对于周一或节假日后的第一个交易日，应该使用前一个交易日的数据
    # 这里使用实际存在的交易日进行rolling计算
    sigma = pivot.rolling(window=lookback_days, min_periods=lookback_days).mean().shift(1)
    
    # 对于lookback_days=1的情况，特殊处理：直接使用前一个交易日的数据
    if lookback_days == 1:
        # shift(1)已经确保使用前一个交易日的数据
        # 因为pivot的索引只包含实际的交易日，所以shift(1)会自动跳过周末
        pass
    
    # 转回长格式
    sigma = sigma.stack().reset_index(name='sigma')
    
    
    # 保存一个原始数据的副本，用于计算买入持有策略
    price_df_original = price_df.copy()
    
    # 将sigma合并回主DataFrame
    price_df = pd.merge(price_df, sigma, on=['Date', 'Time'], how='left')
    
    # 检查每个交易日是否有足够的sigma数据
    # 创建一个标记，记录哪些日期的sigma数据严重不完整（缺失超过10%）
    incomplete_sigma_dates = set()
    for date in price_df['Date'].unique():
        day_data = price_df[price_df['Date'] == date]
        na_count = day_data['sigma'].isna().sum()
        total_count = len(day_data)
        missing_ratio = na_count / total_count if total_count > 0 else 1.0
        
        # 只有当缺失率超过10%时才过滤掉这一天
        if missing_ratio > 0.1:
            incomplete_sigma_dates.add(date)
            incomplete_sigma_dates.add(date)
            # print(f"{date} sigma缺失率过高: {na_count}/{total_count} ({missing_ratio*100:.1f}%) - 将被过滤")
        # elif na_count > 0:
            # 少量缺失值，填充为前值
            # print(f"{date} 有少量sigma缺失: {na_count}/{total_count} ({missing_ratio*100:.1f}%) - 将被保留")
    
    # 移除sigma数据严重不完整的日期
    # if incomplete_sigma_dates:
    #     print(f"sigma严重不完整的日期（将被过滤）: {sorted(incomplete_sigma_dates)}")
    price_df = price_df[~price_df['Date'].isin(incomplete_sigma_dates)]
    
    # 对于剩余的少量缺失值，使用前值填充（forward fill）
    price_df['sigma'] = price_df.groupby('Date')['sigma'].ffill()
    # 如果还有缺失（比如第一个值），使用后值填充
    price_df['sigma'] = price_df.groupby('Date')['sigma'].bfill()
    # 如果整个时间点都缺失，使用0填充（保守策略）
    price_df['sigma'] = price_df['sigma'].fillna(0)
    
    # 确保所有剩余的sigma值都有有效数据
    if price_df['sigma'].isna().any():
        print(f"警告: 仍有{price_df['sigma'].isna().sum()}个缺失的sigma值")
    
    
    # 日内特征（供 k_side_adjustment 动态 K 使用）
    price_df['intraday_ret'] = price_df['Close'] / price_df['day_open'] - 1
    day_start_dt = price_df.groupby('Date')['DateTime'].transform('min')
    price_df['minutes_from_open'] = (price_df['DateTime'] - day_start_dt).dt.total_seconds() / 60
    price_df['cum_high'] = price_df.groupby('Date')['High'].cummax()
    price_df['cum_low'] = price_df.groupby('Date')['Low'].cummin()
    intraday_span = price_df['cum_high'] - price_df['cum_low']
    price_df['intraday_range_pos'] = (
        (price_df['Close'] - price_df['cum_low']) / intraday_span.replace(0, np.nan)
    ).clip(0, 1)
    day_sigma_med = price_df.groupby('Date')['sigma'].transform('median')
    price_df['sigma_vs_day_median'] = price_df['sigma'] / day_sigma_med.replace(0, np.nan)

    # 噪声区域上下边界（支持 k_side_adjustment 动态 K1/K2）
    k1_base = config.get('K1', 1)
    k2_base = config.get('K2', 1)
    price_df = apply_k_bounds(price_df, config)

    # 开仓门控：放在 sigma 与边界之后，才能使用 sigma / minutes_from_open 等列
    price_df['entry_trend_pass'] = compute_entry_trend_pass_series(price_df, config)
    
    # 根据检查间隔生成允许的交易时间
    allowed_times = []
    start_hour, start_minute = trading_start_time  # 使用可配置的开始时间
    end_hour, end_minute = trading_end_time        # 使用可配置的结束时间
    
    current_hour, current_minute = start_hour, start_minute
    while current_hour < end_hour or (current_hour == end_hour and current_minute <= end_minute):
        # 将当前时间添加到allowed_times
        allowed_times.append(f"{current_hour:02d}:{current_minute:02d}")
        
        # 增加check_interval_minutes
        current_minute += check_interval_minutes
        if current_minute >= 60:
            current_hour += current_minute // 60
            current_minute = current_minute % 60
    
    # 始终确保trading_end_time包含在内，用于平仓
    end_time_str = f"{trading_end_time[0]:02d}:{trading_end_time[1]:02d}"
    if end_time_str not in allowed_times:
        allowed_times.append(end_time_str)
        allowed_times.sort()
    
    use_vwap = config.get('use_vwap', False)
    enable_ttp = config.get('enable_trailing_take_profit', False)
    ttp_act = config.get('trailing_tp_activation_pct', 0.005)
    ttp_cb = config.get('trailing_tp_callback_pct', 0.5)
    entry_filter = config.get('entry_trend_filter')
    slip = config.get('slippage_per_share', 0)
    if config.get('enable_k_side_adjustment', False) and config.get('k_side_adjustment'):
        k1_rng = (price_df['K1_eff'].min(), price_df['K1_eff'].max())
        k2_rng = (price_df['K2_eff'].min(), price_df['K2_eff'].max())
        k_info = f"K1={k1_base}∈[{k1_rng[0]:.2f},{k1_rng[1]:.2f}], K2={k2_base}∈[{k2_rng[0]:.2f},{k2_rng[1]:.2f}]"
    else:
        k_info = f"K1={k1_base}, K2={k2_base}"
    ttp_info = f"TTP={ttp_act*100:.2f}%/{ttp_cb*100:.0f}%" if enable_ttp else "TTP=off"
    print(
        f"参数: interval={check_interval_minutes}m, {k_info}, leverage={leverage}x, "
        f"VWAP={use_vwap}, {ttp_info}, filter={entry_filter}, slip={slip}"
    )
    
    # 初始化回测变量
    capital = initial_capital
    daily_results = []
    all_trades = []
    total_transaction_fees = 0  # 跟踪总交易费用
    
    # 添加交易日期统计变量
    trading_days = set()       # 有交易的日期集合
    non_trading_days = set()   # 无交易的日期集合
    
    # 追踪最大日内回撤
    max_intraday_mdd_pct = 0   # 最大日内峰谷回撤（相对日初）
    max_intraday_mdd_date = None  # 最大日内峰谷回撤发生的日期
    max_intraday_loss_from_start_pct = 0  # 最大日内亏损（相对日初，FTMO 日损口径）
    max_intraday_loss_from_start_date = None
    # 每日回撤记录（用于报告列出最差若干天）
    daily_dd_records = []

    # 📊 精确最大回撤追踪（考虑日内波动）
    capital_peak = initial_capital  # 资金峰值（包含日内高点）
    precise_max_drawdown = 0  # 精确最大回撤金额
    precise_max_drawdown_pct = 0  # 精确最大回撤百分比
    precise_mdd_date = None  # 精确最大回撤发生日期（谷底日）
    current_peak_date = None  # 当前历史资金峰值日期
    precise_mdd_peak_date = None  # 最大回撤对应的峰值日期
    precise_mdd_peak_capital = None  # 触发该次最大回撤时的峰值权益
    # 如果指定了随机生成图表的数量，随机选择交易日
    days_with_trades = []
    if random_plots > 0:
        # 先运行回测，记录有交易的日期
        for trade_date in unique_dates:
            day_data = price_df[price_df['Date'] == trade_date].copy()
            # 设置数据点阈值：对于今天允许更少的数据点
            is_today = (day_data['Date'].iloc[0] == datetime.now().date()) if len(day_data) > 0 else False
            min_data_points = 1 if is_today else 10
            if len(day_data) < min_data_points:  # 跳过数据不足的日期
                continue
                
            prev_close = day_data['prev_close'].iloc[0] if not pd.isna(day_data['prev_close'].iloc[0]) else None
            if prev_close is None:
                continue
                
            # 模拟当天交易
            simulation_result = simulate_day(day_data, prev_close, allowed_times, 100, config, config.get('initial_capital', 100000))
            
            # 从结果中提取交易（忽略其他返回值）
            trades = simulation_result[0]
                
            if trades:  # 如果有交易
                days_with_trades.append(trade_date)
        
        # 如果有交易的日期少于请求的随机图表数量，调整随机图表数量
        random_plots = min(random_plots, len(days_with_trades))
        # 随机选择日期
        if random_plots > 0:
            random_plot_days = random.sample(days_with_trades, random_plots)
        else:
            random_plot_days = []
    else:
        random_plot_days = []
    
    # 合并指定的绘图日期和随机选择的日期
    if plot_days is None:
        plot_days = []
    all_plot_days = list(set(plot_days + random_plot_days))
    
    # 确保绘图目录存在
    if all_plot_days and plots_dir:
        os.makedirs(plots_dir, exist_ok=True)
    
    # 创建买入持有回测数据（使用原始数据，不受sigma筛选影响）
    buy_hold_data = []
    filtered_dates = price_df['Date'].unique()  # 策略交易使用的日期（经过sigma筛选）
    
    # 创建独立的买入持有数据，使用原始数据（未经过sigma筛选）
    for trade_date in unique_dates:
        # 获取当天的数据（从原始数据中）
        day_data = price_df_original[price_df_original['Date'] == trade_date].copy()

        # 跳过数据不足的日期
        is_today = (day_data['Date'].iloc[0] == datetime.now().date()) if len(day_data) > 0 else False
        min_data_points = 1 if is_today else 10
        if len(day_data) < min_data_points:  # 任意阈值
            continue
        
        # 获取当天的开盘价和收盘价（用于计算买入持有）
        open_price = day_data['day_open'].iloc[0]
        close_price = day_data['DayClose'].iloc[0]
        
        # 存储买入持有数据
        buy_hold_data.append({
            'Date': trade_date,
            'Open': open_price,
            'Close': close_price
        })
    
    # 处理策略交易部分
    
    for i, trade_date in enumerate(filtered_dates):
        # 获取当天的数据
        day_data = price_df[price_df['Date'] == trade_date].copy()
        day_data = day_data.sort_values('DateTime').reset_index(drop=True)
        
        
        # 跳过数据不足的日期
        is_today = (day_data['Date'].iloc[0] == datetime.now().date()) if len(day_data) > 0 else False
        min_data_points = 1 if is_today else 10
        if len(day_data) < min_data_points:  # 任意阈值
            if not is_today:
                daily_results.append({
                    'Date': trade_date,
                    'capital': capital,
                    'daily_return': 0
                })
                continue
        
        # 获取前一天的收盘价
        prev_close = day_data['prev_close'].iloc[0] if not pd.isna(day_data['prev_close'].iloc[0]) else None
        
        # 将trade_date转换为字符串格式以便统一显示
        date_str = pd.to_datetime(trade_date).strftime('%Y-%m-%d')
        
        # 获取当天的开盘价
        day_open_price = day_data['day_open'].iloc[0]
        
        # 计算仓位大小（应用杠杆）
        leveraged_capital = capital * leverage  # 应用杠杆倍数
        position_size = floor(leveraged_capital / day_open_price)
        
        # 如果资金不足，跳过当天
        if position_size <= 0:
            daily_results.append({
                'Date': trade_date,
                'capital': capital,
                'daily_return': 0
            })
            continue
                
        # 模拟当天的交易
        simulation_result = simulate_day(day_data, prev_close, allowed_times, position_size, config, capital)
        
        # 从结果中提取交易、日内回撤、日内最低/最高资金
        trades, intraday_mdd_pct, intraday_loss_from_start_pct, intraday_low, intraday_high = simulation_result
        
        # 更新交易日期统计
        if trades:  # 有交易的日期
            trading_days.add(trade_date)
        else:  # 无交易的日期
            non_trading_days.add(trade_date)
        
        # 追踪最大日内峰谷回撤
        if intraday_mdd_pct > max_intraday_mdd_pct:
            max_intraday_mdd_pct = intraday_mdd_pct
            max_intraday_mdd_date = trade_date

        # 追踪最大日内亏损（相对日初）
        if intraday_loss_from_start_pct > max_intraday_loss_from_start_pct:
            max_intraday_loss_from_start_pct = intraday_loss_from_start_pct
            max_intraday_loss_from_start_date = trade_date
        
        # 📊 精确最大回撤计算（考虑日内波动）
        if current_peak_date is None:
            current_peak_date = trade_date

        # 更新资金峰值（使用日内最高点）
        if intraday_high > capital_peak:
            capital_peak = intraday_high
            current_peak_date = trade_date
        
        # 计算当前回撤（使用日内最低点与历史峰值的差距）
        current_precise_drawdown = capital_peak - intraday_low
        current_precise_drawdown_pct = current_precise_drawdown / capital_peak if capital_peak > 0 else 0
        
        if current_precise_drawdown_pct > precise_max_drawdown_pct:
            precise_max_drawdown = current_precise_drawdown
            precise_max_drawdown_pct = current_precise_drawdown_pct
            precise_mdd_date = trade_date
            precise_mdd_peak_date = current_peak_date
            precise_mdd_peak_capital = capital_peak

        daily_dd_records.append({
            'date': trade_date,
            'precise_dd_pct': current_precise_drawdown_pct,
            'intraday_mdd_pct': intraday_mdd_pct,
            'loss_from_start_pct': intraday_loss_from_start_pct,
        })
        
        # 打印每天的交易信息
        if trades and print_daily_trades:
            # 计算当天总盈亏
            day_total_pnl = sum(trade['pnl'] for trade in trades)
            
            # 创建交易方向与时间的简要信息
            trade_summary = []
            for trade in trades:
                direction = "多" if trade['side'] == 'Long' else "空"
                entry_time = trade['entry_time'].strftime('%H:%M')
                exit_time = trade['exit_time'].strftime('%H:%M')
                pnl = trade['pnl']
                entry_price = trade['entry_price']
                exit_price = trade['exit_price']
                size = trade.get('position_size', position_size)
                trade_summary.append(f"{direction}({entry_time}->{exit_time}) 买:{entry_price:.2f} 卖:{exit_price:.2f} 股数:{size} 盈亏:${pnl:.2f}")
            
            # 打印单行交易日志
            trade_info = ", ".join(trade_summary)
            leverage_info = f" [杠杆{leverage}x]" if leverage != 1 else ""
            print(f"{date_str} | 交易数: {len(trades)} | 总盈亏: ${day_total_pnl:.2f} | 日内回撤: {intraday_mdd_pct*100:.2f}%{leverage_info} | {trade_info}")
        
        # 检查是否需要为这一天生成图表
        if trade_date in all_plot_days:
            # 为当天的交易生成图表
            plot_path = os.path.join(plots_dir, f"{ticker}_trade_visualization_{trade_date}")
            
            # 添加交易类型到文件名
            sides = [trade['side'] for trade in trades]
            if 'Long' in sides and 'Short' not in sides:
                plot_path += "_Long.png"
            elif 'Short' in sides and 'Long' not in sides:
                plot_path += "_Short.png"
            elif 'Long' in sides and 'Short' in sides:
                plot_path += "_Mixed.png"
            else:
                plot_path += ".png"  # 没有交易
                
            # 生成并保存图表
            plot_trading_day(day_data, trades, save_path=plot_path)
        
        # 计算每日盈亏和交易费用
        day_pnl = 0
        day_transaction_fees = 0
        for trade in trades:
            day_pnl += trade['pnl']
            # 从每笔交易中提取交易费用
            if 'transaction_fees' not in trade:
                # 如果交易数据中没有交易费用，则计算
                if enable_transaction_fees:
                    trade['transaction_fees'] = max(position_size * transaction_fee_per_share * 2, 2.16)  # 买入和卖出费用，最低2.16
                else:
                    trade['transaction_fees'] = 0  # 关闭手续费
            day_transaction_fees += trade['transaction_fees']
        
        # 添加到总交易费用
        total_transaction_fees += day_transaction_fees
        
        # 更新资金并计算每日回报
        capital_start = capital
        capital += day_pnl
        daily_return = day_pnl / capital_start

        # 存储每日结果
        daily_results.append({
            'Date': trade_date,
            'capital': capital,
            'daily_return': daily_return
        })
        
        # 存储交易
        for trade in trades:
            trade['Date'] = trade_date
            all_trades.append(trade)
    
    # 创建每日结果DataFrame
    daily_df = pd.DataFrame(daily_results)
    
    # 检查daily_results是否为空或缺少Date列
    if daily_df.empty:
        raise ValueError("daily_results为空，无法创建日度结果DataFrame")
    if 'Date' not in daily_df.columns:
        raise ValueError("daily_results中缺少Date列，无法进行日期转换")
    
    daily_df['Date'] = pd.to_datetime(daily_df['Date'])
    daily_df.set_index('Date', inplace=True)
    
    # 创建买入持有DataFrame
    buy_hold_df = pd.DataFrame(buy_hold_data)
    
    # 检查buy_hold_data是否为空
    if not buy_hold_data:
        print("警告: 没有足够的数据来计算买入持有策略的表现")
        buy_hold_df = pd.DataFrame()  # 创建一个空的DataFrame
    else:
        buy_hold_df['Date'] = pd.to_datetime(buy_hold_df['Date'])
        buy_hold_df.set_index('Date', inplace=True)
    
    # 计算买入持有策略的表现
    if not buy_hold_df.empty:
        # 计算每日收益率
        buy_hold_df['daily_return'] = buy_hold_df['Close'] / buy_hold_df['Close'].shift(1) - 1
        
        # 计算累积资本
        buy_hold_df['capital'] = initial_capital * (1 + buy_hold_df['daily_return']).cumprod().fillna(1)
    
    # 计算月度回报
    monthly = daily_df.resample('ME').first()[['capital']].rename(columns={'capital': 'month_start'})
    monthly['month_end'] = daily_df.resample('ME').last()['capital']
    monthly['monthly_return'] = monthly['month_end'] / monthly['month_start'] - 1
    
    # 打印月度回报
    print("\n月度回报:")
    pd.set_option('display.max_rows', None)
    
    monthly_display = monthly[['month_start', 'month_end', 'monthly_return']].copy()
    monthly_display['monthly_return_pct'] = monthly_display['monthly_return'] * 100
    monthly_display = monthly_display.round({'month_start': 2, 'month_end': 2, 'monthly_return_pct': 2})
    
    print(monthly_display[['month_start', 'month_end', 'monthly_return_pct']].rename(columns={
        'month_start': '月初资金',
        'month_end': '月末资金', 
        'monthly_return_pct': '收益率(%)'
    }))
    
    monthly_returns = monthly['monthly_return'].dropna()
    if len(monthly_returns) > 0:
        print(f"  平均: {monthly_returns.mean()*100:.2f}% | 最佳: {monthly_returns.max()*100:.2f}% | 最差: {monthly_returns.min()*100:.2f}% | 胜率: {(monthly_returns > 0).mean()*100:.0f}%")
    
    pd.reset_option('display.max_rows')
    
    # 创建交易DataFrame
    trades_df = pd.DataFrame(all_trades)
    
    # 计算策略性能指标
    metrics = calculate_performance_metrics(daily_df, trades_df, initial_capital, buy_hold_df=buy_hold_df)

    # 策略「最大回撤」：用历史权益极大值（跨日滚动 peak）相对当日最低权益（含持仓时用 K 线高低估的日内极值），
    # 而非仅用日终收盘序列的 cummax——后者会低估盘中回撤，与 prop 要求的「相对历史高点回撤」不对应。
    metrics['mdd_eod_close_only'] = metrics['mdd']
    if precise_mdd_date is not None:
        metrics['mdd'] = precise_max_drawdown_pct
        metrics['max_drawdown_date'] = pd.Timestamp(precise_mdd_date)
        if precise_mdd_peak_date is not None:
            metrics['max_drawdown_start_date'] = pd.Timestamp(precise_mdd_peak_date)
        # 恢复日：谷底之后，日终权益重新站上触发该次 MDD 时的峰值（勿无条件置 None）
        peak_cap = precise_mdd_peak_capital
        if peak_cap is None and precise_max_drawdown_pct > 0:
            peak_cap = precise_max_drawdown / precise_max_drawdown_pct
        recovery_end = None
        if peak_cap is not None and len(daily_df) > 0:
            trough_ts = pd.Timestamp(precise_mdd_date)
            after = daily_df.loc[daily_df.index > trough_ts, 'capital']
            recovered = after[after >= peak_cap]
            if len(recovered) > 0:
                recovery_end = recovered.index.min()
        metrics['max_drawdown_end_date'] = recovery_end
    if metrics['mdd'] > 0:
        metrics['calmar_ratio'] = metrics['irr'] / metrics['mdd']
    else:
        metrics['calmar_ratio'] = float('inf')

    metrics['max_single_day_intraday_mdd_pct'] = max_intraday_mdd_pct
    metrics['max_single_day_intraday_mdd_date'] = max_intraday_mdd_date
    metrics['max_single_day_loss_from_start_pct'] = max_intraday_loss_from_start_pct
    metrics['max_single_day_loss_from_start_date'] = max_intraday_loss_from_start_date

    def _top_dd_days(key, n=3):
        ranked = sorted(daily_dd_records, key=lambda r: r[key], reverse=True)
        out = []
        for r in ranked:
            if r[key] <= 0:
                break
            out.append({'date': r['date'], 'pct': r[key]})
            if len(out) >= n:
                break
        return out

    metrics['top_precise_drawdown_days'] = _top_dd_days('precise_dd_pct')
    metrics['top_single_day_intraday_mdd_days'] = _top_dd_days('intraday_mdd_pct')
    metrics['top_single_day_loss_from_start_days'] = _top_dd_days('loss_from_start_pct')

    # 计算总滑点损耗
    slippage_per_share = config.get('slippage_per_share', 0.01)
    if len(trades_df) > 0:
        total_slippage_cost = (trades_df['position_size'] * slippage_per_share * 2).sum()
    else:
        total_slippage_cost = 0
    total_trading_cost = total_transaction_fees + total_slippage_cost
    
    # 策略名称
    leverage_text = f" (杠杆{leverage}x)" if leverage != 1 else ""
    strategy_name = f"{ticker} Curr.Band + VWAP{leverage_text}"
    
    # 打印策略总结
    print(f"\n{'='*50}")
    print(f"策略回测总结 - {strategy_name}")
    print(f"{'='*50}")
    
    # 资金信息
    final_capital = daily_df['capital'].iloc[-1]
    if leverage != 1:
        print(f"初始资金: ${initial_capital:,.0f} | 杠杆: {leverage}x | 最终资金: ${final_capital:,.2f}")
    else:
        print(f"初始资金: ${initial_capital:,.0f} | 最终资金: ${final_capital:,.2f}")
    
    # 性能指标对比表
    print(f"\n{'指标':<20} | {'策略':<15} | {f'{ticker} Buy & Hold':<15}")
    print("-" * 55)
    print(f"{'总回报率':<20} | {metrics['total_return']*100:>14.1f}% | {metrics['buy_hold_return']*100:>14.1f}%")
    print(f"{'年化收益率':<20} | {metrics['irr']*100:>14.1f}% | {metrics['buy_hold_irr']*100:>14.1f}%")
    print(f"{'波动率':<20} | {metrics['volatility']*100:>14.1f}% | {metrics['buy_hold_volatility']*100:>14.1f}%")
    print(f"{'夏普比率':<20} | {metrics['sharpe_ratio']:>14.2f} | {metrics['buy_hold_sharpe']:>14.2f}")
    print(f"{'最大回撤':<20} | {metrics['mdd']*100:>14.1f}% | {metrics['buy_hold_mdd']*100:>14.1f}%")
    # Calmar = 年化收益率 / |最大回撤|（与上方 mdd 一致，含精确日内回撤修正后）
    _calmar = metrics.get('calmar_ratio', 0.0)
    if metrics.get('buy_hold_mdd', 0) and metrics['buy_hold_mdd'] > 0:
        _bh_calmar = metrics['buy_hold_irr'] / metrics['buy_hold_mdd']
    else:
        _bh_calmar = float('inf') if metrics.get('buy_hold_irr', 0) > 0 else 0.0
    metrics['buy_hold_calmar'] = _bh_calmar
    _calmar_s = f"{_calmar:.2f}" if _calmar != float('inf') else "inf"
    _bh_calmar_s = f"{_bh_calmar:.2f}" if _bh_calmar != float('inf') else "inf"
    print(f"{'Calmar比率':<20} | {_calmar_s:>14} | {_bh_calmar_s:>14}")
    _mdd1d = max_intraday_mdd_pct * 100 if max_intraday_mdd_date is not None else None
    _loss1d = max_intraday_loss_from_start_pct * 100 if max_intraday_loss_from_start_date is not None else None
    if _loss1d is not None:
        _loss_d = pd.Timestamp(max_intraday_loss_from_start_date).strftime('%Y-%m-%d')
        print(f"{'单日最大亏损(日初)':<20} | {_loss1d:>14.2f}% | {_loss_d:>15}")
    else:
        print(f"{'单日最大亏损(日初)':<20} | {'-':>15} | {'-':>15}")
    if _mdd1d is not None:
        _mdd_d = pd.Timestamp(max_intraday_mdd_date).strftime('%Y-%m-%d')
        print(f"{'单日峰谷最大回撤':<20} | {_mdd1d:>14.2f}% | {_mdd_d:>15}")
    else:
        print(f"{'单日峰谷最大回撤':<20} | {'-':>15} | {'-':>15}")

    def _fmt_top_days(rows):
        parts = []
        for i, r in enumerate(rows, 1):
            d = pd.Timestamp(r['date']).strftime('%Y-%m-%d')
            parts.append(f"{i}) {d} {r['pct']*100:.2f}%")
        return " · ".join(parts)

    _top_precise = metrics.get('top_precise_drawdown_days') or []
    if _top_precise:
        print(f"  回撤最深三日(相对历史峰值): {_fmt_top_days(_top_precise)}")
    _top_mdd1d = metrics.get('top_single_day_intraday_mdd_days') or []
    if _top_mdd1d:
        print(f"  单日峰谷回撤 Top3: {_fmt_top_days(_top_mdd1d)}")
    _top_loss1d = metrics.get('top_single_day_loss_from_start_days') or []
    if _top_loss1d:
        print(f"  单日最大亏损(日初) Top3: {_fmt_top_days(_top_loss1d)}")

    # 最大回撤详情：峰值日 → 谷底日 →（若有）恢复日
    if 'max_drawdown_start_date' in metrics and 'max_drawdown_date' in metrics:
        start_date = metrics['max_drawdown_start_date'].strftime('%Y-%m-%d')
        bottom_date = metrics['max_drawdown_date'].strftime('%Y-%m-%d')
        if metrics.get('max_drawdown_end_date') is not None:
            end_date = metrics['max_drawdown_end_date'].strftime('%Y-%m-%d')
            duration = (metrics['max_drawdown_end_date'] - metrics['max_drawdown_start_date']).days
            print(f"  回撤: {start_date}(峰值) → {bottom_date}(谷底) → {end_date}(恢复) (峰值到恢复共{duration}天)")
        else:
            to_trough = (metrics['max_drawdown_date'] - metrics['max_drawdown_start_date']).days
            last_day = daily_df.index[-1]
            open_days = (last_day - metrics['max_drawdown_start_date']).days
            print(
                f"  回撤: {start_date}(峰值) → {bottom_date}(谷底, 距峰值{to_trough}天); "
                f"截至回测结束({last_day.strftime('%Y-%m-%d')})尚未回到该峰值 (已{open_days}天)"
            )
    
    # 交易统计
    print(f"\n交易统计:")
    long_trades = len(trades_df[trades_df['side'] == 'Long']) if len(trades_df) > 0 else 0
    short_trades = len(trades_df[trades_df['side'] == 'Short']) if len(trades_df) > 0 else 0
    total_days = len(trading_days) + len(non_trading_days)
    print(f"  总交易: {metrics['total_trades']}次 (多:{long_trades} 空:{short_trades}) | 胜率: {metrics['hit_ratio']*100:.1f}%")
    if metrics['total_trades'] > 0:
        print(f"  单笔最大亏损比例: {metrics['max_single_loss_pct']*100:.2f}%")
    print(f"  交易日: {total_days}天 (有交易:{len(trading_days)} 无交易:{len(non_trading_days)})")
    print(f"  交易成本: ${total_trading_cost:,.2f} (手续费:${total_transaction_fees:,.2f} 滑点:${total_slippage_cost:,.2f})")

    print(f"{'='*50}")

    # 可选：独立权益报告（纯 HTML/SVG，与主回测逻辑隔离）
    if config.get('show_equity_report', False):
        try:
            render_equity_report(
                daily_df,
                metrics,
                config,
                buy_hold_df=buy_hold_df if not buy_hold_df.empty else None,
                trades_df=trades_df if len(trades_df) > 0 else None,
                open_browser=config.get('equity_report_open_browser', True),
                output_path=config.get('equity_report_path'),
            )
        except Exception as e:
            print(f"警告: 权益报告生成失败: {e}")

    return daily_df, monthly, trades_df, metrics 

def calculate_performance_metrics(daily_df, trades_df, initial_capital, risk_free_rate=0.02, trading_days_per_year=252, buy_hold_df=None):
    """
    计算策略的性能指标
    
    参数:
        daily_df: 包含每日回测结果的DataFrame
        trades_df: 包含所有交易的DataFrame
        initial_capital: 初始资金
        risk_free_rate: 无风险利率，默认为2%
        trading_days_per_year: 一年的交易日数量，默认为252
        buy_hold_df: 买入持有策略的DataFrame
        
    返回:
        包含各种性能指标的字典
    """
    metrics = {}
    
    # 确保daily_df有数据
    if len(daily_df) == 0:
        print("警告: 没有足够的数据来计算性能指标")
        # 返回默认值
        return {
            'total_return': 0, 'irr': 0, 'volatility': 0, 'sharpe_ratio': 0,
            'hit_ratio': 0, 'mdd': 0, 'buy_hold_return': 0, 'buy_hold_irr': 0,
            'buy_hold_volatility': 0, 'buy_hold_sharpe': 0, 'buy_hold_mdd': 0
        }
    
    # 1. 总回报率 (Total Return)
    final_capital = daily_df['capital'].iloc[-1]
    metrics['total_return'] = final_capital / initial_capital - 1
    
    # 2. 年化收益率 (IRR - Internal Rate of Return)
    # 获取回测的开始和结束日期
    start_date = daily_df.index[0]
    end_date = daily_df.index[-1]
    # 计算实际年数（考虑实际日历日而不仅仅是交易日）
    years = (end_date - start_date).days / 365.25
    # 如果时间跨度太短，使用交易日计算
    if years < 0.1:  # 少于约36天
        trading_days = len(daily_df)
        years = trading_days / trading_days_per_year
    
    # 计算年化收益率 (CAGR - Compound Annual Growth Rate)
    if years > 0:
        metrics['irr'] = (1 + metrics['total_return']) ** (1 / years) - 1
    else:
        metrics['irr'] = 0
    
    # 3. 波动率 (Vol - Volatility)
    # 计算日收益率的标准差，然后年化
    daily_returns = daily_df['daily_return']
    # 移除异常值（如果有）
    daily_returns = daily_returns[daily_returns.between(daily_returns.quantile(0.001), 
                                                      daily_returns.quantile(0.999))]
    metrics['volatility'] = daily_returns.std() * np.sqrt(trading_days_per_year)
    
    # 4. 夏普比率 (Sharpe Ratio)
    if metrics['volatility'] > 0:
        metrics['sharpe_ratio'] = (metrics['irr'] - risk_free_rate) / metrics['volatility']
    else:
        metrics['sharpe_ratio'] = 0
    
    # 5. 胜率 (Hit Ratio)和交易统计
    if len(trades_df) > 0:
        winning_trades = trades_df[trades_df['pnl'] > 0]
        metrics['hit_ratio'] = len(winning_trades) / len(trades_df)
        
        # 计算平均盈利和平均亏损
        avg_win = winning_trades['pnl'].mean() if len(winning_trades) > 0 else 0
        losing_trades = trades_df[trades_df['pnl'] <= 0]
        avg_loss = losing_trades['pnl'].mean() if len(losing_trades) > 0 else 0
        
        # 计算盈亏比
        metrics['profit_loss_ratio'] = abs(avg_win / avg_loss) if avg_loss != 0 else float('inf')
        
        # 总交易次数
        metrics['total_trades'] = len(trades_df)
        
        # 计算每日交易次数
        daily_trade_counts = trades_df.groupby('Date').size()
        metrics['avg_daily_trades'] = daily_trade_counts.mean() if len(daily_trade_counts) > 0 else 0
        metrics['max_daily_trades'] = daily_trade_counts.max() if len(daily_trade_counts) > 0 else 0
        
        # 计算每日盈亏
        daily_pnl = trades_df.groupby('Date')['pnl'].sum()
        metrics['max_daily_loss'] = daily_pnl.min() if len(daily_pnl) > 0 and daily_pnl.min() < 0 else 0
        metrics['max_daily_gain'] = daily_pnl.max() if len(daily_pnl) > 0 else 0
        
        # 计算最大单笔收益和最大单笔亏损
        # 按盈亏排序，获取前10笔最大收益
        top_gains = trades_df.nlargest(10, 'pnl')[['Date', 'side', 'entry_price', 'exit_price', 'pnl', 'exit_reason']]
        metrics['top_10_gains'] = top_gains.to_dict('records')
        
        # 获取前10笔最大亏损
        top_losses = trades_df.nsmallest(10, 'pnl')[['Date', 'side', 'entry_price', 'exit_price', 'pnl', 'exit_reason']]
        metrics['top_10_losses'] = top_losses.to_dict('records')
        
        # 最大单笔收益和亏损
        metrics['max_single_gain'] = trades_df['pnl'].max()
        metrics['max_single_loss'] = trades_df['pnl'].min()

        # 单笔最大亏损比例（相对开仓价逆向价差；仅统计亏损单）
        adverse_move_pct = np.where(
            trades_df['side'] == 'Long',
            (trades_df['entry_price'] - trades_df['exit_price']) / trades_df['entry_price'],
            (trades_df['exit_price'] - trades_df['entry_price']) / trades_df['entry_price'],
        )
        losing_mask = trades_df['pnl'] < 0
        metrics['max_single_loss_pct'] = (
            float(np.max(adverse_move_pct[losing_mask])) if losing_mask.any() else 0.0
        )
    else:
        metrics['hit_ratio'] = 0
        metrics['profit_loss_ratio'] = 0
        metrics['total_trades'] = 0
        metrics['avg_daily_trades'] = 0
        metrics['max_daily_trades'] = 0
        metrics['max_daily_loss'] = 0
        metrics['max_daily_gain'] = 0
        metrics['top_10_gains'] = []
        metrics['top_10_losses'] = []
        metrics['max_single_gain'] = 0
        metrics['max_single_loss'] = 0
        metrics['max_single_loss_pct'] = 0.0
    
    # 6. 最大回撤 (MDD - Maximum Drawdown)
    # 此处为「仅日终收盘权益」路径：peak = cummax(日终 capital)，用于 Buy&Hold 与备用。
    # 策略侧在 run_backtest 中会优先替换为「历史权益峰值 → 含日内最低权益」的峰值回撤（与 prop 常见口径一致）。
    daily_df['peak'] = daily_df['capital'].cummax()
    daily_df['drawdown'] = (daily_df['capital'] - daily_df['peak']) / daily_df['peak']
    metrics['mdd'] = daily_df['drawdown'].min() * -1
    
    # 找到最大回撤发生的日期
    max_drawdown_date = daily_df['drawdown'].idxmin()
    metrics['max_drawdown_date'] = max_drawdown_date
    
    # 找到最大回撤开始的日期（即达到峰值的日期）
    max_drawdown_peak = daily_df.loc[max_drawdown_date, 'peak']
    # 找到达到这个峰值的最后一个日期
    peak_dates = daily_df[daily_df['capital'] == max_drawdown_peak].index
    max_drawdown_start_date = peak_dates[peak_dates <= max_drawdown_date].max()
    metrics['max_drawdown_start_date'] = max_drawdown_start_date
    
    # 找到最大回撤结束的日期（资金重新达到峰值的日期）
    recovery_dates = daily_df[daily_df['capital'] >= max_drawdown_peak].index
    recovery_dates_after = recovery_dates[recovery_dates > max_drawdown_date]
    if len(recovery_dates_after) > 0:
        max_drawdown_end_date = recovery_dates_after.min()
        metrics['max_drawdown_end_date'] = max_drawdown_end_date
    else:
        metrics['max_drawdown_end_date'] = None  # 尚未恢复
    
    # 计算回撤持续时间
    # 找到每个回撤开始的点
    drawdown_begins = (daily_df['peak'] != daily_df['peak'].shift(1)) & (daily_df['peak'] != daily_df['capital'])
    # 找到每个回撤结束的点（资金达到新高）
    drawdown_ends = daily_df['capital'] == daily_df['peak']
    
    # 计算最长回撤持续时间（交易日）
    if drawdown_begins.any() and drawdown_ends.any():
        begin_dates = daily_df.index[drawdown_begins]
        end_dates = daily_df.index[drawdown_ends]
        
        max_duration = 0
        for begin_date in begin_dates:
            # 找到这个回撤之后的第一个结束点
            end_date = end_dates[end_dates > begin_date]
            if len(end_date) > 0:
                duration = (end_date.min() - begin_date).days
                max_duration = max(max_duration, duration)
        
        metrics['max_drawdown_duration'] = max_duration
    else:
        metrics['max_drawdown_duration'] = 0
    
    # 计算Calmar比率 (年化收益率/最大回撤)
    if metrics['mdd'] > 0:
        metrics['calmar_ratio'] = metrics['irr'] / metrics['mdd']
    else:
        metrics['calmar_ratio'] = float('inf')  # 如果没有回撤，设为无穷大
        
    # 计算曝光时间 (Exposure Time)
    if len(trades_df) > 0:
        # 计算每笔交易的持仓时间（以分钟为单位）
        trades_df['duration'] = (trades_df['exit_time'] - trades_df['entry_time']).dt.total_seconds() / 60
        
        # 计算总交易时间（分钟）
        total_trade_minutes = trades_df['duration'].sum()
        
        # 计算回测总时间（分钟）
        # 假设每个交易日有6.5小时（390分钟）
        trading_minutes_per_day = 390
        total_backtest_minutes = len(daily_df) * trading_minutes_per_day
        
        # 计算曝光时间百分比
        metrics['exposure_time'] = total_trade_minutes / total_backtest_minutes
    else:
        metrics['exposure_time'] = 0
    
    # 计算买入持有策略的表现
    if buy_hold_df is not None and not buy_hold_df.empty:
        # 计算买入持有策略的总回报率
        if 'capital' in buy_hold_df.columns:
            final_buy_hold_capital = buy_hold_df['capital'].iloc[-1]
            metrics['buy_hold_return'] = final_buy_hold_capital / initial_capital - 1
            
            # 计算买入持有策略的年化收益率
            if years > 0:
                metrics['buy_hold_irr'] = (1 + metrics['buy_hold_return']) ** (1 / years) - 1
            else:
                metrics['buy_hold_irr'] = 0
            
            # 计算买入持有策略的波动率
            if 'daily_return' in buy_hold_df.columns:
                buy_hold_returns = buy_hold_df['daily_return'].dropna()
                # 移除异常值
                buy_hold_returns = buy_hold_returns[buy_hold_returns.between(
                    buy_hold_returns.quantile(0.001), buy_hold_returns.quantile(0.999))]
                metrics['buy_hold_volatility'] = buy_hold_returns.std() * np.sqrt(trading_days_per_year)
                
                # 计算买入持有策略的夏普比率
                if metrics['buy_hold_volatility'] > 0:
                    metrics['buy_hold_sharpe'] = (metrics['buy_hold_irr'] - risk_free_rate) / metrics['buy_hold_volatility']
                else:
                    metrics['buy_hold_sharpe'] = 0
            else:
                metrics['buy_hold_volatility'] = 0
                metrics['buy_hold_sharpe'] = 0
            
            # 计算买入持有策略的最大回撤
            if 'capital' in buy_hold_df.columns:
                buy_hold_df['peak'] = buy_hold_df['capital'].cummax()
                buy_hold_df['drawdown'] = (buy_hold_df['capital'] - buy_hold_df['peak']) / buy_hold_df['peak']
                metrics['buy_hold_mdd'] = buy_hold_df['drawdown'].min() * -1
            else:
                metrics['buy_hold_mdd'] = 0
        else:
            # 如果buy_hold_df中没有capital列，则计算起始日期和结束日期的价格变化
            if 'Close' in buy_hold_df.columns:
                start_price = buy_hold_df['Close'].iloc[0]
                end_price = buy_hold_df['Close'].iloc[-1]
                metrics['buy_hold_return'] = end_price / start_price - 1
                
                # 计算买入持有策略的年化收益率
                if years > 0:
                    metrics['buy_hold_irr'] = (1 + metrics['buy_hold_return']) ** (1 / years) - 1
                else:
                    metrics['buy_hold_irr'] = 0
                
                # 其他指标设为0
                metrics['buy_hold_volatility'] = 0
                metrics['buy_hold_sharpe'] = 0
                metrics['buy_hold_mdd'] = 0
            else:
                # 没有足够的数据计算买入持有策略的表现
                metrics['buy_hold_return'] = 0
                metrics['buy_hold_irr'] = 0
                metrics['buy_hold_volatility'] = 0
                metrics['buy_hold_sharpe'] = 0
                metrics['buy_hold_mdd'] = 0
    else:
        # 没有买入持有的数据，设置默认值
        metrics['buy_hold_return'] = 0
        metrics['buy_hold_irr'] = 0
        metrics['buy_hold_volatility'] = 0
        metrics['buy_hold_sharpe'] = 0
        metrics['buy_hold_mdd'] = 0
    
    return metrics 

def analyze_vwap_impact(trades_df):
    """
    分析VWAP对交易平仓的影响
    """
    if len(trades_df) == 0:
        print("\n=== VWAP影响分析 ===")
        print("没有交易数据可供分析")
        return
    
    # 只分析止损平仓的交易
    stop_loss_trades = trades_df[trades_df['exit_reason'] == 'Stop Loss']
    
    if len(stop_loss_trades) == 0:
        print("\n=== VWAP影响分析 ===")
        print("没有止损平仓的交易")
        return
    
    # 统计VWAP影响的交易
    vwap_influenced_trades = stop_loss_trades[stop_loss_trades['vwap_influenced'] == True]
    
    total_stop_loss = len(stop_loss_trades)
    vwap_influenced_count = len(vwap_influenced_trades)
    vwap_influence_ratio = vwap_influenced_count / total_stop_loss * 100
    
    print("\n=== VWAP影响分析 ===")
    print(f"总止损平仓交易数: {total_stop_loss}")
    print(f"VWAP影响的平仓数: {vwap_influenced_count}")
    print(f"VWAP生效比例: {vwap_influence_ratio:.1f}%")
    
    # 分多头和空头分析
    long_stop_loss = stop_loss_trades[stop_loss_trades['side'] == 'Long']
    short_stop_loss = stop_loss_trades[stop_loss_trades['side'] == 'Short']
    
    if len(long_stop_loss) > 0:
        long_vwap_influenced = long_stop_loss[long_stop_loss['vwap_influenced'] == True]
        long_ratio = len(long_vwap_influenced) / len(long_stop_loss) * 100
        print(f"\n多头交易:")
        print(f"  止损平仓数: {len(long_stop_loss)}")
        print(f"  VWAP影响数: {len(long_vwap_influenced)}")
        print(f"  VWAP生效比例: {long_ratio:.1f}%")
    
    if len(short_stop_loss) > 0:
        short_vwap_influenced = short_stop_loss[short_stop_loss['vwap_influenced'] == True]
        short_ratio = len(short_vwap_influenced) / len(short_stop_loss) * 100
        print(f"\n空头交易:")
        print(f"  止损平仓数: {len(short_stop_loss)}")
        print(f"  VWAP影响数: {len(short_vwap_influenced)}")
        print(f"  VWAP生效比例: {short_ratio:.1f}%")
    
    return {
        'total_stop_loss': total_stop_loss,
        'vwap_influenced_count': vwap_influenced_count,
        'vwap_influence_ratio': vwap_influence_ratio
    }

def analyze_trailing_take_profit_impact(trades_df, config):
    """
    🎯 分析动态追踪止盈对交易的影响
    """
    if len(trades_df) == 0:
        print("\n=== 动态追踪止盈分析 ===")
        print("没有交易数据可供分析")
        return None
    
    enable_trailing_tp = config.get('enable_trailing_take_profit', False)
    if not enable_trailing_tp:
        print("\n=== 动态追踪止盈分析 ===")
        print("动态追踪止盈未启用")
        return None
    
    # 统计追踪止盈触发的交易
    trailing_tp_trades = trades_df[trades_df['exit_reason'] == 'Trailing Take Profit']
    stop_loss_trades = trades_df[trades_df['exit_reason'] == 'Stop Loss']
    intraday_close_trades = trades_df[trades_df['exit_reason'] == 'Intraday Close']
    market_close_trades = trades_df[trades_df['exit_reason'] == 'Market Close']
    
    total_trades = len(trades_df)
    trailing_tp_count = len(trailing_tp_trades)
    trailing_tp_ratio = trailing_tp_count / total_trades * 100 if total_trades > 0 else 0
    
    print("\n=== 🎯 动态追踪止盈分析 ===")
    print(f"配置参数:")
    print(f"  激活阈值: {config.get('trailing_tp_activation_pct', 0.005)*100:.2f}%")
    print(f"  保护比例: {config.get('trailing_tp_callback_pct', 0.5)*100:.0f}%")
    print(f"\n出场方式统计:")
    print(f"  总交易数: {total_trades}")
    print(f"  追踪止盈: {trailing_tp_count} ({trailing_tp_ratio:.1f}%)")
    print(f"  止损平仓: {len(stop_loss_trades)} ({len(stop_loss_trades)/total_trades*100:.1f}%)")
    print(f"  日内收盘: {len(intraday_close_trades)} ({len(intraday_close_trades)/total_trades*100:.1f}%)")
    print(f"  市场收盘: {len(market_close_trades)} ({len(market_close_trades)/total_trades*100:.1f}%)")
    
    # 计算各类出场的盈亏
    if trailing_tp_count > 0:
        trailing_tp_pnl = trailing_tp_trades['pnl'].sum()
        trailing_tp_avg_pnl = trailing_tp_trades['pnl'].mean()
        trailing_tp_win_rate = (trailing_tp_trades['pnl'] > 0).mean() * 100
        print(f"\n追踪止盈交易详情:")
        print(f"  总盈亏: ${trailing_tp_pnl:.2f}")
        print(f"  平均盈亏: ${trailing_tp_avg_pnl:.2f}")
        print(f"  胜率: {trailing_tp_win_rate:.1f}%")
    
    if len(stop_loss_trades) > 0:
        stop_loss_pnl = stop_loss_trades['pnl'].sum()
        stop_loss_avg_pnl = stop_loss_trades['pnl'].mean()
        stop_loss_win_rate = (stop_loss_trades['pnl'] > 0).mean() * 100
        print(f"\n止损平仓交易详情:")
        print(f"  总盈亏: ${stop_loss_pnl:.2f}")
        print(f"  平均盈亏: ${stop_loss_avg_pnl:.2f}")
        print(f"  胜率: {stop_loss_win_rate:.1f}%")
    
    # 分多头和空头分析
    long_trailing_tp = trailing_tp_trades[trailing_tp_trades['side'] == 'Long']
    short_trailing_tp = trailing_tp_trades[trailing_tp_trades['side'] == 'Short']
    
    if len(long_trailing_tp) > 0:
        print(f"\n多头追踪止盈:")
        print(f"  次数: {len(long_trailing_tp)}")
        print(f"  总盈亏: ${long_trailing_tp['pnl'].sum():.2f}")
        print(f"  平均盈亏: ${long_trailing_tp['pnl'].mean():.2f}")
    
    if len(short_trailing_tp) > 0:
        print(f"\n空头追踪止盈:")
        print(f"  次数: {len(short_trailing_tp)}")
        print(f"  总盈亏: ${short_trailing_tp['pnl'].sum():.2f}")
        print(f"  平均盈亏: ${short_trailing_tp['pnl'].mean():.2f}")
    
    return {
        'total_trades': total_trades,
        'trailing_tp_count': trailing_tp_count,
        'trailing_tp_ratio': trailing_tp_ratio,
        'trailing_tp_pnl': trailing_tp_trades['pnl'].sum() if trailing_tp_count > 0 else 0,
        'stop_loss_count': len(stop_loss_trades),
        'stop_loss_pnl': stop_loss_trades['pnl'].sum() if len(stop_loss_trades) > 0 else 0
    }

def plot_specific_days(config, dates_to_plot):
    """
    为指定的日期生成交易图表
    
    参数:
        config: 配置字典，包含所有回测参数
        dates_to_plot: 要绘制的日期列表 (datetime.date 对象列表)
    """
    # 创建配置的副本并更新plot_days
    plot_config = config.copy()
    plot_config['plot_days'] = dates_to_plot
    
    # 运行回测，指定要绘制的日期
    _, _, _, _ = run_backtest(plot_config)
    
    print(f"\n已为以下日期生成图表:")
    for d in dates_to_plot:
        print(f"- {d}")

# 示例用法
if __name__ == "__main__":  
    # 创建配置字典
    config = {
        # 'data_path': 'qqq_market_hours_with_indicators.csv',
        'data_path': 'qqq_longport.csv',  # 使用包含Turnover字段的longport数据
        # 'data_path': 'qqq_longport_2year.csv',  # 使用包含Turnover字段的longport数据
        'ticker': 'QQQ',
        'initial_capital': 100000,
        'lookback_days':1,
        # 'start_date': date(2020, 4, 1),
        # 'end_date': date(2025, 4, 1),
        # 近一年回测窗口（数据含更早预热日，供趋势特征）
        'start_date': date(2025, 8, 5),
        'end_date': date(2026, 8, 5),
       
        'check_interval_minutes': 15 ,
        'enable_transaction_fees': True,  # 是否启用手续费计算，False表示不计算手续费
        'transaction_fee_per_share': 0.008166,
        # 'transaction_fee_per_share': 0,
        'slippage_per_share': 0.01,  # 滑点设置，每股滑点金额，买入时多付，卖出时少收
                                     # 例如：0.02表示买入每股多付2美分，卖出每股少收2美分
        'trading_start_time': (9, 40),
        'trading_end_time': (15, 40),
        'max_positions_per_day': 10,
        # 'random_plots': 3,
        # 'plots_dir': 'trading_plots',
        'print_daily_trades': False,
        'print_trade_details': False,
        # 按日权益报告：独立 HTML/SVG（非 matplotlib），默认关闭
        'show_equity_report': False,
        # 'equity_report_dir': 'reports',
        # 'equity_report_open_browser': True,
        # 'equity_report_path': 'reports/equity_report.html',
        'K1': 1,  # 上边界sigma乘数（多头）基准；午后动态见 k_side_adjustment
        'K2': 1.04,  # 下边界sigma乘数（空头），本规则不调整空头
        'enable_k_side_adjustment': True,  # 动态 K 总开关；False 时忽略下方 k_side_adjustment，退化为固定 K1/K2
        # 午后收紧多头 K：开盘满 120 分钟后 K1=0.9（更易触发多），此前 K1=1
        'k_side_adjustment': {
            'long': {
                'metric': 'minutes_from_open',
                'min': 120,
                'k_if_true': 0.9,
                'k_if_false': 1.0,
            },
        },
        'leverage':2,  # 资金杠杆倍数，与simulate一致
        'use_vwap': False,  # VWAP开关，True为使用VWAP，False为不使用
        'enable_intraday_stop_loss': True,  # 是否启用日内止损（与 simulate 的 MAX_DAILY_LOSS_AMOUNT>0 对应）
        'intraday_stop_loss_pct': 0.04,  # 仅在未指定 max_daily_loss_amount 时用于换算美元限额
        # both=日初亏损或峰谷回撤任一触限即强平（推荐）；from_day_start=仅日初；peak_to_trough=仅峰谷
        'intraday_stop_loss_mode': 'both',
        'enable_per_trade_stop_loss': False,  # 是否启用单笔止损（相对开仓价固定百分比）
        'per_trade_stop_loss_pct': 0.03,  # 单笔止损比例，如 0.03 表示开仓价逆向波动 3% 强平

        # 🎯 动态追踪止盈配置
        'enable_trailing_take_profit': True,  # 是否启用动态追踪止盈
        'trailing_tp_activation_pct': 0.006,  # 激活追踪止盈的最低浮盈百分比（1%），追踪的是qqq
        'trailing_tp_callback_pct': 0.65,  # 保护的利润比例（70%），即从最大浮盈回撤30%时触发止盈
        # 开仓趋势门控：None=关闭。单 dict 或 [dict,...]（AND）。特征见 compute_daily_trend_features。
        # er5 + 昨日振幅上限 + 时段sigma下限；2年/5年双窗口验证
        'entry_trend_filter': [
            {'metric': 'er5', 'min': 0.1},
            {'metric': 'range1', 'max': 0.029},
            {'metric': 'sigma', 'min': 0.0003},
        ],
        # 例：'entry_trend_filter': {'metric': 'weekly_sn', 'min': 0.65}
        # 'entry_trend_filter': {'metric': 'linreg5_r2', 'min': 0.46},
    }
    
    # 运行回测
    daily_results, monthly_results, trades, metrics = run_backtest(config)
