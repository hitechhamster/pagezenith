"""从网页 HTML 抓联系邮箱 + 找 contact/about 页 + 判是否只有联系表单。"""

from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

from selectolax.parser import HTMLParser

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

# 明显不是真人联系邮箱的噪声
_NOISE = (
    "sentry", "wixpress", "example.com", "example.org", "yourdomain", "your@email",
    "email@", "domain.com", "@2x", ".png", ".jpg", ".jpeg", ".gif", ".webp",
    "godaddy", "cloudflare", "u003e", "protection",
)
# 角色前缀优先级（越靠前越像可外联的编辑/合作邮箱）
_ROLE_ORDER = ("editor", "editorial", "contribute", "guest", "press", "pr@",
               "partnership", "partner", "outreach", "marketing", "hello", "contact",
               "team", "hi@", "info", "admin", "support")

_CONTACT_HINTS = ("contact", "about", "team", "write-for-us", "write_for_us",
                  "contribute", "advertise", "connect")


def extract_emails(html: str) -> list[str]:
    """从 HTML 抽邮箱：mailto: + 正文正则，去噪去重。"""
    if not html:
        return []
    found: list[str] = []
    tree = HTMLParser(html)
    for a in tree.css('a[href^="mailto:"]'):
        href = a.attributes.get("href", "") or ""
        addr = href[7:].split("?", 1)[0].strip()
        if addr:
            found.append(addr)
    for m in _EMAIL_RE.findall(html):
        found.append(m)

    out, seen = [], set()
    for e in found:
        e = e.strip().strip(".").lower()
        low = e.lower()
        if not e or e in seen:
            continue
        if any(n in low for n in _NOISE):
            continue
        if len(e) > 70:
            continue
        seen.add(e)
        out.append(e)
    return _rank(out)


def _rank(emails: list[str]) -> list[str]:
    def key(e: str):
        for i, role in enumerate(_ROLE_ORDER):
            if e.startswith(role) or ("@" + role) in e:
                return i
        return len(_ROLE_ORDER)
    return sorted(emails, key=key)


def find_contact_links(html: str, base_url: str) -> list[str]:
    """从首页找 contact/about 类链接，返回绝对 URL（同域，去重，最多 3 个）。"""
    if not html:
        return []
    base_host = urlparse(base_url).netloc.replace("www.", "")
    tree = HTMLParser(html)
    out, seen = [], set()
    for a in tree.css("a[href]"):
        href = (a.attributes.get("href") or "").strip()
        if not href or href.startswith(("mailto:", "tel:", "#", "javascript:")):
            continue
        text = (a.text() or "").lower()
        hay = (href.lower() + " " + text)
        if not any(h in hay for h in _CONTACT_HINTS):
            continue
        absu = urljoin(base_url, href)
        if urlparse(absu).netloc.replace("www.", "") != base_host:
            continue
        key = absu.split("#", 1)[0].rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        out.append(absu)
        if len(out) >= 3:
            break
    return out


def has_contact_form(html: str) -> bool:
    if not html:
        return False
    low = html.lower()
    return ("<form" in low and ("email" in low or "message" in low or "contact" in low))
