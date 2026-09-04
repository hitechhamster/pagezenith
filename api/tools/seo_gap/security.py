"""SSRF 防护：禁止服务器去抓取私有/内网/回环/云元数据地址。

公开服务必须有——否则有人会让你的后端去抓 http://169.254.169.254（云元数据，偷密钥）
或内网地址打内网。这里做主机名解析 + IP 段判断。

## 2026-09-04 补的两个洞（都是实测打通过的）

1. **阿里云元数据 `100.100.100.200` 漏网。** 它落在 RFC 6598 的 100.64.0.0/10
   （运营商级 NAT 共享地址段），而 Python 的 `is_private` / `is_reserved` 对这一段
   全返回 False —— 靠属性判断是抓不到它的，必须显式列网段。实测当时能读到
   `/latest/meta-data/` 和 `/latest/user-data`。

2. **只校验首个 URL，`follow_redirects=True` 一跳就绕过。** 攻击者挂一个公网地址
   302 到 169.254.169.254 即可，前置检查完全无效。修法是把校验挂到 httpx 的
   request event hook 上 —— 重定向产生的每一次请求都会再过一遍（见 SAFE_HOOKS）。

## 已知残留风险

**DNS rebinding 没挡住**：这里解析一次做判断，httpx 连接时会再解析一次，中间可以翻脸。
彻底修需要把校验通过的 IP 钉住再带 Host 头去连。当前靠"所有入口都要求登录 + 全局成本
熔断"兜底；哪天这个端点要重新对匿名开放，必须先补这条。
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse


class UnsafeURLError(ValueError):
    pass


_BLOCKED_HOSTS = {
    "localhost",
    "metadata.google.internal",
    "metadata.goog",
    "instance-data",          # 部分云厂商的元数据别名
}

# 属性判断（is_private 等）抓不到、但必须挡的网段
_BLOCKED_NETS = (
    ipaddress.ip_network("100.64.0.0/10"),      # RFC6598 CGNAT —— 阿里云元数据 100.100.100.200 在这
    ipaddress.ip_network("fd00::/8"),           # IPv6 唯一本地地址
    ipaddress.ip_network("::ffff:0:0/96"),      # IPv4-mapped IPv6，绕过用
)


def _ip_blocked(ip: ipaddress._BaseAddress) -> bool:
    if (ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
        return True
    # IPv4-mapped IPv6（::ffff:127.0.0.1）要拆出来再判一次，否则属性判断看不出内网
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None and _ip_blocked(mapped):
        return True
    return any(ip in net for net in _BLOCKED_NETS if ip.version == net.version)


def assert_safe_url(url: str) -> None:
    """不安全则抛 UnsafeURLError。"""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise UnsafeURLError(f"仅允许 http/https：{url}")
    host = parsed.hostname or ""
    if not host or host.lower() in _BLOCKED_HOSTS:
        raise UnsafeURLError(f"禁止的主机：{host}")
    # 解析所有 IP，任一落在私有/保留/元数据段即拒绝
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise UnsafeURLError(f"域名无法解析：{host}") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if _ip_blocked(ip):
            raise UnsafeURLError(f"禁止访问内网/私有地址：{host} → {ip}")


async def _guard_request(request) -> None:
    """httpx request event hook：每一次实际发出的请求都过一遍校验。

    关键在于**重定向产生的后续请求也会触发这个 hook**，所以 follow_redirects=True
    不再是绕过手段。挂法见下面的 SAFE_HOOKS。
    """
    assert_safe_url(str(request.url))


# 直接塞给 httpx.AsyncClient(event_hooks=SAFE_HOOKS)。
# 凡是抓「用户给的 URL」的客户端都应该带上它。
SAFE_HOOKS = {"request": [_guard_request]}
