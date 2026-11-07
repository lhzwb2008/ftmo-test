#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""跟 239 上 simulate.py（Longport QQQ 实盘）的开平仓，在 E8 Pro Perpetual 上下同方向的纳指永续。

只跟方向，不跟股数。仓位按 E8 初始资金的 0.5 倍名义金额计算。
E8 不接受市价单，用穿过盘口的限价单成交；每笔带美元止损，进程挂了也不会把日亏线打穿。
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, time as dtime

import pytz

ET = pytz.timezone("America/New_York")
ENDPOINT = "https://trade.e8markets.com/api/mcp"
SYMBOL = "HL_HIP3_XYZ_XYZ100"
LOG_PATH = os.environ.get("SIMULATE_LOG", "/root/ftmo-test/simulate.log")
STATE_PATH = os.environ.get("E8_STATE", "/root/ftmo-test/e8_follow_state.json")

# $5,000、日亏 $125、总亏 $400、单日计入上限 $100。
# 近一年（2025-10-10 ~ 2026-10-16）4x 通过率最高、也够快：约 3 个月内 62% 的起点能过，
# 中位 26 个交易日。再加大杠杆只增加爆仓，因为单日计入被 $100 封顶，亏的一边却放大。
NOTIONAL_MULT = 4.0
LOT_STEP = 0.01
MAX_LOTS = 1.5
MIN_PRICE = 5_000.0
MAX_PRICE = 80_000.0
# 官方日亏 2.5%（$125）之前先平；总亏 8%（底线 $4,600）留 $100。
DAILY_LOSS_HALT = 100.0
DAILY_ROOM_HALT = 0.5          # room.dailyPct，剩余百分点；0.5 = 还剩 $25
STATIC_ROOM_HALT = 2.0         # 剩余 2 个百分点 = 离 $4,600 还有 $100
PROFIT_CAP_HALT = 90.0         # 官方单日计入上限 $100，到 $90 就停
STOP_LOSS_USD = 110.0          # 略宽于脚本的 $100 日亏停手，只兜进程失联；再紧会切掉策略自己的出场
CROSS_PCT = 0.0015             # 限价穿过最新价 0.15%，相当于立刻成交的市价单
SESSION_OPEN = dtime(9, 40)
SESSION_CLOSE = dtime(15, 39)

OPEN_RE = re.compile(r"\[(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})\] 开仓成功: (Buy|Sell) ")
CLOSE_RE = re.compile(r"\[(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2})\] 平仓成功")


def log(msg):
    now = datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now}] {msg}", flush=True)


def call(tool, args=None):
    key = os.environ["E8_API_KEY"]
    body = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": tool, "arguments": args or {}},
    }).encode()
    req = urllib.request.Request(ENDPOINT, data=body, headers={
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300]
        raise RuntimeError(f"{tool} HTTP {exc.code}: {detail}") from exc
    outer = json.loads(raw)
    if "error" in outer:
        raise RuntimeError(f"{tool}: {outer['error'].get('message', outer['error'])}")
    return json.loads(outer["result"]["content"][0]["text"])


def parse_event(line):
    opened = OPEN_RE.search(line)
    if opened:
        return {"date": opened.group(1), "tod": opened.group(2),
                "side": "long" if opened.group(3) == "Buy" else "short", "raw": line.strip()}
    closed = CLOSE_RE.search(line)
    if closed:
        return {"date": closed.group(1), "tod": closed.group(2), "side": "flat", "raw": line.strip()}
    return None


def load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_state(state):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh)
    os.replace(tmp, STATE_PATH)


