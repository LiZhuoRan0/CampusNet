#!/usr/bin/env python3
"""BIT-Web 自动保活与深澜（SRun）认证工具（仅使用 Python 标准库）。"""

from __future__ import annotations

import argparse
import base64
import ctypes
import hashlib
import hmac
import json
import logging
import re
import subprocess
import sys
import time
from ctypes import wintypes
from dataclasses import dataclass
from http.client import RemoteDisconnected
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "config.json"
LOG_PATH = APP_DIR / "campusnet.log"
LOG_RETENTION_DAYS = 30
STANDARD_BASE64_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
BIT_SRUN_BASE64_ALPHABET = "LVoJPiCN2R8G90yg+hmFHuacZ1OWMnrsSTXkYpUq/3dlbfKwv6xztjI7DeBE45QA"


def configure_logging() -> logging.Logger:
    logger = logging.getLogger("campusnet")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
        # 每天轮转一次；当前文件加 29 个历史文件，最多覆盖约 30 个自然日。
        file_handler = TimedRotatingFileHandler(
            LOG_PATH,
            when="midnight",
            interval=1,
            backupCount=LOG_RETENTION_DAYS - 1,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)
    return logger


LOG = configure_logging()
SINGLE_INSTANCE_MUTEX: int | None = None
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
WLAN_INTF_OPCODE_RADIO_STATE = 4
WLAN_RADIO_STATE_OFF = 0
WLAN_RADIO_STATE_ON = 1
WLAN_MAX_PHY_INDEX = 64


class PortalRequestError(RuntimeError):
    """校园网门户不可达或返回异常状态。"""


class PortalTransportError(PortalRequestError):
    """校园网门户连接被中断，可通过重新关联 Wi-Fi 尝试恢复。"""


@dataclass(frozen=True)
class ConnectionAttempt:
    healthy: bool
    portal_transport_failure: bool = False


@dataclass(frozen=True)
class WifiStatus:
    """Windows WLAN 接口的实际关联状态，而不只是 connect 命令是否返回成功。"""

    interface_name: str | None
    ssid: str | None
    connected: bool


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class WLAN_INTERFACE_INFO(ctypes.Structure):
    _fields_ = [
        ("InterfaceGuid", GUID),
        ("strInterfaceDescription", ctypes.c_wchar * 256),
        ("isState", wintypes.DWORD),
    ]


class WLAN_INTERFACE_INFO_LIST(ctypes.Structure):
    _fields_ = [
        ("dwNumberOfItems", wintypes.DWORD),
        ("dwIndex", wintypes.DWORD),
        ("InterfaceInfo", WLAN_INTERFACE_INFO * 1),
    ]


class WLAN_PHY_RADIO_STATE(ctypes.Structure):
    _fields_ = [
        ("dwPhyIndex", wintypes.DWORD),
        ("dot11SoftwareRadioState", wintypes.DWORD),
        ("dot11HardwareRadioState", wintypes.DWORD),
    ]


class WLAN_RADIO_STATE(ctypes.Structure):
    _fields_ = [
        ("dwNumberOfPhys", wintypes.DWORD),
        ("PhyRadioState", WLAN_PHY_RADIO_STATE * WLAN_MAX_PHY_INDEX),
    ]


def acquire_single_instance() -> bool:
    """防止手动启动和 Windows 启动项同时创建多个保活进程。"""
    global SINGLE_INSTANCE_MUTEX
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_mutex = kernel32.CreateMutexW
    create_mutex.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
    create_mutex.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL

    handle = create_mutex(None, False, "Local\\CampusNetAutoLogin")
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        close_handle(handle)
        return False
    SINGLE_INSTANCE_MUTEX = handle  # 保持句柄存活，直至进程退出时由 Windows 自动释放。
    return True


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _blob(data: bytes) -> tuple[DATA_BLOB, Any]:
    buffer = ctypes.create_string_buffer(data)
    return DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))), buffer


def _dpapi(data: bytes, protect: bool) -> bytes:
    """使用当前 Windows 用户的 DPAPI 加密/解密配置中的密码。"""
    in_blob, keepalive = _blob(data)
    out_blob = DATA_BLOB()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    if protect:
        ok = crypt32.CryptProtectData(ctypes.byref(in_blob), "CampusNet", None, None, None, 0, ctypes.byref(out_blob))
    else:
        ok = crypt32.CryptUnprotectData(ctypes.byref(in_blob), None, None, None, None, 0, ctypes.byref(out_blob))
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)


