#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from datetime import date, timedelta
from zoneinfo import ZoneInfo
from longport.openapi import QuoteContext, Config, Period, AdjustType
import pandas as pd

# ———— 配置 & 初始化 ————
config = Config.from_env()
ctx    = QuoteContext(config)

# ———— 时区定义 ————
TZ_HK = ZoneInfo('Asia/Hong_Kong')
TZ_ET = ZoneInfo('US/Eastern')

# ———— 用户参数：美东起止日期（inclusive） ————
# 注意：history_candlesticks_by_date 接口接受 date 类型
start_date = date(2025, 9, 1)
end_date   = date(2025, 9, 30)

all_candles = []

# ———— 按天拉：每次用 history_candlesticks_by_date ————
current = start_date
while current <= end_date:
    resp = ctx.history_candlesticks_by_date(
        "QQQ.US",
        Period.Min_1,
        AdjustType.NoAdjust,
        current,
        current
    )
    print(f"{current} → 拉到 {len(resp)} 条")
    all_candles.extend(resp)
    current += timedelta(days=1)

# ———— 转换时区 & 保存 ————
rows = []
for c in all_candles:
    # API 返回的 timestamp 是香港本地的 naive 时间
    dt_hk = c.timestamp.replace(tzinfo=TZ_HK)
    # 转到美东
    dt_et = dt_hk.astimezone(TZ_ET)
    rows.append({
        'DateTime': dt_et.strftime('%Y-%m-%d %H:%M:%S'),
        'Open':      c.open,
        'High':      c.high,
        'Low':       c.low,
        'Close':     c.close,
        'Volume':    c.volume,
        'Turnover':  c.turnover
    })

df = pd.DataFrame(rows)

# 检查并去除重复的时间戳，保留最后一条记录（通常是更新后的数据）
initial_count = len(df)
df = df.drop_duplicates(subset=['DateTime'], keep='last')
final_count = len(df)

if initial_count > final_count:
    print(f"⚠️  发现并去除了 {initial_count - final_count} 条重复的时间戳记录")

df.to_csv('qqq_longport.csv', index=False)
print(f"✔️ 已保存 qqq_longport.csv，共 {len(df)} 条记录（所有时间均为美东本地时间）。")
