import os
import platform
import sqlite3
import sys
import time as time_module
from datetime import datetime, time, timedelta

import pytz
from dotenv import load_dotenv
from longport.openapi import AdjustType, Config, Market, Period, QuoteContext

from trend_er5_gate import history_days_back

load_dotenv(override=True)

# ============================================================================
# 公共行情缓存服务配置
# ============================================================================

SYMBOL = os.environ.get("SYMBOL", "QQQ.US")
LOOKBACK_DAYS = 1
REFRESH_INTERVAL_SECONDS = int(os.environ.get("MARKET_DATA_REFRESH_SECONDS", "20"))
LOG_FILE = "longport_data_service.log"


def get_common_files_dir():
    """返回和 MT5/EA 可见的 Common Files 目录；非 Windows 使用当前目录。"""
    if platform.system() == "Windows":
        appdata_path = os.environ.get("APPDATA", os.path.expanduser("~\\AppData\\Roaming"))
        mt5_common_path = os.path.join(appdata_path, "MetaQuotes", "Terminal", "Common", "Files")
        os.makedirs(mt5_common_path, exist_ok=True)
        return mt5_common_path
    return "."


MARKET_DATA_DB_PATH = os.environ.get(
    "MARKET_DATA_DB_PATH",
    os.path.join(get_common_files_dir(), "market_data_cache.db"),
)


class Logger:
    def __init__(self, log_file):
        self.terminal = sys.stdout
        self.log = open(log_file, "a", encoding="utf-8", buffering=1)
        separator = "\n" + "=" * 80 + "\n"
        separator += f"行情服务启动时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        separator += "=" * 80 + "\n"
        self.log.write(separator)
        self.log.flush()

    def write(self, message):
        self.terminal.write(message)
        self.log.write(message)
        self.log.flush()

    def flush(self):
        self.terminal.flush()
        self.log.flush()


def get_us_eastern_time():
    eastern = pytz.timezone("US/Eastern")
    return datetime.now(eastern)


def create_quote_context():
    max_retries = 5
    retry_delay = 5
    for attempt in range(max_retries):
        try:
            quote_ctx = QuoteContext(Config.from_env())
            print(f"[{get_us_eastern_time().strftime('%Y-%m-%d %H:%M:%S')}] Longport 行情连接成功")
            return quote_ctx
        except Exception as e:
            if attempt < max_retries - 1:
                print(f"[{get_us_eastern_time().strftime('%Y-%m-%d %H:%M:%S')}] 行情连接失败 ({attempt + 1}/{max_retries}): {str(e)}")
                print(f"[{get_us_eastern_time().strftime('%Y-%m-%d %H:%M:%S')}] {retry_delay}秒后重试...")
                time_module.sleep(retry_delay)
            else:
                print(f"[{get_us_eastern_time().strftime('%Y-%m-%d %H:%M:%S')}] 行情连接失败，已达最大重试次数")
                raise