def protect_password(password: str) -> str:
    return base64.b64encode(_dpapi(password.encode("utf-8"), True)).decode("ascii")


def unprotect_password(value: str) -> str:
    return _dpapi(base64.b64decode(value), False).decode("utf-8")


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        raise RuntimeError(f"找不到配置文件：{CONFIG_PATH}")
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    credentials = config.get("credentials", {})
    if "password" in credentials:
        # 首次运行自动将明文替换为仅当前 Windows 用户可读取的 DPAPI 密文。
        credentials["password_encrypted"] = protect_password(str(credentials.pop("password")))
        config["credentials"] = credentials
        CONFIG_PATH.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        LOG.info("已将配置文件中的密码转换为 Windows DPAPI 加密格式。")
    if not credentials.get("username") or not credentials.get("password_encrypted"):
        raise RuntimeError("config.json 缺少 credentials.username 或 credentials.password_encrypted")
    return config


def jsonp(url: str, params: dict[str, Any]) -> dict[str, Any]:
    """调用 SRun 的 JSONP 接口并剥去 callback 包装。"""
    query = urlencode({**params, "callback": "callback", "_": int(time.time() * 1000)})
    request = Request(f"{url}?{query}", headers={"User-Agent": "CampusNet/1.0"})
    try:
        with urlopen(request, timeout=10) as response:
            raw = response.read().decode("utf-8", errors="replace").strip()
    except HTTPError as error:
        raise PortalRequestError(f"校园网门户返回 HTTP {error.code}") from error
    except URLError as error:
        raise PortalTransportError(f"无法访问校园网门户：{error.reason}") from error
    except (RemoteDisconnected, ConnectionError, TimeoutError, OSError) as error:
        raise PortalTransportError(f"校园网门户连接中断：{error}") from error
    start, end = raw.find("("), raw.rfind(")")
    if start < 0 or end <= start:
        raise PortalRequestError(f"门户返回格式异常：{raw[:200]}")
    try:
        return json.loads(raw[start + 1 : end])
    except json.JSONDecodeError as error:
        raise PortalRequestError("门户返回了无效 JSON") from error


def srun_xencode(text: str, key: str) -> bytes:
    def to_longs(value: str, include_length: bool) -> list[int]:
        result = []
        for i in range(0, len(value), 4):
            result.append(sum((ord(value[i + j]) if i + j < len(value) else 0) << (8 * j) for j in range(4)))
        if include_length:
            result.append(len(value))
        return result

    values, keys = to_longs(text, True), to_longs(key, False)
    while len(keys) < 4:
        keys.append(0)
    n = len(values) - 1
    if n < 0:
        return b""
    z, delta, total = values[n], 0x9E3779B9, 0
    rounds = 6 + 52 // (n + 1)
    while rounds:
        rounds -= 1
        total = (total + delta) & 0xFFFFFFFF
        e = (total >> 2) & 3
        for p in range(n):
            y = values[p + 1]
            mix = (z >> 5) ^ (y << 2)
            mix += ((y >> 3) ^ (z << 4)) ^ (total ^ y)
            mix += keys[(p & 3) ^ e] ^ z
            z = values[p] = (values[p] + mix) & 0xFFFFFFFF
        y = values[0]
        mix = (z >> 5) ^ (y << 2)
        mix += ((y >> 3) ^ (z << 4)) ^ (total ^ y)
        mix += keys[(n & 3) ^ e] ^ z
        z = values[n] = (values[n] + mix) & 0xFFFFFFFF
    return b"".join(value.to_bytes(4, "little") for value in values)


def srun_base64_encode(data: bytes, alphabet: str = BIT_SRUN_BASE64_ALPHABET) -> str:
    """使用北理门户设置的自定义 Base64 字母表编码 SRun info。"""
    if len(alphabet) != 64 or len(set(alphabet)) != 64:
        raise ValueError("portal.base64_alphabet 必须包含 64 个不重复字符")
    encoded = base64.b64encode(data).decode("ascii")
    return encoded.translate(str.maketrans(STANDARD_BASE64_ALPHABET, alphabet))


