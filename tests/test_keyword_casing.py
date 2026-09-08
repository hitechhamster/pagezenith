"""关键词一律转小写再进流水线（2026-09-08 用户定）。

    cd api && python ../tests/test_keyword_casing.py

起因：主关键词填成「Best headphone factory in China」，正文句子中间也被照抄成
「…the Best headphone factory in China…」。prompt 里那句「关键词保留」被模型当成
"连大小写一起保留"了。

处理方式（用户拍板）：**不做任何判断**，入口直接转小写，别把用户的大小写带进流水线。
模型按自己的语感写英文——标题该大写就大写，China 这类专有名词它自己会大写。
校验挂在请求模型上而不是 router 里，这样会话过期后前端回传参数的降级路径也走得到。
"""
from __future__ import annotations

import os
import pathlib
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
os.environ["USE_MOCKS"] = "true"
API = pathlib.Path(__file__).resolve().parents[1] / "api"
sys.path.insert(0, str(API))

from tools.seo_writer.models import ArticleRequest, OutlineRequest, PolishRequest  # noqa: E402

PASS, FAIL = [], []


def ok(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{('  — ' + str(extra)) if extra else ''}")


def main_() -> int:
    print("\n[关键词转小写]")

    o = OutlineRequest(main_keyword="Best Headphone Factory In China",
                       secondary_keyword="ANC Headphone Supplier",
                       topic="Sourcing Guide For Buyers")
    ok("第一步 主关键词转小写", o.main_keyword == "best headphone factory in china", o.main_keyword)
    ok("第一步 次关键词转小写", o.secondary_keyword == "anc headphone supplier", o.secondary_keyword)
    # 只动关键词：主题是给模型看的自由文本，用户怎么写就怎么传
    ok("主题不受影响", o.topic == "Sourcing Guide For Buyers", o.topic)

    # 会话过期后前端回传参数的降级路径，同样要转
    a = ArticleRequest(session_id="s", main_keyword="Best Factory",
                       secondary_keyword="Cheap OEM")
    ok("第三步 降级回传也转小写",
       (a.main_keyword, a.secondary_keyword) == ("best factory", "cheap oem"),
       (a.main_keyword, a.secondary_keyword))
    ok("没填时仍是 None（不会炸）",
       ArticleRequest(session_id="s").main_keyword is None)

    p = PolishRequest(article="hi", main_keyword="Best Factory")
    ok("润色回传也转小写", p.main_keyword == "best factory", p.main_keyword)

    # 已经是小写 / 中文的照常
    z = OutlineRequest(main_keyword="八字 排盘", secondary_keyword="免费 测算", topic="t")
    ok("中文关键词不受影响", (z.main_keyword, z.secondary_keyword) == ("八字 排盘", "免费 测算"))

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("失败：" + "、".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main_())
