"""发信（Resend）。

## 为什么必须有

预付费产品最怕的不是没人买，是**买了之后进不去账户**。没有自助找回，
用户忘了密码就只能来找站长，而他手里还有已经付过钱的点数 —— 这对信任
是硬伤，量一起来也扛不住。

## 设计

- 没配 RESEND_API_KEY 时**不报错，只记日志**。发信失败绝不能拖垮注册和重置流程 ——
  用户不该因为我们的邮件服务挂了而注册不了。
- 走代理：香港服务器直连 Resend 没问题，但统一用 proxy_for() 判断，
  将来换机器不用改这里。
- 正文用纯文本 + 极简 HTML。这类信只有一个目的：让人点那个链接。
"""

from __future__ import annotations

import logging
from typing import Optional

import httpx

from tools.seo_gap.config import get_settings

logger = logging.getLogger(__name__)
API = "https://api.resend.com/emails"


def _wrap(title: str, body_html: str) -> str:
    """极简模板。不放图、不放追踪像素 —— 这类信越简单越像"正经系统信"。"""
    return f"""<div style="font-family:-apple-system,'Segoe UI','PingFang SC',sans-serif;
      max-width:520px;margin:0 auto;padding:24px;color:#141414;line-height:1.75">
  <div style="font-weight:700;font-size:18px;margin-bottom:18px">页面科技</div>
  <div style="font-size:19px;font-weight:600;margin-bottom:12px">{title}</div>
  {body_html}
  <div style="margin-top:26px;padding-top:14px;border-top:1px solid #DEDED8;
     font-size:12px;color:#98988F">
    这封信由页面科技自动发送。如果不是你操作的，忽略即可，你的账户不受影响。
  </div>
</div>"""


def send(to: str, subject: str, title: str, body_html: str, text: str) -> bool:
    """发一封信。返回是否发出去了；失败只记日志，不抛异常。"""
    s = get_settings()
    if not s.resend_api_key:
        logger.warning("未配置 RESEND_API_KEY，跳过发信：%s → %s", subject, to)
        return False
    try:
        with httpx.Client(timeout=20, proxy=s.proxy_for("resend"), trust_env=False) as c:
            r = c.post(API,
                       headers={"Authorization": f"Bearer {s.resend_api_key}"},
                       json={"from": s.mail_from, "to": [to], "subject": subject,
                             "html": _wrap(title, body_html), "text": text})
        if r.status_code >= 300:
            logger.error("发信失败 %s：%s %s", to, r.status_code, r.text[:200])
            return False
        return True
    except Exception as exc:  # noqa: BLE001  发信挂了不能拖垮注册/重置
        logger.error("发信异常 %s：%s", to, str(exc)[:200])
        return False


def send_reset(to: str, link: str, minutes: int) -> bool:
    return send(
        to, "重设你的页面科技密码", "重设密码",
        f'''<p>点下面的按钮设置新密码，链接 {minutes} 分钟内有效：</p>
        <p style="margin:20px 0">
          <a href="{link}" style="display:inline-block;background:#141414;color:#fff;
             text-decoration:none;padding:12px 26px;border-radius:999px;font-weight:600">
            设置新密码</a></p>
        <p style="font-size:13px;color:#63635E">按钮打不开就复制这个地址：<br />
          <span style="word-break:break-all">{link}</span></p>
        <p style="font-size:13px;color:#63635E">改密码会让所有已登录的设备退出。</p>''',
        f"重设你的页面科技密码（{minutes} 分钟内有效）：\n{link}\n\n"
        f"不是你操作的话忽略即可。")


def send_welcome(to: str, credits: int = 0) -> bool:
    # 注册即送的点数正好够写完整一篇，这里就直说"够写一篇"，比光给个点数好懂。
    # （"全套 10 点"是旧文案，实际是 9 点：大纲 2 + 正文 5 + 润色 2。）
    from .pricing import full_article_credits
    full = full_article_credits()
    extra = (f"<p>账户里已有 <b>{credits} 点</b>"
             + (f"，正好够完整写一篇（全套 {full} 点）。</p>" if credits >= full else "，可以直接开始。</p>")
             if credits else
             f"<p>点数用完即充，不订阅、不绑卡。一篇文章全套 {full} 点。</p>")
    return send(
        to, "页面科技账户已开通", "账户开通了",
        f'''{extra}
        <p style="margin:20px 0">
          <a href="https://pagezenith.com/tools/seo-writer"
             style="display:inline-block;background:#141414;color:#fff;text-decoration:none;
             padding:12px 26px;border-radius:999px;font-weight:600">开始写第一篇</a></p>
        <p style="font-size:13px;color:#63635E">
          这封信也是你的账户凭证 —— 忘了密码可以用这个邮箱自助重设。</p>''',
        f"页面科技账户已开通（{to}）。\nhttps://pagezenith.com/tools/seo-writer")
