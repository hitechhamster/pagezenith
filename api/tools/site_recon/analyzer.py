"""站点情报侦察：确定性指纹 + 几个免费 HTTP 探针（RDAP/Wayback/products.json/sitemap）。
不需要 LLM、不需要 key。复用 seo_gap 的抓取/SSRF/config。"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx

from ..seo_gap.clients.browser_fetch import BrowserFetcher
from ..seo_gap.config import Settings, get_settings
from ..seo_gap.security import assert_safe_url
from .models import AgeInfo, ReconReport, ShopifyInfo

logger = logging.getLogger(__name__)

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# theme_store_id → 官方主题名（常见免费/付费）
THEME_STORE = {
    887: "Dawn (免费)", 1864: "Refresh (免费)", 1500: "Sense (免费)",
    1363: "Craft (免费)", 1368: "Crave (免费)", 1431: "Studio (免费)",
    1500: "Sense (免费)", 1841: "Ride (免费)", 1821: "Colorblock (免费)",
    1500: "Sense", 796: "Debut (旧·免费)", 775: "Brooklyn (旧·免费)",
    380: "Simple (旧·免费)", 730: "Supply (旧·免费)", 829: "Minimal (旧·免费)",
}


def _domain(url: str) -> str:
    return (urlparse(url).hostname or "").lower().lstrip("www.") if "://" in url else url


async def _get(client, url, **kw):
    try:
        r = await client.get(url, **kw)
        return r
    except Exception as exc:
        logger.info("probe failed %s: %s", url, exc)
        return None


# --------------------------------------------------------------------------- #
# 主页抓取（httpx 优先，拿到 headers；失败用浏览器抓 HTML）
# --------------------------------------------------------------------------- #
async def fetch_main(url: str, s: Settings):
    headers = {"User-Agent": _UA, "Accept-Language": "en,zh;q=0.8"}
    async with httpx.AsyncClient(timeout=s.fetch_timeout, follow_redirects=True, headers=headers) as c:
        r = await _get(c, url)
    if r is not None and r.status_code < 400 and len(r.text) > 200:
        return r.text, {k.lower(): v for k, v in r.headers.items()}, str(r.url)
    # 反爬 → 浏览器抓 HTML（无 headers）
    bf = BrowserFetcher(s)
    try:
        page = await bf.fetch(url)
    finally:
        await bf.aclose()
    return (page.raw_html or page.text or ""), {}, url


# --------------------------------------------------------------------------- #
# 平台指纹
# --------------------------------------------------------------------------- #
def detect_platform(html: str, headers: dict):
    h = html.lower()
    hdr = " ".join(f"{k}:{v}".lower() for k, v in headers.items())
    ev, other = [], []
    plat = ""

    def has(*subs):
        return any(x in h or x in hdr for x in subs)

    if has("cdn.shopify.com", "shopify.theme", "myshopify.com", "x-shopify", "x-shopid", "/cdn/shop/"):
        plat = "Shopify"; ev.append("命中 Shopify CDN / Shopify.theme / x-shopify 头")
    elif has("static.wixstatic.com", "x-wix", "_wixcssstate", "wix.com/"):
        plat = "Wix"; ev.append("命中 Wix 静态域/头")
    elif has("static1.squarespace.com", "squarespace.com", "this is squarespace"):
        plat = "Squarespace"; ev.append("命中 Squarespace")
    elif has("assets.website-files.com", "webflow.io", "generator\" content=\"webflow"):
        plat = "Webflow"; ev.append("命中 Webflow")
    elif has("cdn11.bigcommerce.com", "bigcommerce.com", "x-bc-"):
        plat = "BigCommerce"; ev.append("命中 BigCommerce")
    elif has("/skin/frontend/", "mage/", "magento", "static/version"):
        plat = "Magento"; ev.append("命中 Magento")
    elif has("wp-content", "wp-includes", "generator\" content=\"wordpress"):
        plat = "WordPress"; ev.append("命中 WordPress (wp-content)")
    else:
        plat = "未知 / 自建"

    if "woocommerce" in h:
        other.append("WooCommerce（WordPress 电商）")
    if "cloudflare" in hdr or "cf-ray" in hdr:
        other.append("Cloudflare CDN")
    if "x-powered-by" in headers:
        other.append("X-Powered-By: " + headers["x-powered-by"][:40])
    if "server" in headers:
        other.append("Server: " + headers["server"][:40])
    return plat, ev, other


# --------------------------------------------------------------------------- #
# Shopify 专项
# --------------------------------------------------------------------------- #
async def shopify_info(html: str, base: str, s: Settings) -> ShopifyInfo:
    info = ShopifyInfo(is_shopify=True)
    m = re.search(r"Shopify\.theme\s*=\s*(\{.*?\})\s*;", html, re.S)
    if m:
        try:
            t = json.loads(m.group(1))
            info.theme_name = str(t.get("name", ""))
            tsid = t.get("theme_store_id")
            info.theme_store_id = tsid if isinstance(tsid, int) else None
        except Exception:
            pass
    if not info.theme_store_id:
        m2 = re.search(r'"theme_store_id":\s*(\d+)', html)
        if m2:
            info.theme_store_id = int(m2.group(1))
    if info.theme_store_id:
        info.theme_known = THEME_STORE.get(info.theme_store_id, f"Theme Store 主题 (id {info.theme_store_id})")
        info.theme_paid_hint = "付费" if "免费" not in info.theme_known and info.theme_store_id not in THEME_STORE else ""
    if "shopifycloud" in html.lower() and "checkout" in html.lower():
        pass
    if "shopify_plus" in html.lower() or "plus.shopify" in html.lower():
        info.is_plus_hint = True

    # 公开的 /products.json（很多 Shopify 店开放）→ 上新/规模/价格情报
    async with httpx.AsyncClient(timeout=s.fetch_timeout, follow_redirects=True,
                                 headers={"User-Agent": _UA}) as c:
        r = await _get(c, base.rstrip("/") + "/products.json?limit=250")
    if r is not None and r.status_code == 200:
        try:
            prods = r.json().get("products", [])
        except Exception:
            prods = []
        if prods:
            info.products_count = len(prods)
            info.products_capped = len(prods) >= 250
            dates = [p.get("created_at", "") for p in prods if p.get("created_at")]
            upd = [p.get("updated_at", "") or p.get("published_at", "") for p in prods]
            if dates:
                info.earliest_product = min(dates)[:10]
            if upd:
                info.latest_product = max([u for u in upd if u])[:10]
            prices = []
            for p in prods:
                for v in p.get("variants", []):
                    try:
                        prices.append(float(v.get("price", 0)))
                    except Exception:
                        pass
            if prices:
                info.price_min, info.price_max = round(min(prices), 2), round(max(prices), 2)
            info.vendors = sorted({p.get("vendor", "") for p in prods if p.get("vendor")})[:10]
            info.product_types = sorted({p.get("product_type", "") for p in prods if p.get("product_type")})[:10]
    return info


# --------------------------------------------------------------------------- #
# 站龄：RDAP 域名注册 + Wayback 首次存档
# --------------------------------------------------------------------------- #
def _years_since(date_str: str) -> float | None:
    try:
        d = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return round((datetime.now(timezone.utc) - d).days / 365.25, 1)
    except Exception:
        return None


async def age_info(domain: str, s: Settings) -> AgeInfo:
    age = AgeInfo()
    async with httpx.AsyncClient(timeout=15, follow_redirects=True, headers={"User-Agent": _UA}) as c:
        rdap, way = await asyncio.gather(
            _get(c, f"https://rdap.org/domain/{domain}"),
            _get(c, f"http://web.archive.org/cdx/search/cdx?url={domain}&output=json&fl=timestamp&limit=1&sort=ascending&collapse=timestamp"),
        )
    if rdap is not None and rdap.status_code == 200:
        try:
            for ev in rdap.json().get("events", []):
                if ev.get("eventAction") == "registration":
                    age.domain_created = ev.get("eventDate", "")[:10]
                    age.domain_age_years = _years_since(ev.get("eventDate", ""))
        except Exception:
            pass
    if way is not None and way.status_code == 200:
        try:
            rows = way.json()
            if len(rows) > 1:
                ts = rows[1][0]  # YYYYMMDDhhmmss
                age.first_archived = f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}"
                age.first_seen_years = _years_since(f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}")
        except Exception:
            pass
    return age


# --------------------------------------------------------------------------- #
# 营销技术栈 / 像素
# --------------------------------------------------------------------------- #
def marketing(html: str):
    stack, pix = [], {}
    def grab(pat):
        m = re.search(pat, html, re.I)
        return m.group(1) if m else ""

    ga = grab(r"gtag/js\?id=(G-[A-Z0-9]+)") or grab(r"(G-[A-Z0-9]{8,})")
    if ga: pix["GA4"] = ga; stack.append("Google Analytics 4")
    ua = grab(r"(UA-\d{4,}-\d+)")
    if ua: pix["UA"] = ua; stack.append("Universal Analytics(旧)")
    gtm = grab(r"(GTM-[A-Z0-9]+)")
    if gtm: pix["GTM"] = gtm; stack.append("Google Tag Manager")
    fb = grab(r"fbq\(\s*['\"]init['\"]\s*,\s*['\"](\d{6,})") or (grab(r"connect\.facebook\.net") and "yes")
    if fb and fb != "yes": pix["Facebook Pixel"] = fb; stack.append("Facebook Pixel")
    elif "connect.facebook.net" in html: stack.append("Facebook Pixel")
    tt = grab(r"ttq\.load\(\s*['\"]([A-Z0-9]+)")
    if tt: pix["TikTok Pixel"] = tt; stack.append("TikTok Pixel")
    elif "analytics.tiktok.com" in html: stack.append("TikTok Pixel")
    for name, sub in [("Pinterest", "pintrk"), ("Klaviyo 邮件营销", "klaviyo"),
                      ("Judge.me 评价", "judge.me"), ("Loox 评价", "loox.io"),
                      ("Yotpo 评价", "yotpo"), ("Stamped 评价", "stamped.io"),
                      ("Recharge 订阅", "rechargecdn"), ("Hotjar", "hotjar"),
                      ("Microsoft Clarity", "clarity.ms"), ("Gorgias 客服", "gorgias"),
                      ("Tidio 客服", "tidio"), ("PageFly 落地页", "pagefly")]:
        if sub in html.lower(): stack.append(name)
    return sorted(set(stack)), pix


def contacts(html: str):
    emails = sorted(set(re.findall(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", html)))
    emails = [e for e in emails if not e.lower().endswith((".png", ".jpg", ".gif", ".webp"))
              and "sentry" not in e and "example.com" not in e][:10]
    socials = []
    for dom in ["facebook.com", "instagram.com", "tiktok.com", "youtube.com",
                "twitter.com", "x.com", "pinterest.com", "linkedin.com"]:
        m = re.search(r"https?://(?:www\.)?" + dom.replace(".", r"\.") + r"/[A-Za-z0-9_./\-]+", html)
        if m: socials.append(m.group(0).split("?")[0])
    return emails, sorted(set(socials))


async def seo_info(html: str, base: str, s: Settings):
    title = (re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I) or [None, ""])
    title = re.sub(r"\s+", " ", title[1]).strip() if isinstance(title, list) else ""
    desc = re.search(r'<meta[^>]+name=["\']description["\'][^>]+content=["\'](.*?)["\']', html, re.I)
    langs = sorted(set(re.findall(r'hreflang=["\']([a-zA-Z\-]+)["\']', html)))[:12]
    sitemap_urls = None
    async with httpx.AsyncClient(timeout=15, follow_redirects=True, headers={"User-Agent": _UA}) as c:
        r = await _get(c, base.rstrip("/") + "/sitemap.xml")
    if r is not None and r.status_code == 200:
        sitemap_urls = r.text.count("<loc>")
    return {"title": title, "description": (desc.group(1)[:200] if desc else ""),
            "sitemap_locs": sitemap_urls, "languages": langs}


# --------------------------------------------------------------------------- #
# 编排
# --------------------------------------------------------------------------- #
async def recon(url: str, settings: Settings | None = None) -> ReconReport:
    s = settings or get_settings()
    if s.block_private_urls:
        assert_safe_url(url)
    html, headers, final_url = await fetch_main(url, s)
    base = f"{urlparse(final_url).scheme}://{urlparse(final_url).hostname}"
    domain = _domain(final_url)

    platform, ev, other = detect_platform(html, headers)
    stack, pix = marketing(html)
    emails, socials = contacts(html)

    tasks = {"age": age_info(domain, s), "seo": seo_info(html, base, s)}
    if platform == "Shopify":
        tasks["shop"] = shopify_info(html, base, s)
    results = dict(zip(tasks.keys(), await asyncio.gather(*tasks.values())))

    rep = ReconReport(
        url=url, final_url=final_url, platform=platform, platform_evidence=ev,
        other_tech=other, age=results["age"], marketing_stack=stack, pixels=pix,
        emails=emails, socials=socials, seo=results["seo"],
    )
    if "shop" in results:
        rep.shopify = results["shop"]
    if rep.shopify.earliest_product and (not rep.age.domain_created):
        rep.notes.append("域名注册日期未取到，最早产品时间可作开店时间近似。")
    if platform == "Shopify" and any("next" in t.lower() or "react" in t.lower() for t in other):
        rep.shopify.is_plus_hint = True
        rep.notes.append("检测到前端框架（Next.js 等），疑似 Shopify 无头(headless)/Plus 架构，主题与 /products.json 可能不暴露。")
    return rep
