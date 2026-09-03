"""账户 API（前缀 /api/auth）。机制见 accounts.py 顶部。

会话走 HttpOnly cookie —— 前端 JS 读不到 token，减少 XSS 拿走会话的面。
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, Response
from pydantic import BaseModel

from . import accounts, mailer, store
from .deps import SESSION_COOKIE, _client_ip, _rate_limit_bad_key
from tools.seo_gap.config import get_settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auth", tags=["auth"])

COOKIE = dict(httponly=True, samesite="lax", max_age=accounts.SESSION_TTL, path="/")


class Creds(BaseModel):
    email: str
    password: str


class SignupReq(BaseModel):
    email: str
    password: str
    password2: str = ""      # 二次确认；前端也校验，这里是最后一道


def _issue(request: Request, response: Response, out: dict) -> dict:
    # 站点在 nginx 反代后面，uvicorn 看到的 scheme 是 http —— 只看 request.url.scheme
    # 会让生产环境的会话 cookie 一直没有 Secure 标志。三个信号任一成立就打上。
    secure = (request.url.scheme == "https"
              or request.headers.get("x-forwarded-proto", "").lower() == "https"
              or get_settings().site_url.startswith("https://"))
    response.set_cookie(SESSION_COOKIE, out["token"], secure=secure, **COOKIE)
    return {"id": out["id"], "email": out["email"]}


@router.post("/register")
async def register(req: SignupReq, request: Request, response: Response,
                   bg: BackgroundTasks):
    if req.password2 and req.password != req.password2:
        raise HTTPException(status_code=400, detail="两次输入的密码不一样。")
    try:
        out = accounts.register(req.email, req.password, _client_ip(request))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    logger.info("新账户 #%s %s", out["id"], out["email"])
    # 后台发信：发不出去也不能挡住注册
    bg.add_task(mailer.send_welcome, out["email"], 0)
    return _issue(request, response, out)


@router.post("/login")
async def login(req: Creds, request: Request, response: Response):
    ip = _client_ip(request)
    try:
        out = accounts.login(req.email, req.password, ip)
    except ValueError as exc:
        _rate_limit_bad_key(ip)          # 撞密码跟撞卡密走同一个限流
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    return _issue(request, response, out)


@router.post("/logout")
async def logout(request: Request, response: Response):
    accounts.logout(request.cookies.get(SESSION_COOKIE, ""))
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@router.get("/me")
async def me(request: Request):
    """当前登录状态 + 余额。未登录返回 {"account": null}，不报错 —— 前端拿它决定显示什么。"""
    acct = accounts.account_by_token(request.cookies.get(SESSION_COOKIE, ""))
    if acct is None:
        return {"account": None}
    st = store.card_state(acct["wallet_hash"]) or {}
    return {"account": {"id": acct["id"], "email": acct["email"]},
            "balance": {"total": st.get("total", 0), "used": st.get("used", 0),
                        "remaining": st.get("remaining", 0),
                        "spent_today": store.card_spent_today(acct["wallet_hash"])}}


class PwReq(BaseModel):
    old_password: str
    new_password: str


@router.post("/password")
async def change_password(req: PwReq, request: Request, response: Response):
    acct = accounts.account_by_token(request.cookies.get(SESSION_COOKIE, ""))
    if acct is None:
        raise HTTPException(status_code=401, detail="请先登录。")
    try:
        accounts.change_password(acct["id"], req.old_password, req.new_password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # 改密码会踢掉所有会话（包括当前这个），让前端知道要重新登录
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True, "relogin": True}


class RedeemReq(BaseModel):
    card_key: str


@router.post("/redeem")
async def redeem(req: RedeemReq, request: Request):
    """把一张不记名卡密兑进当前账户。老用户手里的卡靠这个继续有效。"""
    acct = accounts.account_by_token(request.cookies.get(SESSION_COOKIE, ""))
    if acct is None:
        raise HTTPException(status_code=401, detail="请先登录再兑换。")
    try:
        got = accounts.redeem_card(acct["id"], req.card_key)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    logger.info("账户 #%s 兑换卡密 +%s 点", acct["id"], got)
    return {"ok": True, "credits": got}


# ── 忘记密码 ────────────────────────────────────────────────────────
class ForgotReq(BaseModel):
    email: str


@router.post("/forgot")
async def forgot(req: ForgotReq, request: Request, bg: BackgroundTasks):
    """申请重置链接。

    ⚠️ 无论邮箱存不存在都返回同一句话 —— 否则这就是个枚举注册用户的接口。
    """
    ip = _client_ip(request)
    _rate_limit_bad_key(ip)          # 跟撞密码共用限流，防有人拿它刷邮件
    out = accounts.start_reset(req.email)
    if out is not None:
        token, email = out
        link = f"{get_settings().site_url}/reset?token={token}"
        bg.add_task(mailer.send_reset, email, link, accounts.RESET_TTL // 60)
        logger.info("重置链接已发往 %s", email)
    return {"ok": True,
            "message": "如果这个邮箱注册过，重置链接已经发出去了，30 分钟内有效。"}


class ResetReq(BaseModel):
    token: str
    password: str
    password2: str = ""


@router.post("/reset")
async def reset(req: ResetReq, response: Response):
    if req.password2 and req.password != req.password2:
        raise HTTPException(status_code=400, detail="两次输入的密码不一样。")
    try:
        accounts.finish_reset(req.token, req.password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True, "message": "密码已重设，请用新密码登录。"}
