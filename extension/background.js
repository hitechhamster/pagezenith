"use strict";

// 后台 service worker：真正发起分析请求。这样即使用户关掉/碰没了弹窗，
// 请求仍在后台跑完，结果存进 storage，并用工具栏角标提示状态。

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg && msg.type === "analyze") {
    runAnalyze(msg.backend, msg.body);
    sendResponse({ ok: true });
  }
  return true;
});

async function runAnalyze(backend, body) {
  await chrome.storage.local.set({
    runState: "running", report: null, error: "",
    startedAt: Date.now(),
    lastInput: { target_url: body.target_url, keyword: body.keyword },
  });
  setBadge("…", "#4ba3e3");

  try {
    const resp = await fetch(backend + "/analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!resp.ok) {
      const t = await resp.text();
      throw new Error("后端 " + resp.status + "：" + t.slice(0, 400));
    }
    const report = await resp.json();
    await chrome.storage.local.set({ runState: "done", report, error: "" });
    setBadge("✓", "#5a9e6f");
  } catch (e) {
    const msg = String(e).includes("Failed to fetch")
      ? "连不上后端。请确认已运行 uvicorn，且后端地址正确。"
      : String(e);
    await chrome.storage.local.set({ runState: "error", error: msg, report: null });
    setBadge("!", "#e5534b");
  }
}

function setBadge(text, color) {
  chrome.action.setBadgeText({ text });
  chrome.action.setBadgeBackgroundColor({ color });
}