def replay_today(today):
    """今天已经发生的开平仓折叠成当前应持方向。历史日期全部忽略。"""
    desired = "flat"
    last_raw = None
    try:
        with open(LOG_PATH, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                event = parse_event(line)
                if event and event["date"] == today:
                    desired = event["side"]
                    last_raw = event["raw"]
            offset = fh.tell()
    except OSError:
        offset = 0
    return desired, last_raw, offset


def read_new_events(offset, today):
    desired_updates = []
    try:
        with open(LOG_PATH, encoding="utf-8", errors="replace") as fh:
            fh.seek(offset)
            chunk = fh.read()
            new_offset = fh.tell()
    except OSError:
        return [], offset
    if not chunk:
        return [], offset
    # 最后一行可能没写完，留到下一轮
    if not chunk.endswith("\n"):
        head, _, tail = chunk.rpartition("\n")
        new_offset -= len(tail.encode())
        chunk = head + ("\n" if head else "")
    for line in chunk.splitlines():
        event = parse_event(line)
        if event and event["date"] == today:
            desired_updates.append(event)
    return desired_updates, new_offset


def in_session(now):
    if now.weekday() >= 5:
        return False
    return SESSION_OPEN <= now.time() < SESSION_CLOSE


def position_side(positions):
    for pos in positions:
        if pos.get("symbol") != SYMBOL:
            continue
        side = str(pos.get("side", "")).lower()
        if side in ("long", "buy"):
            return "long", pos
        if side in ("short", "sell"):
            return "short", pos
    return "flat", None


def lots_for(price, initial):
    if price < MIN_PRICE or price > MAX_PRICE:
        raise RuntimeError(f"价格异常 {price}，拒绝下单")
    notional = initial * NOTIONAL_MULT
    lots = int(notional / price / LOT_STEP) * LOT_STEP
    lots = min(lots, MAX_LOTS)
    if lots < LOT_STEP:
        raise RuntimeError(f"计算出的手数 {lots} 低于最小手数")
    return round(lots, 2), round(notional, 2)


def aggressive_limit(side, price):
    if side == "buy":
        return round(price * (1 + CROSS_PCT), 2)
    return round(price * (1 - CROSS_PCT), 2)


def stop_price(side, price, lots):
    distance = STOP_LOSS_USD / lots
    if side == "buy":
        return round(price - distance, 2)
    return round(price + distance, 2)


def open_position(account, side, lots, price):
    order_side = "buy" if side == "long" else "sell"
    limit = aggressive_limit(order_side, price)
    stop = stop_price(order_side, price, lots)
    client_id = f"e8f-{int(time.time())}-{side}-{lots}"
    result = call("e8_order_place", {
        "accountId": account,
        "symbol": SYMBOL,
        "side": order_side,
        "type": "limit",
        "amount": lots,
        "limitPrice": limit,
        "stopLoss": stop,
        "slippage": 50,
        "clientOrderId": client_id,
    })
    order = result.get("order") or {}
    log(f"提交开仓 {side} {lots} 张 限价 {limit} 止损 {stop} 状态 {order.get('status')} id {order.get('id')}")
    return order.get("id")


def cancel_order(order_id):
    if not order_id:
        return
    try:
        call("e8_order_cancel", {"orderId": order_id})
        log(f"撤销未成交挂单 {order_id}")
    except RuntimeError as exc:
        log(f"撤单失败（可能已成交）{order_id}: {exc}")


def close_position(account):
    result = call("e8_position_close", {"accountId": account, "symbol": SYMBOL, "slippage": 50})
    log(f"平仓 {result.get('status')} 成交价 {result.get('executedPrice')} 盈亏 {result.get('realizedPnl')}")


def risk_block(limits):
    """返回需要强制空仓的原因；不需要时返回空字符串。"""
    if not limits:
        return ""
    breached = limits.get("breached") or {}
    if any(breached.get(k) for k in ("daily", "total", "margin")):
        return "账户已触线"
    pnl = (limits.get("pnl") or {}).get("today")
    room = limits.get("room") or {}
    if pnl is not None and pnl <= -DAILY_LOSS_HALT:
        return f"当日已亏 ${pnl:.0f}，停在日亏线前面"
    if pnl is not None and pnl >= PROFIT_CAP_HALT:
        return f"当日已盈 ${pnl:.0f}，停在单日计入上限前面"
    daily_room = room.get("dailyPct")
    if daily_room is not None and daily_room <= DAILY_ROOM_HALT:
        return f"日亏剩余 {daily_room:.2f} 个百分点"
    total_room = room.get("totalPct")
    if total_room is not None and total_room <= STATIC_ROOM_HALT:
        return f"总亏剩余 {total_room:.2f} 个百分点"
    return ""


def main():
    account = os.environ["E8_ACCOUNT_ID"]
    limits = call("e8_account_limits", {"accountId": account})
    initial = float(limits.get("initialBalance") or 5000)
    log(f"E8 跟单启动 账户 {limits.get('accountId')} 初始 ${initial:.0f} "
        f"名义 {NOTIONAL_MULT:g}x=${initial * NOTIONAL_MULT:.0f} 品种 {SYMBOL}")
    log(f"日亏停 ${DAILY_LOSS_HALT:.0f} / 官方 ${initial * 0.025:.0f}；"
        f"总亏底线 ${initial * 0.92:.0f}，剩余 {STATIC_ROOM_HALT:g} 个百分点即停；"
        f"单日盈利停 ${PROFIT_CAP_HALT:.0f}")

    today = datetime.now(ET).date().isoformat()
    desired, last_raw, offset = replay_today(today)
    if last_raw:
        log(f"今日最近一条信号: {last_raw}")
    log(f"回放后目标仓位: {desired}")
    state = load_state()
    state.update({"offset": offset, "day": today, "desired": desired})
    save_state(state)
    pending_order = None
    pending_since = 0.0
    last_block = ""

    while True:
        now = datetime.now(ET)
        day = now.date().isoformat()
        if day != today:
            today = day
            desired = "flat"
            log("新的一天，目标仓位清成空仓，等待今天的信号")

        events, offset = read_new_events(offset, today)
        for event in events:
            desired = event["side"]
            log(f"信号 {event['raw']} → 目标 {desired}")
        state.update({"offset": offset, "day": today, "desired": desired})
        save_state(state)

        try:
            limits = call("e8_account_limits", {"accountId": account})
            positions = call("e8_positions_list", {"accountId": account}).get("positions") or []
            asset = call("e8_trade_asset_get", {"symbol": SYMBOL})
        except (RuntimeError, urllib.error.URLError, TimeoutError, OSError) as exc:
            log(f"查询失败，下轮重试: {exc}")
            time.sleep(5)
            continue

        reason = risk_block(limits)
        target = desired if in_session(now) and not reason else "flat"
        if reason != last_block:
            if reason:
                log(f"风控空仓: {reason}")
            last_block = reason
        actual, _pos = position_side(positions)

        if pending_order and time.time() - pending_since > 20 and actual == "flat":
            cancel_order(pending_order)
            pending_order = None

        try:
            if actual != "flat" and actual != target:
                close_position(account)
                pending_order = None
            elif actual == "flat" and target != "flat":
                if pending_order:
                    pass
                else:
                    price = float(asset["lastPrice"])
                    lots, notional = lots_for(price, initial)
                    log(f"开仓目标 {target} 价格 {price} 手数 {lots} 名义约 ${lots * price:.0f}（计划 ${notional:.0f}）")
                    pending_order = open_position(account, target, lots, price)
                    pending_since = time.time()
            elif actual == target and actual != "flat":
                pending_order = None
        except (RuntimeError, urllib.error.URLError, TimeoutError, OSError) as exc:
            log(f"下单失败，下轮重试: {exc}")

        time.sleep(3)


if __name__ == "__main__":
    main()
