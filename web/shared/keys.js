"use strict";
/* 卡密管理（2026-08 取代原来的「用户自带 API Key」）。
 *
 * 设计要点：
 * 1. **卡密即身份**：卡号存本机 localStorage，每次请求自动带 X-Card-Key 头。
 *    不需要注册登录；换台电脑输入同一张卡，余额和历史记录都在。
 * 2. **fetch 挂钩**：拦截所有 /api/ 请求自动加头 —— 六个工具页原有的 fetch
 *    代码一行都不用改。401/402/429 也在这里统一提示，省得每页各写一套。
 * 3. 文件名保留 keys.js、全局仍叫 SEOKEYS：老页面的 <script src> 和
 *    SEOKEYS.open()/hasKeys() 调用继续可用（内部语义已换成卡密）。
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
      // 卡密无效 / 点数不足：统一弹窗，避免每个工具页各写一遍提示
      resp.clone().json().then(j => toast(j.detail || "请检查卡密", true)).catch(() => {});
      if (resp.status === 401) openModal();
      refresh();
    } else if (isApi && resp.ok) {
      // 花过点的请求顺手刷一下余额（成本可忽略：一个 GET）
      if ((init && init.method === "POST") || (typeof input !== "string" && input.method === "POST")) {
        setTimeout(refresh, 800);
      }
    }
    return resp;
  };

  /* ---------- 身份与余额 ----------
     两种身份：登录账户（主）与裸卡密（兼容旧用户 / 未注册直接用卡）。
     账户优先 —— 登录着就走账户余额，不再看本地存的卡密。 */
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
    if (!read())  return "登录 / 卡密";
    return balance ? `余额 ${balance.remaining} 点` : "卡密无效";
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

  /* 点徽标：登录了进「我的记录」看账，没登录去登录页（卡密入口在那页下面） */
  function onBadgeClick() {
    if (account) { location.href = "/history"; return; }
    location.href = "/login?next=" + encodeURIComponent(location.pathname + location.search);
  }

  async function signOut() {
    try { await rawFetch("/api/auth/logout", { method: "POST" }); } catch (e) {}
    account = null; balance = null; paintBadge();
    location.href = "/";
  }

  /* ---------- 弹窗 ---------- */
  function modal() {
    let m = document.getElementById("card-modal");
    if (m) return m;
    m = document.createElement("div");
    m.id = "card-modal";
    m.innerHTML = `
      <div class="km-bg"></div>
      <div class="km-card">
        <div class="km-h">输入卡密</div>
        <p class="km-sub">卡密即身份：余额、生成记录都跟着这张卡走，换设备输入同一张卡即可。
          卡密只保存在你本机浏览器。</p>
        <label>卡密</label>
        <input id="card-input" placeholder="PZ-XXXX-XXXX-XXXX" autocomplete="off" spellcheck="false" />
        <div id="card-msg" class="km-msg"></div>
        <div class="km-actions">
          <a class="km-buy" href="/#pricing">还没有卡密？看套餐 →</a>
          <span style="flex:1"></span>
          <button id="card-cancel" class="btn ghost">取消</button>
          <button id="card-save" class="btn">保存</button>
        </div>
      </div>`;
    document.body.append(m);
    m.querySelector(".km-bg").onclick = () => (m.style.display = "none");
    m.querySelector("#card-cancel").onclick = () => (m.style.display = "none");
    m.querySelector("#card-save").onclick = async () => {
      const v = m.querySelector("#card-input").value.trim();
      const msg = m.querySelector("#card-msg");
      if (!v) { msg.textContent = "请输入卡密。"; return; }
      write(v);
      msg.textContent = "校验中…";
      const b = await refresh();
      if (b) {
        msg.textContent = "";
        m.style.display = "none";
        toast(`卡密已保存，余额 ${b.remaining} 点`);
      } else {
        msg.textContent = "卡密无效或已停用，请核对后重试。";
      }
    };
    m.querySelector("#card-input").addEventListener("keydown", e => {
      if (e.key === "Enter") m.querySelector("#card-save").click();
    });
    return m;
  }

  function openModal() {
    const m = modal();
    m.querySelector("#card-input").value = read();
    m.querySelector("#card-msg").textContent = "";
    m.style.display = "block";
    setTimeout(() => m.querySelector("#card-input").focus(), 30);
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
