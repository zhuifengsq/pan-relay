"""请求上下文工具：可信代理 IP 解析与 HTTPS 判定。

安全默认：只有 TRUSTED_PROXIES 中配置的代理地址才有资格提供
X-Forwarded-For / X-Real-IP / CF-Connecting-IP / True-Client-IP / X-Forwarded-Proto。
未配置时一律使用 request.remote_addr，避免客户端伪造请求头绕过限流、黑名单与审计日志。
"""

import ipaddress
import logging
import os
import threading
from typing import List, Optional

from flask import has_request_context, request

logger = logging.getLogger(__name__)

_IP_HEADERS = ("X-Forwarded-For", "X-Real-IP", "CF-Connecting-IP", "True-Client-IP")

_TRUSTED_LOCK = threading.Lock()
_TRUSTED_RAW: Optional[str] = None
_TRUSTED_NETWORKS: List[object] = []


def _parse_trusted_proxies(raw: str) -> List[object]:
    networks: List[object] = []
    for item in str(raw or "").split(","):
        item = item.strip()
        if not item:
            continue
        try:
            networks.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            logger.warning("忽略非法的 TRUSTED_PROXIES 条目: %s", item)
    return networks


def get_trusted_proxy_networks() -> List[object]:
    """读取 TRUSTED_PROXIES（逗号分隔 IP/CIDR），按配置原文缓存解析结果。"""
    global _TRUSTED_RAW, _TRUSTED_NETWORKS
    raw = os.environ.get("TRUSTED_PROXIES", "").strip()
    if raw == _TRUSTED_RAW:
        return _TRUSTED_NETWORKS
    with _TRUSTED_LOCK:
        if raw != _TRUSTED_RAW:
            _TRUSTED_NETWORKS = _parse_trusted_proxies(raw)
            _TRUSTED_RAW = raw
    return _TRUSTED_NETWORKS


def is_trusted_proxy(address: Optional[str]) -> bool:
    if not address:
        return False
    try:
        ip = ipaddress.ip_address(str(address).strip())
    except ValueError:
        return False
    return any(ip in network for network in get_trusted_proxy_networks())


def get_client_ip() -> str:
    """获取可信客户端 IP；仅当直连对端是受信代理时才解析转发头。"""
    if not has_request_context():
        return "127.0.0.1"

    remote = (request.remote_addr or "127.0.0.1").strip()
    if not is_trusted_proxy(remote):
        return remote

    for header in _IP_HEADERS:
        value = request.headers.get(header)
        if not value:
            continue
        for part in str(value).split(","):
            candidate = part.strip()
            if not candidate:
                continue
            try:
                ipaddress.ip_address(candidate)
            except ValueError:
                continue
            return candidate
    return remote


def request_is_secure() -> bool:
    """判断当前请求是否为 HTTPS；受信代理场景下信任 X-Forwarded-Proto。"""
    if request.is_secure:
        return True
    if is_trusted_proxy(request.remote_addr):
        proto = (request.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip().lower()
        return proto == "https"
    return False
