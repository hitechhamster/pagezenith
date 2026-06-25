"""LLM 客户端（OpenRouter，OpenAI 兼容）。

能力：
  - chat_json():   调模型强制返回 JSON（单页抽取）。
  - embed():       批量 embedding（语义去重粗判），统一走 OpenRouter /embeddings、多语言模型。
  - judge_pairs(): LLM 裁边界——只对落在模糊带的 claim 对判 same/different，返回严格 JSON。

mock 模式下不发请求：embed 返回确定性 hash 向量，judge_pairs 用否定/数字启发式，
便于无 key 跑通且结果可复现。
"""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

import httpx

from ..config import Settings, get_settings
from ..lexical import embed_tokens, heuristic_same


class LLMClient:
    def __init__(self, settings: Settings | None = None):
        self.s = settings or get_settings()

    # ------------------------------------------------------------- chat JSON
    async def chat_json(self, system: str, user: str) -> dict[str, Any]:
        """返回模型输出解析后的 dict。要求模型只输出 JSON。"""
        if self.s.use_mocks:
            return _mock_extraction(user)

        url = f"{self.s.openrouter_base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.s.openrouter_api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.s.llm_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }
        async with httpx.AsyncClient(timeout=self.s.request_timeout) as client:
            resp = await client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
        content = data["choices"][0]["message"]["content"]
        return _safe_json(content)

    async def complete_json(self, system: str, user: str, mock=None, model: str | None = None):
        """通用 JSON 补全，返回 dict 或 list。mock 模式直接返回传入的 mock 值。
        model 可覆盖默认模型（如增补段落用更强的写作模型）。"""
        if self.s.use_mocks:
            return mock
        url = f"{self.s.openrouter_base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.s.openrouter_api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": model or self.s.llm_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0,
        }
        async with httpx.AsyncClient(timeout=self.s.request_timeout) as client:
            resp = await client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
        return _loads_lenient(data["choices"][0]["message"]["content"])

    async def vision_json(self, prompt: str, image_png: bytes, mock=None, model: str | None = None):
        """多模态：传一张 PNG 截图 + 文本提示，返回解析后的 JSON。"""
        if self.s.use_mocks:
            return mock
        b64 = base64.b64encode(image_png).decode()
        url = f"{self.s.openrouter_base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.s.openrouter_api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": model or self.s.llm_model,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64," + b64}},
            ]}],
            "temperature": 0,
        }
        async with httpx.AsyncClient(timeout=self.s.request_timeout) as client:
            resp = await client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
        return _loads_lenient(data["choices"][0]["message"]["content"])

    # --------------------------------------------------------------- embed
    async def embed(self, texts: list[str]) -> list[list[float]]:
        """批量 embedding（与输入等长）。mock 模式返回确定性 hash 向量。"""
        if not texts:
            return []
        if self.s.use_mocks:
            return [_mock_embed(t) for t in texts]

        base_url, api_key = self.s.embedding_endpoint()
        url = f"{base_url}/embeddings"
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        payload = {"model": self.s.embedding_model, "input": texts}
        async with httpx.AsyncClient(timeout=self.s.request_timeout) as client:
            resp = await client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
        # 按 index 排序，保证与输入顺序对齐
        items = sorted(data["data"], key=lambda d: d.get("index", 0))
        return [item["embedding"] for item in items]

    # ---------------------------------------------------------- judge pairs
    async def judge_pairs(self, pairs: list[tuple[str, str]]) -> list[bool]:
        """裁决模糊对是否“同一条事实”。返回与 pairs 等长的 bool 列表。"""
        if not pairs:
            return []
        if self.s.use_mocks:
            return [_mock_judge(a, b) for a, b in pairs]

        numbered = [{"pair_id": i, "text_a": a, "text_b": b} for i, (a, b) in enumerate(pairs)]
        user = JUDGE_USER_TEMPLATE.format(pairs=json.dumps(numbered, ensure_ascii=False))
        url = f"{self.s.openrouter_base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.s.openrouter_api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.s.llm_model,
            "messages": [
                {"role": "system", "content": JUDGE_SYSTEM},
                {"role": "user", "content": user},
            ],
            "temperature": 0,
        }
        async with httpx.AsyncClient(timeout=self.s.request_timeout) as client:
            resp = await client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
        content = data["choices"][0]["message"]["content"]
        return _parse_judgements(content, len(pairs))


def _strip_fences(content: str) -> str:
    content = content.strip()
    if content.startswith("```"):
        content = content.split("```", 2)[1]
        if content.startswith("json"):
            content = content[4:]
    return content


def _loads_lenient(content: str):
    """容错解析：去 ``` 包裹，截取最外层 {} 或 []；失败则用 json_repair 修复 LLM 破 JSON。"""
    content = _strip_fences(content)
    obj_s, arr_s = content.find("{"), content.find("[")
    if arr_s != -1 and (obj_s == -1 or arr_s < obj_s):
        start, end = arr_s, content.rfind("]")
    else:
        start, end = obj_s, content.rfind("}")
    if start != -1 and end != -1:
        content = content[start : end + 1]
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        from json_repair import repair_json
        return json.loads(repair_json(content))


