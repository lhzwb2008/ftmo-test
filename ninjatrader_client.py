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

账户/合约配置由各 simulate_*.py 在启动时传入（一台机器可多开多个进程, 各管各的账户）:
    account       NT8 里的账户名（Accounts 标签页 Name 列, 非 Display Name）
    instrument    NT8 格式的合约名（含到期月, 每季度换月时需手动更新!）
    incoming_dir  可选: incoming 文件夹路径, 默认 ~/Documents/NinjaTrader 8/incoming（同机所有程序共用同一目录）
    file_tag      可选: 写入 OIF 文件名的短标签, 多进程并行时避免文件名碰撞

ATI 指令格式（分号分隔, 可选字段留空但保留分号）:
    PLACE;<账户>;<合约>;<BUY|SELL>;<手数>;MARKET;0;0;DAY;;;;
    CLOSEPOSITION;<账户>;<合约>;;;;;;;;;;
    FLATTENEVERYTHING;;;;;;;;;;;;   # 危险: 平掉该 NT8 下全部账户; 多账户场景请勿使用
"""
import os
import re
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


def sanitize_file_tag(tag):
    """OIF 文件名只保留安全字符, 避免路径/空格问题。"""
    if not tag:
        return "nt8"
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", str(tag).strip())
    return cleaned[:48] or "nt8"


class NinjaTraderClient:
    def __init__(self, account, instrument, incoming_dir=None, file_tag=None):
        self.account = account
        self.instrument = instrument
        self.incoming_dir = incoming_dir or default_incoming_dir()
        self.file_tag = sanitize_file_tag(file_tag or account)

        missing = [k for k, v in {
            "account": self.account,
            "instrument": self.instrument,
        }.items() if not v]
        if missing:
            raise NinjaTraderError(f"缺少配置: {', '.join(missing)}（请在启动参数或脚本中填写）")

        if not os.path.isdir(self.incoming_dir):
            raise NinjaTraderError(
                f"NT8 incoming 文件夹不存在: {os.path.abspath(self.incoming_dir)}\n"
                "请确认 NinjaTrader 8 已安装且已启用 ATI (Tools → Options → Automated trading interface), "
                "或通过 NT8_INCOMING_DIR 指定正确路径"
            )

    MAX_WRITE_RETRIES = 6

    def _write_oif(self, lines):
        """直接写 .txt 到 incoming 目录。
        ⚠️ 不能用 tmp+rename 的原子写法: NT8 ATI 监听的是文件"创建"事件——
        改名不触发处理(指令被无视), 而 .tmp 落盘会被 NT8 立刻抢占报 Unknown OIF file type
        并锁住文件导致改名 WinError 5(2026-07-08 实盘已踩坑)。
        指令只有一行, 单次 write+close 足够快, 半截文件风险可忽略。
        失败(目录被杀毒软件等瞬时锁定)则指数退避重试, 每次换新文件名。
        文件名含 file_tag, 多进程并行时避免碰撞。"""
        last_err = None
        for attempt in range(self.MAX_WRITE_RETRIES):
            fname = f"oif_{self.file_tag}_{int(time.time() * 1000)}_{attempt}"
            final_path = os.path.join(self.incoming_dir, fname + ".txt")
            try:
                with open(final_path, "w", encoding="ascii", newline="\r\n") as f:
                    for line in lines:
                        f.write(line + "\n")
                return final_path
            except OSError as e:
                last_err = e
                time.sleep(0.5 * (attempt + 1))
        raise NinjaTraderError(f"OIF 写入重试 {self.MAX_WRITE_RETRIES} 次后仍失败: {last_err}")

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
        """紧急指令: 平掉该 NT8 客户端下所有账户所有持仓并撤销全部挂单。
        ⚠️ 多账户并行时严禁调用——会误伤其他进程管理的账户。"""
        path = self._write_oif(["FLATTENEVERYTHING;;;;;;;;;;;;"])
        return os.path.basename(path)


def nt8_data_dir():
    """NinjaTrader 8 用户目录。中文 Windows 的“文档”实际路径仍是 Documents。"""
    home = os.path.expanduser("~")
    for rel in (
        ("Documents", "NinjaTrader 8"),
        ("OneDrive", "Documents", "NinjaTrader 8"),
        ("My Documents", "NinjaTrader 8"),
    ):
        path = os.path.join(home, *rel)
        if os.path.isdir(path):
            return path
    return os.path.join(home, "Documents", "NinjaTrader 8")


def _scan_bytes_for_accounts(data, prefix):
    """从文本或 UTF-16 字节里抽出以 prefix 开头的账户名。"""
    prefix = prefix.upper()
    found = []
    for encoding, step in (("ascii", 1), ("utf-16le", 2)):
        needle = prefix.encode(encoding)
        start = 0
        while True:
            index = data.find(needle, start)
            if index < 0:
                break
            chars = [prefix]
            pos = index + len(needle)
            while pos + step <= len(data):
                chunk = data[pos:pos + step]
                if encoding == "utf-16le" and chunk[1] != 0:
                    break
                char = chr(chunk[0])
                if not char.isalnum():
                    break
                chars.append(char.upper())
                pos += step
            name = "".join(chars)
            if len(name) > len(prefix):
                found.append(name)
            start = index + max(len(needle), 1)
    return found


def discover_nt8_accounts(prefix, max_age_hours=36, root=None):
    """读 NinjaTrader 最近的 log/trace，找出当前登录过的账户名。

    ATI 文件接口不能列举账户。账户一连上，名字会出现在当天日志里。
    只看最近 max_age_hours 内改过的文件，避免把以前用过、现在已不在列表里的旧账户也选上。
    数据库里有全部历史账户，故意不扫。
    """
    if not prefix:
        return []
    root = root or nt8_data_dir()
    cutoff = time.time() - max_age_hours * 3600
    paths = []
    for folder in ("log", "trace", "outgoing"):
        directory = os.path.join(root, folder)
        if not os.path.isdir(directory):
            continue
        for name in os.listdir(directory):
            path = os.path.join(directory, name)
            if not os.path.isfile(path):
                continue
            try:
                if os.path.getmtime(path) >= cutoff and os.path.getsize(path) <= 80 * 1024 * 1024:
                    paths.append(path)
            except OSError:
                continue
    found = []
    seen = set()
    for path in sorted(paths, key=lambda item: os.path.getmtime(item), reverse=True):
        try:
            size = os.path.getsize(path)
            with open(path, "rb") as handle:
                if size > 4 * 1024 * 1024:
                    handle.seek(size - 4 * 1024 * 1024)
                data = handle.read()
        except OSError:
            continue
        for account in _scan_bytes_for_accounts(data, prefix):
            if account not in seen:
                seen.add(account)
                found.append(account)
    return sorted(found)


def _process_alive(pid):
    if pid <= 0:
        return False
    if platform.system() == "Windows":
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _account_lock_path(account):
    os.makedirs("logs", exist_ok=True)
    return os.path.join("logs", "nt8_%s.pid" % sanitize_file_tag(account))


def _locked_by_other(account):
    path = _account_lock_path(account)
    if not os.path.exists(path):
        return None
    try:
        pid = int(open(path, encoding="ascii").read().strip())
    except (OSError, ValueError):
        return None
    if pid == os.getpid() or not _process_alive(pid):
        return None
    return pid


def claim_nt8_account(account):
    """占住这个账户，避免同一台机器上两个进程下同一个账户。进程退出时释放。"""
    import atexit
    path = _account_lock_path(account)
    fd = None
    for _ in range(2):
        other = _locked_by_other(account)
        if other:
            raise NinjaTraderError("账户 %s 已有程序在跑（pid %s）" % (account, other))
        try:
            os.remove(path)
        except OSError:
            pass
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            fd = None
    if fd is None:
        raise NinjaTraderError("账户 %s 刚被另一个窗口领走，请再启动一次" % account)
    os.write(fd, str(os.getpid()).encode("ascii"))
    os.close(fd)

    def _release():
        try:
            if int(open(path, encoding="ascii").read().strip()) == os.getpid():
                os.remove(path)
        except (OSError, ValueError):
            pass

    atexit.register(_release)
    return path


def choose_nt8_account(prefix):
    """有账户前缀时，从 NinjaTrader 日志自动领一个还没被占用的账户。领不到再手填。"""
    if prefix:
        found = discover_nt8_accounts(prefix)
        if found:
            free = [name for name in found if not _locked_by_other(name)]
            busy = [(name, _locked_by_other(name)) for name in found if _locked_by_other(name)]
            if free:
                chosen = free[0]
                others = "、".join(free[1:])
                print("从 NinjaTrader 读到 %d 个 %s 账户，本窗口自动使用 %s" % (len(found), prefix, chosen))
                if others:
                    print("再开一个窗口会自动用剩下的: %s" % others)
                if busy:
                    print("已在跑、跳过: " + "、".join("%s（pid %s）" % (name, pid) for name, pid in busy))
                claim_nt8_account(chosen)
                return chosen
            print("读到的账户都已有程序在跑: " + "、".join(
                "%s（pid %s）" % (name, pid) for name, pid in busy))
        else:
            print("没有在 %s 最近的日志里找到 %s 开头的账户（请先在 NinjaTrader 连上账户）。" % (nt8_data_dir(), prefix))
            print("改为手动输入。")

    while True:
        try:
            account = input("请输入 NT8 账户名（Accounts 标签页 Name 列）: ").strip()
        except EOFError:
            raise NinjaTraderError("无法读取输入")
        if not account:
            print("错误: 账户名不能为空")
            continue
        if prefix and not account.upper().startswith(prefix.upper()):
            confirm = input("警告: 该账户名不以 %s 开头。仍要继续? (yes/no): " % prefix).strip().lower()
            if confirm not in ("y", "yes"):
                continue
        other = _locked_by_other(account)
        if other:
            print("账户 %s 已有程序在跑（pid %s），请换一个" % (account, other))
            continue
        claim_nt8_account(account)
        return account


def create_client_or_none(account, instrument, incoming_dir=None, file_tag=None, logger_print=print):
    """尝试创建客户端; 缺少配置或 incoming 目录不可用时返回 None(仅记录信号模式)。"""
    try:
        client = NinjaTraderClient(account, instrument, incoming_dir, file_tag=file_tag)
        logger_print(f"NinjaTrader ATI 已就绪: 账户={client.account}, 合约={client.instrument}, tag={client.file_tag}")
        logger_print(f"OIF 指令目录: {os.path.abspath(client.incoming_dir)}")
        logger_print("⚠️ 请确认 NT8 客户端已登录且 ATI 开关已启用, 否则指令文件不会被执行")
        return client
    except Exception as e:
        logger_print(f"⚠️ NinjaTrader 客户端未启用: {e}")
        logger_print("⚠️ 将以【仅记录信号】模式运行（信号写入 SQLite 与日志，不实际下单）")
        return None
