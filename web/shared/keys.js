"use strict";
// 全站共享的 API Key 管理：存浏览器 localStorage，发请求时带上。
// key 只存在用户本机浏览器，不上传服务器存储。
(function () {
  const LS = "seo_tools_keys";
  const FIELDS = ["openrouter_key", "serpapi_key", "tavily_key", "deepseek_key", "exa_key"];
  function read() { try { return JSON.parse(localStorage.getItem(LS) || "{}"); } catch (e) { return {}; } }
  function write(o) { localStorage.setItem(LS, JSON.stringify(o)); }

  function get() {
    const k = read(), out = {};
    FIELDS.forEach(f => out[f] = k[f] || "");
    return out;
  }
  // 老工具（内容差距分析等）的门槛：OpenRouter + SerpApi。语义不要动，多个页面依赖它。
  function hasKeys() { const k = get(); return !!(k.openrouter_key && k.serpapi_key); }
  function hasTavily() { return !!get().tavily_key; }
  function has(name) { return !!get()[name.endsWith("_key") ? name : name + "_key"]; }

  function modal() {
    let m = document.getElementById("keys-modal");
    if (m) return m;
    m = document.createElement("div");
    m.id = "keys-modal";
    m.innerHTML = `
      <div class="km-bg"></div>
      <div class="km-card">
        <div class="km-h">API Key 设置</div>
        <p class="km-sub">key 只保存在你本机浏览器，请求时直接发给对应服务商，本站不存储。按需填，用哪个工具填哪个。</p>
        <label>OpenRouter API Key <a href="https://openrouter.ai/keys" target="_blank">获取</a></label>
        <input id="km-or" type="password" placeholder="sk-or-..." />
        <label>SerpApi Key <a href="https://serpapi.com/manage-api-key" target="_blank">获取</a></label>
        <input id="km-serp" type="password" placeholder="..." />
        <label>Tavily Key（解析竞品正文 / 文章生成的搜索源）<a href="https://app.tavily.com" target="_blank">获取</a></label>
        <input id="km-tavily" type="password" placeholder="tvly-..." />
        <label>DeepSeek Key（文章生成可选的 LLM，便宜）<a href="https://platform.deepseek.com/api_keys" target="_blank">获取</a></label>
        <input id="km-deepseek" type="password" placeholder="sk-..." />
        <label>Exa Key（文章生成可选的搜索源）<a href="https://dashboard.exa.ai/api-keys" target="_blank">获取</a></label>
        <input id="km-exa" type="password" placeholder="..." />
        <div class="km-btns"><button id="km-cancel" class="km-ghost">取消</button><button id="km-save">保存</button></div>
      </div>`;
    document.body.appendChild(m);
    const close = () => { m.style.display = "none"; };
    m.querySelector(".km-bg").onclick = close;
    m.querySelector("#km-cancel").onclick = close;
    m.querySelector("#km-save").onclick = () => {
      write({ openrouter_key: m.querySelector("#km-or").value.trim(),
              serpapi_key: m.querySelector("#km-serp").value.trim(),
              tavily_key: m.querySelector("#km-tavily").value.trim(),
              deepseek_key: m.querySelector("#km-deepseek").value.trim(),
              exa_key: m.querySelector("#km-exa").value.trim() });
      close();
      window.dispatchEvent(new Event("seo-keys-updated"));
    };
    return m;
  }

  function open() {
    const m = modal(); const k = get();
    m.querySelector("#km-or").value = k.openrouter_key;
    m.querySelector("#km-serp").value = k.serpapi_key;
    m.querySelector("#km-tavily").value = k.tavily_key;
    m.querySelector("#km-deepseek").value = k.deepseek_key;
    m.querySelector("#km-exa").value = k.exa_key;
    m.style.display = "block";
  }

  window.SEOKEYS = { get, hasKeys, hasTavily, has, open };
})();
