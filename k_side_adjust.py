"""
与 Quantra/backtest.py 午后收紧多头 K 一致：
开盘满 120 分钟后 K1=0.9，此前 K1=1.0（仅多头上边界，K2 不变）。
供各 simulate_*.py 在 calculate_noise_area 中调用；开关由各 simulate 的 ENABLE_K_SIDE_ADJUSTMENT 传入。
"""
SESSION_OPEN_TIME = "09:30"  # 与 backtest minutes_from_open（日首根 K）对齐
K_LONG_AFTER_MINUTES = 120
K1_AFTER_THRESHOLD = 0.9
K1_BEFORE_THRESHOLD = 1.0


def minutes_from_session_open(time_str: str, session_open: str = SESSION_OPEN_TIME) -> float:
    h, m = map(int, time_str.split(":"))
    oh, om = map(int, session_open.split(":"))
    return (h - oh) * 60 + (m - om)


def effective_k1_for_time(time_str: str, base_k1: float = 1.0, enabled: bool = False) -> float:
    """按时间点返回有效 K1；enabled=False 时退回 base_k1。"""
    if not enabled:
        return float(base_k1)
    if minutes_from_session_open(time_str) >= K_LONG_AFTER_MINUTES:
        return K1_AFTER_THRESHOLD
    return K1_BEFORE_THRESHOLD


def format_k_strategy_params(base_k1, k2, lookback_days, enabled: bool = False) -> str:
    if enabled:
        return (
            f"K1动态(午前{K1_BEFORE_THRESHOLD}/午后{K1_AFTER_THRESHOLD}, "
            f"开盘后{K_LONG_AFTER_MINUTES}min起), K2={k2}, 回看天数={lookback_days}"
        )
    return f"K1={base_k1}, K2={k2}, 回看天数={lookback_days}"
