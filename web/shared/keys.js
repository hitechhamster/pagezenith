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
        } else {
          // 402 = 点数不足。以前只弹一个 3.6 秒就消失的 toast 就没了下文：用户卡在
          // "点数不足"四个字上，页面上没有任何一处告诉他去哪充值（导航里那个"充值"
          // 和旁边两个胶囊长得一样，手机上还在屏幕外）。既然点数不足是"必须充值才能
          // 继续"的死结，就直接送到充值页，跟 401 送去登录同一个道理。
          setTimeout(() => { location.href = "/buy"; }, 1800);
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
  let ready = null;                  // 首次 refresh 的 Promise；未完成前身份是「未知」不是「未登录」

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
    paintAuthNav();
  }

  /* 点徽标：登录了进「我的记录」看账，没登录去登录页。

     ⚠️ 必须先 await ready —— 页面刚加载时 refresh() 还没回来，account 是 null，
     那是"还不知道"不是"没登录"。直接当没登录处理会把已登录的用户送去登录页，
     登录页发现他已登录又弹回来，看起来就是"点登录被踢回首页"。2026-08-29 实测复现过。 */
  async function onBadgeClick() {
    try { await ready; } catch (e) {}
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

  /* ---------- 点数徽标 ----------
     页面上任何 <span data-cost="工具:动作"> 都会被填成「本次消耗 N 点」。
     数字从 /api/billing/pricing 实时取，不写死在各页面里 —— 首页价目表就吃过
     写死的亏（页面上的数和后端对不上）。取不到就保持空白，不显示错的数字。 */
  async function paintCosts() {
    const nodes = document.querySelectorAll("[data-cost]");
    if (!nodes.length) return;
    let prices;
    try {
      const j = await (await rawFetch("/api/billing/pricing")).json();
      prices = j.prices || [];
    } catch (e) { return; }
    nodes.forEach(el => {
      const [tool, action] = (el.getAttribute("data-cost") || "").split(":");
      const row = prices.find(p => p.tool === tool && p.action === action);
      if (!row) return;
      el.textContent = `本次消耗 ${row.credits} 点`;
      el.classList.add("ready");
    });
  }

  /* 注册赠送额度：同样从接口取，别再手抄。
     首页曾把它写死成"注册送 9 点"，后端改成 11 之后前端没跟着动。 */
  async function paintSignupCredits() {
    const nodes = document.querySelectorAll("[data-signup-credits]");
    if (!nodes.length) return;
    try {
      const j = await (await rawFetch("/api/billing/pricing")).json();
      if (!j.signup_credits) return;
      nodes.forEach(el => { el.textContent = j.signup_credits; });
    } catch (e) { /* 取不到就保留占位符里的默认值 */ }
  }

  /* 按登录状态切换导航：未登录露「注册」，登录后露「充值 / 我的记录」。
     以前导航只有「充值 / 我的记录 / 登录」——陌生人看到"登录"会以为这里没他的事，
     注册入口只藏在登录页一行小字里。 */
  function paintAuthNav() {
    const known = account !== null || !!read();
    document.querySelectorAll("[data-when=anon]").forEach(el => {
      el.style.display = known ? "none" : "";
    });
    document.querySelectorAll("[data-when=user]").forEach(el => {
      el.style.display = known ? "" : "none";
    });
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
    paintCosts,
    paintAuthNav,
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
    // ⚠️ 别再把 onclick 覆盖成 openModal —— 页面里写的是 CARD.onBadgeClick()，
    //    覆盖掉等于我改的行为全不生效（2026-08-29 踩过）。只给没写 onclick 的兜底。
    document.querySelectorAll("[data-card-open], #keybtn").forEach(el => {
      if (!el.getAttribute("onclick")) el.onclick = onBadgeClick;
    });
    paintBadge();
    // ⚠️ 无条件刷新。原来写的是 if (read()) —— 只有本地存了卡密才查身份，
    //    于是账户用户的 account 永远是 null、徽标永远显示"登录"、点了就去登录页。
    //    这是「已登录却被弹回首页」的真正根因。
    ready = refresh();
    paintCosts();
    paintSignupCredits();
    paintAuthNav();
  });
})();