def _safe_json(content: str) -> dict[str, Any]:
    """模型偶尔会用 ```json 包裹或加前言/输出破 JSON，做容错解析 + json_repair 兜底。"""
    content = _strip_fences(content)
    start, end = content.find("{"), content.rfind("}")
    if start != -1 and end != -1:
        content = content[start : end + 1]
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        from json_repair import repair_json
        return json.loads(repair_json(content))


# --------------------------------------------------------------------------- #
# LLM 裁边界 prompt + 解析
# --------------------------------------------------------------------------- #
JUDGE_SYSTEM = """\
You decide whether two short factual claims state THE SAME fact.
You are correcting an embedding model that is blind to two things:
  1) Negation / opposite direction (regulated vs unregulated, rise vs fall, up vs down)
     → these are DIFFERENT facts.
  2) Different numbers (different value OR different direction), even if the wording is
     almost identical (e.g. "withdrawal in 2 days" vs "withdrawal in 5 days") → DIFFERENT.
The same fact expressed differently (paraphrase, synonym, another language phrasing of the
same fact) → SAME.
Output ONLY a JSON array, no preface, no markdown fences:
[{"pair_id": <int>, "same": <true|false>}]
"""

JUDGE_USER_TEMPLATE = """\
Judge each pair. Return one object per pair_id.
PAIRS:
{pairs}
"""


def _parse_judgements(content: str, n: int) -> list[bool]:
    content = content.strip()
    if content.startswith("```"):
        content = content.split("```", 2)[1]
        if content.startswith("json"):
            content = content[4:]
    start, end = content.find("["), content.rfind("]")
    if start != -1 and end != -1:
        content = content[start : end + 1]
    result = [False] * n
    for item in json.loads(content):
        pid = item.get("pair_id")
        if isinstance(pid, int) and 0 <= pid < n:
            result[pid] = bool(item.get("same", False))
    return result


# --------------------------------------------------------------------------- #
# Mock：确定性 embedding（hash 向量）+ 启发式裁决（裁决逻辑见 lexical.py）
# --------------------------------------------------------------------------- #
_MOCK_DIM = 256


def _mock_embed(text: str) -> list[float]:
    """确定性 hashing 向量：相同文本 → 相同向量，token 重叠越多 cosine 越高。"""
    vec = [0.0] * _MOCK_DIM
    for tok in embed_tokens(text):
        h = int(hashlib.md5(tok.encode("utf-8")).hexdigest(), 16)
        vec[h % _MOCK_DIM] += 1.0
    norm = sum(v * v for v in vec) ** 0.5 or 1.0
    return [v / norm for v in vec]


def _mock_judge(a: str, b: str) -> bool:
    return heuristic_same(a, b)


# --------------------------------------------------------------------------- #
# Mock：返回一个合理的单页 profile，区分目标页/竞品页
# --------------------------------------------------------------------------- #
def _mock_extraction(user: str) -> dict[str, Any]:
    is_competitor = "competitor" in user
    if is_competitor:
        return {
            "url": _url_from(user),
            "claims": [
                {"text": "Forex brokers are licensed by FCA, ASIC and CySEC",
                 "type": "fact", "has_source": True, "is_firsthand": False},
                {"text": "A regulated broker segregates client funds",
                 "type": "fact", "has_source": False, "is_firsthand": False},
                {"text": "EU retail leverage is capped at 30:1",
                 "type": "data_point", "has_source": True, "is_firsthand": False},
                {"text": "Verify a broker license number on the regulator register",
                 "type": "how_to_step", "has_source": False, "is_firsthand": False},
            ],
            "subtopics": ["regulation", "license verification", "leverage caps"],
            "eeat_signals": {
                "author_named": True, "author_credentials": "CFA, 10y markets",
                "firsthand_markers": [], "outbound_citations": 7,
                "published_date": "2025-03-01", "updated_date": "2026-01-10",
                "has_about_or_contact": True, "red_flags": [],
            },
            "readability_notes": ["术语 CySEC 未解释"],
        }
    return {
        "url": _url_from(user),
        "claims": [
            {"text": "Unregulated brokers delay withdrawals by 9 days on average",
             "type": "data_point", "has_source": False, "is_firsthand": True},
            {"text": "We tested 12 brokers and recorded withdrawal times",
             "type": "fact", "has_source": False, "is_firsthand": True},
            {"text": "EU retail leverage is capped at 30:1",
             "type": "data_point", "has_source": True, "is_firsthand": False},
        ],
        "subtopics": ["scam detection", "withdrawal testing", "leverage caps"],
        "eeat_signals": {
            "author_named": False, "author_credentials": None,
            "firsthand_markers": ["我们实测了 12 家券商", "附原始 support 聊天截图"],
            "outbound_citations": 1, "published_date": "2026-02-01",
            "updated_date": None, "has_about_or_contact": False, "red_flags": [],
        },
        "readability_notes": ["开头直接给结论，意图回答较快"],
    }


def _url_from(user: str) -> str:
    for token in user.split():
        if token.startswith("http"):
            return token.strip().strip("\"'")
    return ""
