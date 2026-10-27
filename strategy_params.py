"""
全部 simulate_*.py 共用的策略参数，与 Quantra/backtest.py 默认 config 一致（Quantra 505f1e4，版本 R + 开盘冷清半仓）。
改参数只改这里；账户相关参数（杠杆、资金、风控比例、交易时间）仍在各 simulate 脚本中。
上一版实盘参数见 git tag live-G-20260718（K2=1.04、午后 K1=0.9、range1<=0.029、无冷清半仓）。
"""

STRATEGY_VERSION = "2026-10-27 R+开盘冷清半仓"  # 启动日志打印，改参数时同步更新

# 噪声区域
LOOKBACK_DAYS = 1  # 回看天数（用于计算噪声区域）
K1 = 1  # 上边界sigma乘数（多头）
K2 = 1  # 下边界sigma乘数（空头）
ENABLE_K_SIDE_ADJUSTMENT = False  # 午后收紧多头 K（午前1.0/午后0.9）；False=全天固定 K1
USE_VWAP = False  # False=不使用VWAP作为入场/止损条件

# 动态追踪止盈（单笔浮盈回撤止盈，触发后当日不再开仓）
ENABLE_TRAILING_TAKE_PROFIT = True
TRAILING_TP_ACTIVATION_PCT = 0.006  # 激活追踪止盈的最低浮盈百分比（0.6%），追踪的是qqq
TRAILING_TP_CALLBACK_PCT = 0.65     # 保护的利润比例（65%），即从最大浮盈回撤35%时触发止盈

# 开仓门控（trend_er5_gate.py）
ENABLE_ENTRY_TREND_FILTER = True
ENTRY_TREND_ER5_MIN = 0.1         # 5 日 Kaufman 效率比下限
ENABLE_RANGE1_FILTER = True
ENTRY_TREND_RANGE1_MAX = 0.04     # 昨日 (High-Low)/Close 上限
ENABLE_SIGMA_FILTER = True
ENTRY_TREND_SIGMA_MIN = 0.0003    # 当前 sigma 下限

# 开盘冷清半仓（quiet_open.py）：当日 09:30-09:39 成交量 / 前 N 个交易日同窗口均值 < 阈值 → 当日仓位乘以系数
QUIET_OPEN_HALF_POSITION = True
QUIET_OPEN_REL_THRESHOLD = 0.65
QUIET_OPEN_LOOKBACK_DAYS = 20
QUIET_OPEN_POSITION_MULT = 0.5