def srun_login(config: dict[str, Any]) -> tuple[bool, str]:
    portal = config["portal"]
    credentials = config["credentials"]
    username = str(credentials["username"])
    password = unprotect_password(str(credentials["password_encrypted"]))
    base_url = str(portal["service_url"]).rstrip("/")
    challenge = jsonp(f"{base_url}/cgi-bin/get_challenge", {"username": username, "ip": ""})
    if challenge.get("error") != "ok":
        return False, str(challenge.get("error_msg") or challenge.get("error") or "获取 challenge 失败")
    token, ip = str(challenge["challenge"]), str(challenge["client_ip"])
    hmd5 = hmac.new(token.encode(), password.encode(), hashlib.md5).hexdigest()
    info_payload = json.dumps({"username": username, "password": password, "ip": ip, "acid": str(portal["ac_id"]), "enc_ver": "srun_bx1"}, separators=(",", ":"), ensure_ascii=False)
    alphabet = str(portal.get("base64_alphabet", BIT_SRUN_BASE64_ALPHABET))
    info = "{SRBX1}" + srun_base64_encode(srun_xencode(info_payload, token), alphabet)
    checksum_source = "".join(token + item for item in (username, hmd5, str(portal["ac_id"]), ip, "200", "1", info))
    response = jsonp(
        f"{base_url}/cgi-bin/srun_portal",
        {
            "action": "login", "username": username, "password": "{MD5}" + hmd5,
            "ac_id": portal["ac_id"], "ip": ip, "chksum": hashlib.sha1(checksum_source.encode()).hexdigest(),
            "info": info, "n": 200, "type": 1, "os": "Windows 10", "name": "Windows",
            "double_stack": int(portal.get("double_stack", 0)),
            "ignore": int(portal.get("ignore", 2)),
        },
    )
    if response.get("error") == "ok":
        return True, str(response.get("ploy_msg") or "登录成功")
    return False, str(response.get("ploy_msg") or response.get("error_msg") or response.get("error") or "登录失败")


def wifi_status() -> WifiStatus:
    """读取 Windows 报告的 WLAN 连接状态。

    ``netsh wlan connect`` 只表示系统接受了请求。恢复流程必须以这里的
    ``connected + SSID`` 为准，否则 Wi-Fi 尚在关联或 DHCP 尚未就绪时会过早
    发起门户认证。
    """
    try:
        result = subprocess.run(
            ["netsh", "wlan", "show", "interfaces"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", check=False, timeout=10,
            creationflags=NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        LOG.warning("查询 Wi-Fi 状态超时。")
        return WifiStatus(None, None, False)
    interface_name: str | None = None
    ssid: str | None = None
    state: str | None = None
    for line in result.stdout.splitlines():
        if match := re.match(r"^\s*(?:Name|名称)\s*:\s*(.*?)\s*$", line, flags=re.IGNORECASE):
            interface_name = match.group(1) or None
        elif match := re.match(r"^\s*(?:State|状态)\s*:\s*(.*?)\s*$", line, flags=re.IGNORECASE):
            state = match.group(1).strip().lower()
        elif match := re.match(r"^\s*SSID\s*:\s*(.*?)\s*$", line):
            ssid = match.group(1) or None
    # Windows 中文界面的“已连接”和英文的“connected”均能覆盖；SSID 存在是
    # 兼容其他系统语言的额外依据。
    connected = state in {"connected", "已连接"} or (ssid is not None and state is None)
    return WifiStatus(interface_name, ssid, connected)


def current_ssid() -> str | None:
    status = wifi_status()
    return status.ssid if status.connected else None


def _wlan_api() -> Any:
    """配置 Windows WLAN API 的 ctypes 签名。"""
    wlanapi = ctypes.WinDLL("wlanapi", use_last_error=True)
    wlanapi.WlanOpenHandle.argtypes = (
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.HANDLE),
    )
    wlanapi.WlanOpenHandle.restype = wintypes.DWORD
    wlanapi.WlanCloseHandle.argtypes = (wintypes.HANDLE, ctypes.c_void_p)
    wlanapi.WlanCloseHandle.restype = wintypes.DWORD
    wlanapi.WlanEnumInterfaces.argtypes = (
        wintypes.HANDLE,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.POINTER(WLAN_INTERFACE_INFO_LIST)),
    )
    wlanapi.WlanEnumInterfaces.restype = wintypes.DWORD
    wlanapi.WlanQueryInterface.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(GUID),
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.DWORD),
    )
    wlanapi.WlanQueryInterface.restype = wintypes.DWORD
    wlanapi.WlanSetInterface.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(GUID),
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
    )
    wlanapi.WlanSetInterface.restype = wintypes.DWORD
    wlanapi.WlanFreeMemory.argtypes = (ctypes.c_void_p,)
    wlanapi.WlanFreeMemory.restype = None
    return wlanapi


