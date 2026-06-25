"use strict";

const REGION = {
  "us-en": { location_code: 2840, language_code: "en" },
  "cn-zh": { location_code: 2156, language_code: "zh" },
  "hk-zh": { location_code: 2344, language_code: "zh" },
  "uk-en": { location_code: 2826, language_code: "en" },
};

const $ = (id) => document.getElementById(id);
let CURRENT_TAB = null;

async function init() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  CURRENT_TAB = tab;
  $("url").value = (tab && tab.url) || "";

  const saved = await chrome.storage.local.get(
    ["keyword", "region", "backend", "useBrowser", "runState", "report", "error"]
  );
  if (saved.keyword) $("keyword").value = saved.keyword;
  if (saved.region) $("region").value = saved.region;
  if (saved.backend) $("backend").value = saved.backend;
  if (saved.useBrowser) $("browser").checked = true;

  // 恢复上次/进行中的状态（弹窗被碰没后重开也能看到结果）
  applyState(saved.runState, saved.report, saved.error);

  // 打开弹窗即清角标
  chrome.action.setBadgeText({ text: "" });
}

// 后台状态变化时实时刷新弹窗
chrome.storage.onChanged.addListener((changes, area) => {
  if (area !== "local") return;
  if (changes.runState || changes.report || changes.error) {
    chrome.storage.local.get(["runState", "report", "error"]).then((s) =>
      applyState(s.runState, s.report, s.error)
    );
  }
});

function applyState(runState, report, error) {
  if (runState === "running") {
    $("run").disabled = true;
    setStatus("分析进行中…（可关闭此弹窗，完成后工具栏角标会显示 ✓，重开即见结果）");
    $("report").innerHTML = "";
  } else if (runState === "error") {
    $("run").disabled = false;
    setStatus(error || "分析失败", true);
    $("report").innerHTML = "";
  } else if (runState === "done" && report) {
    $("run").disabled = false;
    setStatus("");
    render(report);
  } else {
    $("run").disabled = false;
  }
}

function setStatus(msg, isError) {
  const el = $("status");
  el.textContent = msg || "";
  el.className = "status" + (isError ? " error" : "");
}

async function run() {
  const target_url = $("url").value.trim();
  const keyword = $("keyword").value.trim();
  const region = $("region").value;
  const backend = $("backend").value.trim().replace(/\/+$/, "");
  const useBrowser = $("browser").checked;

  if (!target_url || !/^https?:/.test(target_url)) {
    return setStatus("目标 URL 需为 http/https 网址。", true);
  }

  await chrome.storage.local.set({ keyword, region, backend, useBrowser });
  $("run").disabled = true;
  $("report").innerHTML = "";

  // 仅当目标 URL 还是当前标签页时，才读它的已渲染正文（有登录态、已过反爬）；
  // 若用户改成了别的网址，则交后端抓取（勾选则用本地浏览器过反爬）。
  let pageText = "", pageTitle = "";
  const sameAsTab = CURRENT_TAB && CURRENT_TAB.url === target_url;
  if (sameAsTab) {
    setStatus("读取当前页面正文…");
    try {
      const [inj] = await chrome.scripting.executeScript({
        target: { tabId: CURRENT_TAB.id },
        func: () => ({
          text: (document.body && document.body.innerText) || "",
          title: document.title || "",
        }),
      });
      if (inj && inj.result) {
        pageText = (inj.result.text || "").slice(0, 40000);
        pageTitle = inj.result.title || "";
      }
    } catch (e) {
      // chrome:// 等受限页无法注入，留空交后端抓取
    }
  }

  // keyword 为空 → 单页模式；交给后台 worker 跑（弹窗关了也不断）
  const body = {
    keyword: keyword || null,
    target_url,
    ...REGION[region],
    target_text: pageText,
    target_title: pageTitle,
    fetch_mode: useBrowser ? "browser" : null,
  };
  const mode = keyword ? "对比竞品" : "单页";
  const extra = useBrowser ? " · 本地浏览器抓取较慢" : "";
  setStatus(`分析中（${mode}模式${extra}）…可关闭弹窗，完成后角标显示 ✓`);
  chrome.runtime.sendMessage({ type: "analyze", backend, body });
}

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}
function chip(k, v) {
  const c = el("div", "chip");
  c.append(el("div", "k", k), el("div", "v", String(v)));
  return c;
}

function render(r) {
  const root = $("report");
  root.innerHTML = "";

  const bar = el("div", "scorebar");
  const auth = r.eeat.authoritativeness;
  const ig = r.information_gain;
  if (r.mode === "keyword") {
    bar.append(chip("排名", r.in_top_10 ? "#" + r.rank : "未进前10"));
    bar.append(chip("信息增益", ig.gain_score != null ? ig.gain_score : "—"));
  } else {
    bar.append(chip("模式", "单页"));
  }
  bar.append(
    chip("可读性", { easier: "偏易", aligned: "匹配", harder: "偏难", "n/a": "—" }[r.readability.verdict] || r.readability.verdict),
    chip("经验", r.eeat.experience.score),
    chip("专业", r.eeat.expertise.score),
    chip("可信", r.eeat.trust.score),
    chip("权威", auth.available ? auth.score : "—")
  );
  root.append(bar);

  root.append(el("div", "section-title", "优先行动建议"));
  if (!r.priority_actions.length) root.append(el("div", "list-item", "暂无明显差距。"));
  for (const a of r.priority_actions) {
    const box = el("div", "action " + a.impact);
    box.append(el("div", "meta", a.impact + " · " + a.dimension));
    box.append(el("div", null, a.action));
    root.append(box);
  }

  if (r.mode === "keyword") {
    root.append(detailsList(
      "缺失内容（竞品有、你缺）· " + ig.missing_points.length,
      ig.missing_points.map((m) => `[${m.covered_by_n_competitors}家] ${m.text}`)
    ));
  }
  root.append(detailsList(
    (r.mode === "keyword" ? "你的独特内容 · " : "本页主要论点 · ") + ig.novel_points.length,
    ig.novel_points.map((n) => `[${n.type}] ${n.text}`)
  ));

  const ex = (r.debug && r.debug.excel_path) || null;
  if (ex) root.append(el("div", "note", "已写入 Excel：" + ex));
  if (!auth.available) {
    root.append(el("div", "note",
      "权威性（外链）数据不可用，需 DataForSEO 账号验证；其余维度均真实。"));
  }
  root.append(el("div", "note", "EEAT/信息增益为启发式信号代理，非 Google 真实评分。"));
}

function detailsList(title, items) {
  const d = el("details");
  d.append(el("summary", null, title));
  for (const it of items.slice(0, 40)) d.append(el("div", "list-item", it));
  return d;
}

$("run").addEventListener("click", run);
init();
