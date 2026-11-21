#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from datetime import date, timedelta

import pandas as pd
import pytz
from dotenv import load_dotenv
from longport.openapi import AdjustType, Config, Period, QuoteContext

load_dotenv(override=True)

# ———— 配置 & 初始化 ————
# longport 4.x 只有 from_apikey_env / from_apikey，没有 from_env
config = Config.from_apikey_env()
ctx = QuoteContext(config)

# ———— 时区定义 ————
# history_candlesticks_by_date 的 timestamp 是 UTC 墙钟（美股 09:30 ET = 13:30 UTC），
# 不是香港本地时间；按 HK 转会错到凌晨。
TZ_UTC = pytz.UTC
TZ_ET = pytz.timezone('US/Eastern')

# ———— 用户参数：美东起止日期（inclusive） ————
# 注意：history_candlesticks_by_date 接口接受 date 类型
# 近一年行情 + 约 3 个月趋势特征预热（er5 / rank60 等）
start_date = date(2025, 6, 1)
end_date = date(2026, 9, 21)  # 美东最近完整交易日；跑脚本前可按需改

all_candles = []

# ———— 按天拉：每次用 history_candlesticks_by_date ————
current = start_date
while current <= end_date:
    if current.weekday() < 5:
        resp = ctx.history_candlesticks_by_date(
            "QQQ.US",
            Period.Min_1,
            AdjustType.ForwardAdjust,
            current,
            current
        )
        print(f"{current} → 拉到 {len(resp)} 条")
        all_candles.extend(resp)
    current += timedelta(days=1)

# ———— 转换时区 & 保存 ————
rows = []
for c in all_candles:
    ts = c.timestamp.replace(tzinfo=None)
    dt_et = TZ_UTC.localize(ts).astimezone(TZ_ET)
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
