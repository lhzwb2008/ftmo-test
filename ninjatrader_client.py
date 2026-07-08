# -*- coding: utf-8 -*-
"""
NinjaTrader 8 ATI (Automated Trading Interface) 文件接口客户端
用于 FundedNext Futures 等期货 prop 账户的自动下单。

原理: 把一行 ATI 指令写成 oif*.txt 丢进 NT8 监听的 incoming 文件夹,
NT8 会立即读取执行（与 CFD 方案中 "SQLite + MT5 EA" 相同的落地文件架构）。

Windows 服务器上的准备工作:
    1. 安装 NinjaTrader 8, 用 FundedNext 提供的凭据登录（连接到对应的 prop 连接）
    2. Tools → Options → Automated trading interface → 勾选 "AT Interface" 启用 ATI
    3. NT8 与本脚本跑在同一台 Windows 机器上

账户/合约配置直接写在各 simulate_*.py 顶部（同一台机器可运行多家 prop firm 的程序, 各自传入自己的账户）:
    NT8_ACCOUNT   NT8 里的账户名（Accounts 标签页 Name 列, 非 Display Name）
    NT8_INSTRUMENT  NT8 格式的合约名（含到期月, 每季度换月时需手动更新!）
    incoming_dir  可选: incoming 文件夹路径, 默认 ~/Documents/NinjaTrader 8/incoming（同机所有程序共用同一目录）

ATI 指令格式（分号分隔, 可选字段留空但保留分号）:
    PLACE;<账户>;<合约>;<BUY|SELL>;<手数>;MARKET;0;0;DAY;;;;
    CLOSEPOSITION;<账户>;<合约>;;;;;;;;;;
    FLATTENEVERYTHING;;;;;;;;;;;;
"""
import os
import time
import platform


class NinjaTraderError(Exception):
    pass


def default_incoming_dir():
    if platform.system() == "Windows":
        docs = os.path.join(os.path.expanduser("~"), "Documents")
        return os.path.join(docs, "NinjaTrader 8", "incoming")
    # 非 Windows 仅用于本地测试
    return os.path.join(".", "nt8_incoming")


class NinjaTraderClient:
    def __init__(self, account, instrument, incoming_dir=None):
        self.account = account
        self.instrument = instrument
        self.incoming_dir = incoming_dir or default_incoming_dir()

        missing = [k for k, v in {
            "account": self.account,
            "instrument": self.instrument,
        }.items() if not v]
        if missing:
            raise NinjaTraderError(f"缺少配置: {', '.join(missing)}（请在 simulate 脚本顶部填写）")

        if not os.path.isdir(self.incoming_dir):
            raise NinjaTraderError(
                f"NT8 incoming 文件夹不存在: {os.path.abspath(self.incoming_dir)}\n"
                "请确认 NinjaTrader 8 已安装且已启用 ATI (Tools → Options → Automated trading interface), "
                "或通过 NT8_INCOMING_DIR 指定正确路径"
            )

    def _write_oif(self, lines):
        """原子写入 OIF 文件: 先写临时文件再改名, 避免 NT8 读到半截文件。返回最终文件路径。"""
        fname = f"oif_{int(time.time() * 1000)}"
        tmp_path = os.path.join(self.incoming_dir, fname + ".tmp")
        final_path = os.path.join(self.incoming_dir, fname + ".txt")
        with open(tmp_path, "w", encoding="ascii", newline="\r\n") as f:
            for line in lines:
                f.write(line + "\n")
        os.rename(tmp_path, final_path)
        return final_path

    def place_market_order(self, action, qty):
        """市价单。action: 'Buy'/'Sell', qty: 手数(正整数)。返回 OIF 文件名作为订单标识。"""
        if qty <= 0:
            raise NinjaTraderError(f"下单手数必须为正: {qty}")
        act = "BUY" if action.lower() == "buy" else "SELL"
        line = f"PLACE;{self.account};{self.instrument};{act};{int(qty)};MARKET;0;0;DAY;;;;"
        path = self._write_oif([line])
        return os.path.basename(path)

    def close_all(self):
        """平掉该合约在该账户的全部持仓（NT8 CLOSEPOSITION 指令, 无持仓时 NT8 自行忽略）。
        返回 (OIF 文件名, None)。文件接口无法查询持仓数量, 手数由 NT8 端决定。"""
        line = f"CLOSEPOSITION;{self.account};{self.instrument};;;;;;;;;;"
        path = self._write_oif([line])
        return os.path.basename(path), None

    def flatten_everything(self):
        """紧急指令: 平掉该 NT8 客户端下所有账户所有持仓并撤销全部挂单。"""
        path = self._write_oif(["FLATTENEVERYTHING;;;;;;;;;;;;"])
        return os.path.basename(path)


def create_client_or_none(account, instrument, incoming_dir=None, logger_print=print):
    """尝试创建客户端; 缺少配置或 incoming 目录不可用时返回 None(仅记录信号模式)。"""
    try:
        client = NinjaTraderClient(account, instrument, incoming_dir)
        logger_print(f"NinjaTrader ATI 已就绪: 账户={client.account}, 合约={client.instrument}")
        logger_print(f"OIF 指令目录: {os.path.abspath(client.incoming_dir)}")
        logger_print("⚠️ 请确认 NT8 客户端已登录且 ATI 开关已启用, 否则指令文件不会被执行")
        return client
    except Exception as e:
        logger_print(f"⚠️ NinjaTrader 客户端未启用: {e}")
        logger_print("⚠️ 将以【仅记录信号】模式运行（信号写入 SQLite 与日志，不实际下单）")
        return None