def enable_powered_down_wifi_radios() -> bool:
    """开启被 Windows 软件开关关闭的 Wi-Fi 无线电。

    Windows 任务栏的 Wi-Fi 开关会让 ``netsh wlan connect`` 直接返回
    "interface is powered down"。该状态下网卡仍可能显示为 Up，因此必须使用
    WLAN API 修改软件无线电状态。硬件开关/Fn 键关闭时 API 无权开启，只记录原因。
    """
    client = wintypes.HANDLE()
    interface_list: ctypes.POINTER(WLAN_INTERFACE_INFO_LIST) | None = None
    try:
        wlanapi = _wlan_api()
        negotiated_version = wintypes.DWORD()
        status = wlanapi.WlanOpenHandle(2, None, ctypes.byref(negotiated_version), ctypes.byref(client))
        if status:
            raise OSError(status, "WlanOpenHandle failed")
        list_pointer = ctypes.POINTER(WLAN_INTERFACE_INFO_LIST)()
        status = wlanapi.WlanEnumInterfaces(client, None, ctypes.byref(list_pointer))
        if status:
            raise OSError(status, "WlanEnumInterfaces failed")
        interface_list = list_pointer
        base_address = ctypes.addressof(interface_list.contents) + WLAN_INTERFACE_INFO_LIST.InterfaceInfo.offset
        enabled = False
        for index in range(interface_list.contents.dwNumberOfItems):
            info = ctypes.cast(
                base_address + index * ctypes.sizeof(WLAN_INTERFACE_INFO), ctypes.POINTER(WLAN_INTERFACE_INFO)
            ).contents
            # 任务栏 Wi-Fi 开关在不同 Windows/驱动版本中可能报告为 not_ready、
            # disconnected 或其他状态。调用方已确认当前没有连接 Wi-Fi，因此这里
            # 查询每个 WLAN 接口的实际无线电状态，不能只依赖接口状态枚举值。
            data_size = wintypes.DWORD()
            radio_data = ctypes.c_void_p()
            opcode_type = wintypes.DWORD()
            status = wlanapi.WlanQueryInterface(
                client,
                ctypes.byref(info.InterfaceGuid),
                WLAN_INTF_OPCODE_RADIO_STATE,
                None,
                ctypes.byref(data_size),
                ctypes.byref(radio_data),
                ctypes.byref(opcode_type),
            )
            if status:
                LOG.warning("无法读取 Wi-Fi 无线电状态（%s）：Windows 错误 %s。", info.strInterfaceDescription, status)
                continue
            try:
                radio_state = ctypes.cast(radio_data, ctypes.POINTER(WLAN_RADIO_STATE)).contents
                phy_count = min(int(radio_state.dwNumberOfPhys), WLAN_MAX_PHY_INDEX)
                software_off = [
                    radio_state.PhyRadioState[phy]
                    for phy in range(phy_count)
                    if radio_state.PhyRadioState[phy].dot11SoftwareRadioState == WLAN_RADIO_STATE_OFF
                ]
                if not software_off:
                    continue
                hardware_off = [
                    phy for phy in software_off if phy.dot11HardwareRadioState == WLAN_RADIO_STATE_OFF
                ]
                if hardware_off:
                    LOG.warning("Wi-Fi 硬件无线电已关闭（%s）；请使用机身开关或 Fn 键开启。", info.strInterfaceDescription)
                    continue
                for phy in software_off:
                    phy.dot11SoftwareRadioState = WLAN_RADIO_STATE_ON
                payload_size = ctypes.sizeof(wintypes.DWORD) + phy_count * ctypes.sizeof(WLAN_PHY_RADIO_STATE)
                status = wlanapi.WlanSetInterface(
                    client,
                    ctypes.byref(info.InterfaceGuid),
                    WLAN_INTF_OPCODE_RADIO_STATE,
                    payload_size,
                    ctypes.byref(radio_state),
                    None,
                )
                if status:
                    LOG.warning("无法开启 Wi-Fi 软件无线电（%s）：Windows 错误 %s。", info.strInterfaceDescription, status)
                    continue
                LOG.warning("检测到 Wi-Fi 软件无线电被关闭，已自动开启：%s。", info.strInterfaceDescription)
                enabled = True
            finally:
                if radio_data:
                    wlanapi.WlanFreeMemory(radio_data)
        return enabled
    except (AttributeError, OSError) as error:
        LOG.warning("无法检查或开启 Wi-Fi 无线电：%s", error)
        return False
    finally:
        if interface_list is not None:
            try:
                wlanapi.WlanFreeMemory(interface_list)
            except (UnboundLocalError, AttributeError):
                pass
        if client:
            try:
                wlanapi.WlanCloseHandle(client, None)
            except (UnboundLocalError, AttributeError):
                pass


