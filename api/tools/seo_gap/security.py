"""SSRF 防护：禁止服务器去抓取私有/内网/回环/云元数据地址。

公开服务必须有——否则有人会让你的后端去抓 http://169.254.169.254（云元数据，偷密钥）
或内网地址打内网。这里做主机名解析 + IP 段判断。
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse


class UnsafeURLError(ValueError):
    pass


_BLOCKED_HOSTS = {"localhost", "metadata.google.internal"}


def assert_safe_url(url: str) -> None:
    """不安全则抛 UnsafeURLError。"""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise UnsafeURLError(f"仅允许 http/https：{url}")
    host = parsed.hostname or ""
    if not host or host.lower() in _BLOCKED_HOSTS:
        raise UnsafeURLError(f"禁止的主机：{host}")
    # 解析所有 IP，任一落在私有/保留段即拒绝
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise UnsafeURLError(f"域名无法解析：{host}") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            raise UnsafeURLError(f"禁止访问内网/私有地址：{host} → {ip}")
