# -*- coding: utf-8 -*-
"""
Tradovate REST API 客户端（用于 FundedNext Futures 等期货 prop 账户）

环境变量（写在 .env 中）:
    TRADOVATE_ENV=demo            # demo=模拟环境, live=实盘环境（FundedNext 挑战账户通常在 demo 环境）
    TRADOVATE_USERNAME=xxx        # Tradovate 登录用户名
    TRADOVATE_PASSWORD=xxx        # Tradovate 登录密码
    TRADOVATE_APP_ID=xxx          # API 应用 ID（Tradovate 后台 API Access 申请）
    TRADOVATE_APP_VERSION=1.0
    TRADOVATE_CID=xxx             # API client id
    TRADOVATE_SECRET=xxx          # API client secret
    TRADOVATE_ACCOUNT_SPEC=xxx    # 可选: 指定账户名(FundedNext 分配的账户)，不填则用第一个账户
    TRADOVATE_CONTRACT=MNQU6      # 可选: 指定合约代码(含到期月)，不填则自动取 MNQ 最近月合约
"""
import os
import time
import threading

import requests

BASE_URLS = {
    "demo": "https://demo.tradovateapi.com/v1",
    "live": "https://live.tradovateapi.com/v1",
}


class TradovateError(Exception):
    pass