def init_market_data_db():
    conn = sqlite3.connect(MARKET_DATA_DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS candles (
        symbol TEXT NOT NULL,
        datetime_et TEXT NOT NULL,
        date TEXT NOT NULL,
        time TEXT NOT NULL,
        open REAL NOT NULL,
        high REAL NOT NULL,
        low REAL NOT NULL,
        close REAL NOT NULL,
        volume REAL NOT NULL,
        turnover REAL NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (symbol, datetime_et)
    )
    """)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS quotes (
        symbol TEXT PRIMARY KEY,
        last_done TEXT,
        open TEXT,
        high TEXT,
        low TEXT,
        volume TEXT,
        turnover TEXT,
        quote_timestamp TEXT,
        updated_at TEXT NOT NULL
    )
    """)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS service_state (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """)
    conn.commit()
    conn.close()
    print(f"行情缓存数据库: {os.path.abspath(MARKET_DATA_DB_PATH)}")


def normalize_timestamp(timestamp, symbol):
    eastern = pytz.timezone("US/Eastern")
    if isinstance(timestamp, datetime):
        if timestamp.tzinfo is None:
            hour = timestamp.hour
            if symbol.endswith(".US") and 9 <= hour < 17:
                return eastern.localize(timestamp)
            if symbol.endswith(".US") and (hour >= 21 or hour < 5):
                beijing = pytz.timezone("Asia/Shanghai")
                return beijing.localize(timestamp).astimezone(eastern)
            return pytz.utc.localize(timestamp).astimezone(eastern)
        return timestamp.astimezone(eastern)
    return datetime.fromtimestamp(timestamp, eastern)


def fetch_historical_candles(quote_ctx, symbol, days_back=None):
    if days_back is None:
        days_back = history_days_back(LOOKBACK_DAYS)

    sdk_period = Period.Min_1
    adjust_type = AdjustType.ForwardAdjust
    now_et = get_us_eastern_time()
    current_date = now_et.date()
    start_date = current_date - timedelta(days=days_back)
    date_to_check = current_date
    all_rows = []
    api_call_count = 0

    while date_to_check >= start_date:
        if date_to_check.weekday() >= 5:
            date_to_check -= timedelta(days=1)
            continue

        if api_call_count > 0:
            time_module.sleep(0.2)

        day_candles = quote_ctx.history_candlesticks_by_date(
            symbol,
            sdk_period,
            adjust_type,
            date_to_check,
            date_to_check,
        )
        api_call_count += 1

        for candle in day_candles or []:
            dt = normalize_timestamp(candle.timestamp, symbol)
            if dt.date() > current_date:
                continue
            time_str = dt.strftime("%H:%M")
            if symbol.endswith(".US") and not ("09:30" <= time_str <= "16:00"):
                continue
            all_rows.append((
                symbol,
                dt.strftime("%Y-%m-%d %H:%M:%S"),
                dt.date().isoformat(),
                time_str,
                float(candle.open),
                float(candle.high),
                float(candle.low),
                float(candle.close),
                float(candle.volume),
                float(candle.turnover),
                now_et.strftime("%Y-%m-%d %H:%M:%S"),
            ))

        date_to_check -= timedelta(days=1)

    return all_rows


def upsert_candles(rows):
    conn = sqlite3.connect(MARKET_DATA_DB_PATH)
    cursor = conn.cursor()
    cursor.executemany("""
    INSERT OR REPLACE INTO candles (
        symbol, datetime_et, date, time, open, high, low, close, volume, turnover, updated_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, rows)
    conn.commit()
    conn.close()


def upsert_quote(quote_ctx, symbol):
    quotes = quote_ctx.quote([symbol])
    quote = quotes[0]
    now_et = get_us_eastern_time().strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(MARKET_DATA_DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
    INSERT OR REPLACE INTO quotes (
        symbol, last_done, open, high, low, volume, turnover, quote_timestamp, updated_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        quote.symbol,
        str(quote.last_done),
        str(quote.open),
        str(quote.high),
        str(quote.low),
        str(quote.volume),
        str(quote.turnover),
        quote.timestamp.isoformat(),
        now_et,
    ))
    cursor.execute("""
    INSERT OR REPLACE INTO service_state (key, value, updated_at)
    VALUES (?, ?, ?)
    """, ("last_success_at", now_et, now_et))
    conn.commit()
    conn.close()


def get_market(symbol):
    if symbol.endswith(".HK"):
        return Market.HK
    if symbol.endswith(".SH") or symbol.endswith(".SZ"):
        return Market.CN
    if symbol.endswith(".SG"):
        return Market.SG
    return Market.US


def upsert_trading_calendar(quote_ctx, symbol):
    now_et = get_us_eastern_time()
    current_date = now_et.date()
    calendar_resp = quote_ctx.trading_days(get_market(symbol), current_date, current_date)
    is_trade_day = current_date in calendar_resp.trading_days
    is_half_trade_day = current_date in calendar_resp.half_trading_days
    updated_at = now_et.strftime("%Y-%m-%d %H:%M:%S")

    conn = sqlite3.connect(MARKET_DATA_DB_PATH)
    cursor = conn.cursor()
    cursor.executemany("""
    INSERT OR REPLACE INTO service_state (key, value, updated_at)
    VALUES (?, ?, ?)
    """, [
        ("calendar_date", current_date.isoformat(), updated_at),
        ("is_trading_day", "1" if is_trade_day else "0", updated_at),
        ("is_half_trading_day", "1" if is_half_trade_day else "0", updated_at),
    ])
    conn.commit()
    conn.close()


def run_service():
    sys.stdout = Logger(LOG_FILE)
    sys.stderr = sys.stdout
    init_market_data_db()
    quote_ctx = create_quote_context()
    print(f"交易品种: {SYMBOL}")
    print(f"刷新间隔: {REFRESH_INTERVAL_SECONDS} 秒")

    while True:
        try:
            rows = fetch_historical_candles(quote_ctx, SYMBOL)
            if rows:
                upsert_candles(rows)
                print(f"[{get_us_eastern_time().strftime('%Y-%m-%d %H:%M:%S')}] K线缓存更新完成: {len(rows)} 条")
            else:
                print(f"[{get_us_eastern_time().strftime('%Y-%m-%d %H:%M:%S')}] 警告: 本轮没有获取到K线")
            upsert_quote(quote_ctx, SYMBOL)
            print(f"[{get_us_eastern_time().strftime('%Y-%m-%d %H:%M:%S')}] 报价缓存更新完成")
            upsert_trading_calendar(quote_ctx, SYMBOL)
            print(f"[{get_us_eastern_time().strftime('%Y-%m-%d %H:%M:%S')}] 交易日历缓存更新完成")
        except Exception as e:
            print(f"[{get_us_eastern_time().strftime('%Y-%m-%d %H:%M:%S')}] 行情缓存更新失败: {str(e)}")

        time_module.sleep(REFRESH_INTERVAL_SECONDS)


if __name__ == "__main__":
    run_service()
