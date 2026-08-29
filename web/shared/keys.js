"use strict";
/* 身份与余额（2026-08-29：账户体系取代卡密，卡密降级成充值券）。
 *
 * 设计要点：
 * 1. **账户即身份**：会话走 HttpOnly cookie，前端读不到也不需要读。
 *    本机 localStorage 里的卡号只作兼容路径保留，主流程用不到。
 *    不需要注册登录；换台电脑输入同一张卡，余额和历史记录都在。
 * 2. **fetch 挂钩**：拦截所有 /api/ 请求自动加头 —— 六个工具页原有的 fetch
 *    代码一行都不用改。401/402/429 也在这里统一提示，省得每页各写一套。
 * 3. 文件名保留 keys.js、全局仍叫 SEOKEYS：老页面的 <script src> 和
 *    SEOKEYS.open()/hasKeys() 调用继续可用（open 现在是跳登录页）。
 */
(function () {
  const LS = "pz_card_key";
  let balance = null;                 // {remaining,total,used} 或 null

  const read = () => (localStorage.getItem(LS) || "").trim();
  const write = (v) => localStorage.setItem(LS, (v || "").trim().toUpperCase());

  /* ---------- 请求挂钩：自动带卡密 ---------- */
  const rawFetch = window.fetch.bind(window);
  window.fetch = async function (input, init) {
    const url = typeof input === "string" ? input : (input && input.url) || "";
    const isApi = url.startsWith("/api/") || url.includes("//") === false && url.startsWith("api/");
    if (isApi) {
      init = init || {};
      const h = new Headers(init.headers || (typeof input !== "string" ? input.headers : undefined) || {});
      const key = read();
      if (key) h.set("X-Card-Key", key);
      init.headers = h;
    }
    const resp = await rawFetch(input, init);
    if (isApi && (resp.status === 401 || resp.status === 402)) {
      // 身份失效 / 点数不足：统一提示 + 送去登录，免得每个工具页各写一遍。
      // ⚠️ 两类接口必须排除，它们的 401 有别的含义、也各自处理：
      //   /api/auth/* —— 登录密码错也是 401，在登录页跳登录页会死循环
      //   /api/pay/*  —— 店主页口令错是 401，跳走会把店主自己踢出 /payadmin
      const selfHandled = url.includes("/api/auth/") || url.includes("/api/pay/");
      if (!selfHandled) {
        resp.clone().json().then(j => toast(j.detail || "请先登录", true)).catch(() => {});
        if (resp.status === 401) {
          setTimeout(() => {
            location.href = "/login?next=" +
              encodeURIComponent(location.pathname + location.search);
          }, 1200);           // 留一点时间让用户看见提示，别闪一下就跳走
        }
        refresh();
      }
    } else if (isApi && resp.ok) {
      // 花过点的请求顺手刷一下余额（成本可忽略：一个 GET）
      if ((init && init.method === "POST") || (typeof input !== "string" && input.method === "POST")) {
        setTimeout(refresh, 800);
      }
    }
    return resp;
  };

  /* ---------- 身份与余额 ----------
     账户是唯一的主路径。localStorage 里的卡号只在极老的浏览器会话里可能残留，
     服务端仍认它（require_card 的兼容分支），但界面不再引导任何人去用。 */
  let account = null;                // {id,email} 或 null

  async function refresh() {
    // 先问账户；服务端认的是 HttpOnly cookie，前端读不到也不需要读
    try {
      const r = await rawFetch("/api/auth/me");
      const j = r.ok ? await r.json() : null;
      account = j && j.account ? j.account : null;
      if (account) {
        balance = j.balance || null;
        paintBadge();
        window.dispatchEvent(new CustomEvent("pz-card-updated", { detail: balance }));
        return balance;
      }
    } catch (e) { account = null; }

    if (!read()) { balance = null; paintBadge(); return null; }
    try {
      const r = await rawFetch("/api/billing/balance", { headers: { "X-Card-Key": read() } });
      balance = r.ok ? await r.json() : null;
    } catch (e) { balance = null; }
    paintBadge();
    window.dispatchEvent(new CustomEvent("pz-card-updated", { detail: balance }));
    return balance;
  }

  function badgeText() {
    if (account) return balance ? `${balance.remaining} 点` : "已登录";
    if (!read())  return "登录";
    return balance ? `余额 ${balance.remaining} 点` : "身份已失效";
  }

  function paintBadge() {
    const bad = !balance && (account || read());
    document.querySelectorAll("[data-card-badge]").forEach(el => {
      el.classList.toggle("warn", !!bad);
      el.textContent = badgeText();
    });
    const b = document.getElementById("keybtn");
    if (b) { b.classList.toggle("warn", !!bad); b.textContent = badgeText(); }
  }

  /* 点徽标：登录了进「我的记录」看账，没登录去登录页 */
  function onBadgeClick() {
    if (account) { location.href = "/history"; return; }
    location.href = "/login?next=" + encodeURIComponent(location.pathname + location.search);
  }

  async function signOut() {
    try { await rawFetch("/api/auth/logout", { method: "POST" }); } catch (e) {}
    account = null; balance = null; paintBadge();
    location.href = "/";
  }

  /* 卡密弹窗已删除（2026-08-29 只留登录充值）。
     open() 保留为跳登录页 —— 老调用点不用改，语义变成"去拿一个身份"。
     兑换充值券的入口在「我的记录」页，不再全局弹窗。 */
  function openModal() {
    location.href = "/login?next=" + encodeURIComponent(location.pathname + location.search);
  }


  /* ---------- 轻提示 ---------- */
  function toast(text, isErr) {
    let t = document.getElementById("pz-toast");
    if (!t) {
      t = document.createElement("div");
      t.id = "pz-toast";
      document.body.append(t);
    }
    t.textContent = text;
    t.className = isErr ? "err show" : "show";
    clearTimeout(t._h);
    t._h = setTimeout(() => (t.className = ""), 3600);
  }

  /* ---------- 对外接口（保留旧名，语义已换）---------- */
  window.CARD = {
    get: read,
    set: write,
    // has() = "有没有一个能扣点的身份"。登录了就算有 —— 工具页拿它决定
    // 能不能提交，只看本地卡密会把登录用户挡在外面。
    has: () => !!account || !!read(),
    headers: () => (read() ? { "X-Card-Key": read() } : {}),
    balance: () => balance,
    account: () => account,
    signOut,
    onBadgeClick,
    refresh,
    open: openModal,
    toast,
  };

  // 老页面兼容层：不再有任何 API key，get() 返回空对象即可
  window.SEOKEYS = {
    get: () => ({}),
    hasKeys: () => !!read(),
    has: () => !!read(),
    hasTavily: () => false,
    open: openModal,
    refresh,
  };

  document.addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll("[data-card-open], #keybtn").forEach(el => (el.onclick = openModal));
    paintBadge();
    if (read()) refresh();
  });
})();