def connect_wifi(ssid: str) -> bool:
    try:
        # 此命令仅调用 Windows 已保存的 Wi-Fi 配置，不需要外网连接。
        result = subprocess.run(
            ["netsh", "wlan", "connect", f"name={ssid}"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", check=False, timeout=15,
            creationflags=NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        LOG.warning("请求连接 Wi-Fi 超时。")
        return False
    if result.returncode != 0:
        LOG.warning("请求连接 Wi-Fi 失败：%s", result.stderr.strip() or result.stdout.strip())
        return False
    LOG.info("已请求连接 Wi-Fi：%s", ssid)
    return True


def disconnect_wifi() -> bool:
    """断开当前 Wi-Fi，使 Windows 重新与接入点关联。"""
    try:
        result = subprocess.run(
            ["netsh", "wlan", "disconnect"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", check=False, timeout=15,
            creationflags=NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        LOG.warning("断开 Wi-Fi 超时。")
        return False
    if result.returncode != 0:
        LOG.warning("断开 Wi-Fi 失败：%s", result.stderr.strip() or result.stdout.strip())
        return False
    LOG.info("已请求断开当前 Wi-Fi。")
    return True


def wait_for_wifi(ssid: str, wait_seconds: int) -> WifiStatus | None:
    """等待实际关联到目标 SSID；成功时返回接口名以供后续 DHCP 操作使用。"""
    deadline = time.monotonic() + max(1, wait_seconds)
    last_status = WifiStatus(None, None, False)
    while time.monotonic() < deadline:
        last_status = wifi_status()
        if last_status.connected and last_status.ssid == ssid:
            LOG.info("已确认连接 Wi-Fi：%s（接口：%s）。", ssid, last_status.interface_name or "未知")
            return last_status
        time.sleep(2)
    LOG.warning(
        "等待 Wi-Fi %s 完成关联超时（当前 SSID：%s，状态：%s）。",
        ssid,
        last_status.ssid or "无",
        "已连接" if last_status.connected else "未连接",
    )
    return None


def renew_dhcp_lease(interface_name: str | None) -> bool:
    """重新向校园网 DHCP 获取地址，不依赖外网或浏览器。"""
    if not interface_name:
        LOG.warning("无法确定 Wi-Fi 接口名称，跳过 DHCP 续租。")
        return False
    try:
        result = subprocess.run(
            ["ipconfig", "/renew", interface_name],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=35,
            creationflags=NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        LOG.warning("Wi-Fi DHCP 续租超时（接口：%s）。", interface_name)
        return False
    if result.returncode != 0:
        LOG.warning("Wi-Fi DHCP 续租失败（接口：%s）：%s", interface_name, result.stderr.strip() or result.stdout.strip())
        return False
    LOG.info("已请求 Wi-Fi DHCP 续租（接口：%s）。", interface_name)
    return True


def reset_wifi_adapter(interface_name: str | None) -> bool:
    """最后手段：禁用再启用无线接口。

    某些 Windows 策略要求管理员权限；失败时只记录并继续后续重连，不弹出 UAC
    窗口、不终止守护进程。
    """
    if not interface_name:
        LOG.warning("无法确定 Wi-Fi 接口名称，跳过无线适配器重置。")
        return False
    command_prefix = ["netsh", "interface", "set", "interface", f"name={interface_name}"]
    try:
        disabled = subprocess.run(
            [*command_prefix, "admin=DISABLED"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", check=False, timeout=15,
            creationflags=NO_WINDOW,
        )
        if disabled.returncode != 0:
            LOG.warning("无线适配器重置未获执行（接口：%s）：%s", interface_name, disabled.stderr.strip() or disabled.stdout.strip())
            return False
        LOG.warning("已禁用无线适配器，正在重新启用：%s。", interface_name)
        time.sleep(3)
        enabled = subprocess.run(
            [*command_prefix, "admin=ENABLED"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", check=False, timeout=15,
            creationflags=NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        LOG.warning("无线适配器重置超时（接口：%s）。", interface_name)
        return False
    if enabled.returncode != 0:
        LOG.warning("重新启用无线适配器失败（接口：%s）：%s", interface_name, enabled.stderr.strip() or enabled.stdout.strip())
        return False
    LOG.info("已重新启用无线适配器：%s。", interface_name)
    return True


def wifi_recovery_cooldown(config: dict[str, Any], recovery_count: int) -> int:
    """物理 Wi-Fi 恢复采用指数退避；门户认证本身仍按 retry_interval 重试。"""
    wifi = config["wifi"]
    base = max(10, int(wifi.get("wifi_reconnect_cooldown_seconds", 30)))
    maximum = max(base, int(wifi.get("max_wifi_reconnect_cooldown_seconds", 300)))
    return min(maximum, base * (2 ** max(0, recovery_count - 1)))


def internet_available(config: dict[str, Any]) -> bool:
    # 认证门户也可能返回 HTTP 200；因此必须同时核对预期的状态码或正文，不能只看请求是否成功。
    for probe in config["network_check"]["probes"]:
        try:
            with urlopen(Request(probe["url"], headers={"User-Agent": "CampusNet/1.0"}), timeout=int(config["network_check"]["timeout_seconds"])) as response:
                expected_status = probe.get("expected_status")
                expected_text = probe.get("expected_text")
                if expected_status is not None and response.status != expected_status:
                    continue
                if expected_text is not None and expected_text not in response.read().decode("utf-8", errors="replace"):
                    continue
                if expected_status is not None or expected_text is not None:
                    return True
        except (URLError, OSError, TimeoutError):
            pass
    return False


def ensure_connected(
    config: dict[str, Any],
    force_wifi_reconnect: bool = False,
    renew_dhcp: bool = False,
    reset_adapter: bool = False,
) -> ConnectionAttempt:
    ssid = str(config["wifi"]["ssid"])
    status = wifi_status()
    active_ssid = status.ssid if status.connected else None

    if force_wifi_reconnect and active_ssid == ssid:
        if reset_adapter:
            LOG.warning("校园网门户长期不可达，正在重置无线适配器后重新关联 Wi-Fi：%s。", ssid)
            reset_wifi_adapter(status.interface_name)
        else:
            LOG.warning("校园网门户连续连接失败，正在重新关联 Wi-Fi：%s。", ssid)
            disconnect_wifi()
            time.sleep(1)
        active_ssid = None

    # 未连接任何 Wi-Fi 时，直接使用 Windows 本地保存的配置重连；不等待外网探测超时。
    if active_ssid is None:
        LOG.warning("当前未连接 Wi-Fi，开始连接 %s。", ssid)
        if enable_powered_down_wifi_radios():
            # 软件无线电刚开启时，给驱动少量时间恢复扫描能力；不依赖外网。
            time.sleep(2)
        if not connect_wifi(ssid):
            return ConnectionAttempt(False)
        status = wait_for_wifi(ssid, int(config["wifi"].get("connect_wait_seconds", 20)))
        if status is None:
            return ConnectionAttempt(False)

    # 若用户正在使用其他且可正常联网的 Wi-Fi，不抢占连接；其余情况切换到 BIT-Web。
    elif active_ssid != ssid:
        if internet_available(config):
            LOG.info("当前连接 %s，网络正常。", active_ssid)
            return ConnectionAttempt(True)
        LOG.warning("当前连接 %s 但网络不可用，开始连接 %s。", active_ssid, ssid)
        if not connect_wifi(ssid):
            return ConnectionAttempt(False)
        status = wait_for_wifi(ssid, int(config["wifi"].get("connect_wait_seconds", 20)))
        if status is None:
            return ConnectionAttempt(False)

    # 在多轮“已关联但门户断开”后刷新 DHCP 租约。这对应手动断开/重连通常
    # 会触发的地址更新，且只访问本地 DHCP，不需要外网。
    if renew_dhcp:
        renew_dhcp_lease(status.interface_name)
        status = wait_for_wifi(ssid, int(config["wifi"].get("connect_wait_seconds", 20)))
        if status is None:
            return ConnectionAttempt(False)

    if internet_available(config):
        LOG.info("网络正常。")
        return ConnectionAttempt(True)

    # 已连接时只访问校园网内网门户；因此外网断开也可完成认证。
    LOG.warning("检测到网络不可用，正在进行校园网认证。")
    try:
        success, message = srun_login(config)
    except PortalTransportError as error:
        LOG.warning("校园网门户连接失败：%s", error)
        return ConnectionAttempt(False, portal_transport_failure=True)
    except PortalRequestError as error:
        LOG.warning("校园网门户请求失败：%s", error)
        return ConnectionAttempt(False)
    LOG.info("认证%s：%s", "成功" if success else "失败", message)
    if not success:
        return ConnectionAttempt(False)
    time.sleep(3)
    return ConnectionAttempt(internet_available(config))


def main() -> int:
    parser = argparse.ArgumentParser(description="BIT-Web 自动保活与登录")
    parser.add_argument("--once", action="store_true", help="仅检测并修复一次")
    args = parser.parse_args()
    try:
        if not acquire_single_instance():
            LOG.info("已有 CampusNet 实例在运行，本次启动退出。")
            return 0
        config = load_config()
        normal_interval = max(10, int(config.get("check_interval_seconds", 60)))
        retry_interval = max(1, int(config.get("retry_interval_seconds", 10)))
        reconnect_threshold = max(1, int(config["wifi"].get("reconnect_after_portal_failures", 3)))
        portal_transport_failures = 0
        wifi_recovery_count = 0
        next_wifi_recovery_at = 0.0
        while True:
            attempt_started = time.monotonic()
            force_wifi_reconnect = (
                portal_transport_failures >= reconnect_threshold and attempt_started >= next_wifi_recovery_at
            )
            recovery_number = wifi_recovery_count + 1 if force_wifi_reconnect else 0
            renew_dhcp = force_wifi_reconnect and recovery_number >= max(
                1, int(config["wifi"].get("dhcp_renew_after_wifi_recoveries", 2))
            )
            reset_adapter = force_wifi_reconnect and recovery_number >= max(
                1, int(config["wifi"].get("adapter_reset_after_wifi_recoveries", 5))
            )
            try:
                attempt = ensure_connected(
                    config,
                    force_wifi_reconnect=force_wifi_reconnect,
                    renew_dhcp=renew_dhcp,
                    reset_adapter=reset_adapter,
                )
            except Exception as error:  # 保活程序不能因一次网络错误退出
                LOG.exception("本次检测出错：%s", error)
                attempt = ConnectionAttempt(False)
            healthy = attempt.healthy
            if healthy:
                portal_transport_failures = 0
                wifi_recovery_count = 0
                next_wifi_recovery_at = 0.0
            elif attempt.portal_transport_failure:
                portal_transport_failures += 1
            if force_wifi_reconnect:
                wifi_recovery_count += 1
                cooldown = wifi_recovery_cooldown(config, wifi_recovery_count)
                next_wifi_recovery_at = time.monotonic() + cooldown
                # 已实际执行恢复动作后重新累计连续门户失败次数；在冷却期内仍
                # 每 10 秒认证，但不反复断开 Wi-Fi。
                portal_transport_failures = 0
                LOG.info("下一次 Wi-Fi 物理恢复最早将在 %s 秒后执行。", cooldown)
            if args.once:
                return 0 if healthy else 1
            # 按每次尝试的开始时间计时，避免“检测耗时 + 重试间隔”把周期拉长。
            interval = normal_interval if healthy else retry_interval
            elapsed = time.monotonic() - attempt_started
            time.sleep(max(0.0, interval - elapsed))
    except Exception as error:
        LOG.exception("程序无法启动：%s", error)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
