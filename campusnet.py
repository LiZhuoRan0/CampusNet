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


class PortalRequestError(RuntimeError):
    """校园网门户不可达或返回异常状态。"""


class PortalTransportError(PortalRequestError):
    """校园网门户连接被中断，可通过重新关联 Wi-Fi 尝试恢复。"""


@dataclass(frozen=True)
class ConnectionAttempt:
    healthy: bool
    portal_transport_failure: bool = False


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


def current_ssid() -> str | None:
    try:
        result = subprocess.run(
            ["netsh", "wlan", "show", "interfaces"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", check=False, timeout=10,
            creationflags=NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        LOG.warning("查询 Wi-Fi 状态超时。")
        return None
    for line in result.stdout.splitlines():
        match = re.match(r"^\s*SSID\s*:\s*(.*?)\s*$", line)
        if match:
            return match.group(1) or None
    return None


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


def ensure_connected(config: dict[str, Any], force_wifi_reconnect: bool = False) -> ConnectionAttempt:
    ssid = str(config["wifi"]["ssid"])
    active_ssid = current_ssid()

    if force_wifi_reconnect and active_ssid == ssid:
        LOG.warning("校园网门户连续连接失败，正在重新关联 Wi-Fi：%s。", ssid)
        disconnect_wifi()
        time.sleep(1)
        active_ssid = None

    # 未连接任何 Wi-Fi 时，直接使用 Windows 本地保存的配置重连；不等待外网探测超时。
    if active_ssid is None:
        LOG.warning("当前未连接 Wi-Fi，开始连接 %s。", ssid)
        if not connect_wifi(ssid):
            return ConnectionAttempt(False)
        deadline = time.monotonic() + int(config["wifi"].get("connect_wait_seconds", 20))
        while time.monotonic() < deadline:
            if current_ssid() == ssid:
                break
            time.sleep(2)
        if current_ssid() != ssid:
            LOG.warning("等待连接 %s 超时。", ssid)
            return ConnectionAttempt(False)

    # 若用户正在使用其他且可正常联网的 Wi-Fi，不抢占连接；其余情况切换到 BIT-Web。
    elif active_ssid != ssid:
        if internet_available(config):
            LOG.info("当前连接 %s，网络正常。", active_ssid)
            return ConnectionAttempt(True)
        LOG.warning("当前连接 %s 但网络不可用，开始连接 %s。", active_ssid, ssid)
        if not connect_wifi(ssid):
            return ConnectionAttempt(False)
        deadline = time.monotonic() + int(config["wifi"].get("connect_wait_seconds", 20))
        while time.monotonic() < deadline:
            if current_ssid() == ssid:
                break
            time.sleep(2)
        if current_ssid() != ssid:
            LOG.warning("等待连接 %s 超时。", ssid)
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
        while True:
            attempt_started = time.monotonic()
            force_wifi_reconnect = portal_transport_failures >= reconnect_threshold
            if force_wifi_reconnect:
                portal_transport_failures = 0
            try:
                attempt = ensure_connected(config, force_wifi_reconnect=force_wifi_reconnect)
            except Exception as error:  # 保活程序不能因一次网络错误退出
                LOG.exception("本次检测出错：%s", error)
                attempt = ConnectionAttempt(False)
            healthy = attempt.healthy
            if attempt.portal_transport_failure:
                portal_transport_failures += 1
            else:
                portal_transport_failures = 0
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