class TradovateClient:
    def __init__(self):
        self.env = os.environ.get("TRADOVATE_ENV", "demo").lower()
        if self.env not in BASE_URLS:
            raise TradovateError(f"TRADOVATE_ENV 必须是 demo 或 live, 当前: {self.env}")
        self.base = BASE_URLS[self.env]
        self.username = os.environ.get("TRADOVATE_USERNAME")
        self.password = os.environ.get("TRADOVATE_PASSWORD")
        self.app_id = os.environ.get("TRADOVATE_APP_ID", "SQLiteSignalBridge")
        self.app_version = os.environ.get("TRADOVATE_APP_VERSION", "1.0")
        self.cid = os.environ.get("TRADOVATE_CID")
        self.secret = os.environ.get("TRADOVATE_SECRET")
        self.account_spec = os.environ.get("TRADOVATE_ACCOUNT_SPEC")
        self.contract_name = os.environ.get("TRADOVATE_CONTRACT")

        self._token = None
        self._token_expire_ts = 0
        self._account_id = None
        self._contract_id = None
        self._lock = threading.Lock()

        missing = [k for k, v in {
            "TRADOVATE_USERNAME": self.username,
            "TRADOVATE_PASSWORD": self.password,
            "TRADOVATE_CID": self.cid,
            "TRADOVATE_SECRET": self.secret,
        }.items() if not v]
        if missing:
            raise TradovateError(f"缺少环境变量: {', '.join(missing)}")

    # ---------- 认证 ----------
    def _authenticate(self):
        resp = requests.post(f"{self.base}/auth/accesstokenrequest", json={
            "name": self.username,
            "password": self.password,
            "appId": self.app_id,
            "appVersion": self.app_version,
            "cid": int(self.cid),
            "sec": self.secret,
        }, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if "accessToken" not in data:
            # p-ticket 表示触发了限流/需要等待
            raise TradovateError(f"认证失败: {data}")
        self._token = data["accessToken"]
        # token 有效期约 80 分钟，提前 5 分钟刷新
        self._token_expire_ts = time.time() + 75 * 60

    def _headers(self):
        with self._lock:
            if self._token is None or time.time() >= self._token_expire_ts:
                self._authenticate()
        return {"Authorization": f"Bearer {self._token}"}

    def _get(self, path, params=None):
        resp = requests.get(f"{self.base}{path}", headers=self._headers(), params=params, timeout=15)
        resp.raise_for_status()
        return resp.json()

    def _post(self, path, payload):
        resp = requests.post(f"{self.base}{path}", headers=self._headers(), json=payload, timeout=15)
        resp.raise_for_status()
        return resp.json()

    # ---------- 账户 ----------
    def get_account(self):
        """返回 (account_id, account_spec)。优先匹配 TRADOVATE_ACCOUNT_SPEC。"""
        if self._account_id is not None:
            return self._account_id, self.account_spec
        accounts = self._get("/account/list")
        if not accounts:
            raise TradovateError("账户列表为空")
        acct = None
        if self.account_spec:
            for a in accounts:
                if a.get("name") == self.account_spec:
                    acct = a
                    break
            if acct is None:
                raise TradovateError(f"未找到账户 {self.account_spec}, 可用: {[a.get('name') for a in accounts]}")
        else:
            acct = accounts[0]
            self.account_spec = acct.get("name")
        self._account_id = acct["id"]
        return self._account_id, self.account_spec

    def get_equity(self):
        """返回当前账户净值（现金余额 + 未实现盈亏）。失败抛异常。"""
        account_id, _ = self.get_account()
        snap = self._get("/cashBalance/getcashbalancesnapshot", params={"accountId": account_id})
        # totalCashValue 含已实现; openPnL/realizedPnL 字段命名随版本略有差异, 做兼容
        cash = snap.get("totalCashValue", snap.get("cashBalance", 0.0)) or 0.0
        open_pnl = snap.get("openPnL", snap.get("openPnl", 0.0)) or 0.0
        return float(cash) + float(open_pnl)

    # ---------- 合约 ----------
    def get_contract_id(self):
        """解析合约: 指定 TRADOVATE_CONTRACT 时精确查找, 否则取 MNQ 最近月。"""
        if self._contract_id is not None:
            return self._contract_id
        if self.contract_name:
            found = self._get("/contract/find", params={"name": self.contract_name})
            if not found or "id" not in found:
                raise TradovateError(f"未找到合约 {self.contract_name}")
            self._contract_id = found["id"]
        else:
            suggestions = self._get("/contract/suggest", params={"t": "MNQ", "l": 2})
            if not suggestions:
                raise TradovateError("MNQ 合约自动匹配失败, 请设置 TRADOVATE_CONTRACT")
            self._contract_id = suggestions[0]["id"]
            self.contract_name = suggestions[0]["name"]
        return self._contract_id

    # ---------- 持仓 ----------
    def get_net_position(self):
        """返回当前合约净持仓手数(多为正/空为负), 无持仓返回 0。"""
        account_id, _ = self.get_account()
        contract_id = self.get_contract_id()
        positions = self._get("/position/list")
        for p in positions:
            if p.get("accountId") == account_id and p.get("contractId") == contract_id:
                return int(p.get("netPos", 0) or 0)
        return 0

    # ---------- 下单 ----------
    def place_market_order(self, action, qty):
        """市价单。action: 'Buy'/'Sell', qty: 手数(正整数)。返回订单 id。"""
        if qty <= 0:
            raise TradovateError(f"下单手数必须为正: {qty}")
        account_id, account_spec = self.get_account()
        self.get_contract_id()
        result = self._post("/order/placeorder", {
            "accountSpec": account_spec,
            "accountId": account_id,
            "action": action,
            "symbol": self.contract_name,
            "orderQty": int(qty),
            "orderType": "Market",
            "isAutomated": True,
        })
        if "orderId" not in result:
            raise TradovateError(f"下单失败: {result}")
        return result["orderId"]

    def close_all(self):
        """平掉当前合约全部净持仓。返回 (订单id或None, 平仓手数)。"""
        net = self.get_net_position()
        if net == 0:
            return None, 0
        action = "Sell" if net > 0 else "Buy"
        order_id = self.place_market_order(action, abs(net))
        return order_id, abs(net)


def create_client_or_none(logger_print=print):
    """尝试创建客户端; 缺少配置时返回 None(信号记录模式)。"""
    try:
        client = TradovateClient()
        account_id, spec = client.get_account()
        contract_id = client.get_contract_id()
        logger_print(f"Tradovate 已连接: env={client.env}, 账户={spec}(id={account_id}), 合约={client.contract_name}(id={contract_id})")
        return client
    except Exception as e:
        logger_print(f"⚠️ Tradovate 客户端未启用: {e}")
        logger_print("⚠️ 将以【仅记录信号】模式运行（信号写入 SQLite 与日志，不实际下单）")
        return None
