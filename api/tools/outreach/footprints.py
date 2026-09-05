"""外链拓客的搜索足迹（footprint）。

用搜索引擎足迹近似"谁可能给外链"——不接付费反链库。每个足迹 = 1 次 SerpApi。
每个足迹自带一个默认机会类型提示（LLM 会再据实纠正）。

⚠️ 足迹必须按目标语言出：gl/hl 只是排序偏好，挡不住"查询词本身是英文"。
实测 gl=fr&hl=fr 下 `assurance auto "write for us"` 10 条里只有 1 个法语站，
换成 `assurance auto "article invité"` 就有 8 个。所以每种语言要有自己的一套。
"""

from __future__ import annotations

# (查询模板, 默认机会提示)。按经验从高命中往下排，取前 N 个
# （standard 取 6 / wide 取 12 / max 取 8，所以前 6 个必须是最硬的）。
_EN: list[tuple[str, str]] = [
    ('{kw} "write for us"', "投稿"),
    ('{kw} "guest post"', "投稿"),
    ('{kw} intitle:resources', "资源位加链"),
    ('{kw} "recommended tools"', "资源位加链"),
    ('{kw} blog', "合作"),
    ('{kw} "useful links"', "资源位加链"),
    ('{kw} "submit a guest post"', "投稿"),
    ('{kw} "become a contributor"', "投稿"),
    ('{kw} "guest post guidelines"', "投稿"),
    ('{kw} inurl:resources', "资源位加链"),
    ('{kw} "recommended reading"', "资源位加链"),
    ('{kw} "best" (tools OR resources)', "资源位加链"),
    ('{kw} "roundup"', "合作"),
    ('{kw} "advertise with us"', "合作"),
    ('{kw} "contribute to"', "投稿"),
    ('{kw} "top" blogs', "合作"),
]

_FR: list[tuple[str, str]] = [
    ('{kw} "article invité"', "投稿"),
    ('{kw} "proposer un article"', "投稿"),
    ('{kw} "écrire pour nous"', "投稿"),
    ('{kw} "devenir rédacteur"', "投稿"),
    ('{kw} "liens utiles"', "资源位加链"),
    ('{kw} blog', "合作"),
    ('{kw} intitle:ressources', "资源位加链"),
    ('{kw} "outils recommandés"', "资源位加链"),
    ('{kw} "publier un article"', "投稿"),
    ('{kw} "meilleurs blogs"', "合作"),
    ('{kw} "nos partenaires"', "合作"),
    ('{kw} inurl:ressources', "资源位加链"),
    ('{kw} "à lire également"', "资源位加链"),
    ('{kw} "devenir partenaire"', "合作"),
    ('{kw} "annuaire"', "目录"),
    ('{kw} "guest blogging"', "投稿"),
]

_ES: list[tuple[str, str]] = [
    ('{kw} "escribe para nosotros"', "投稿"),
    ('{kw} "artículo invitado"', "投稿"),
    ('{kw} "colabora con nosotros"', "投稿"),
    ('{kw} "post invitado"', "投稿"),
    ('{kw} "enlaces de interés"', "资源位加链"),
    ('{kw} blog', "合作"),
    ('{kw} intitle:recursos', "资源位加链"),
    ('{kw} "herramientas recomendadas"', "资源位加链"),
    ('{kw} "sé colaborador"', "投稿"),
    ('{kw} "mejores blogs"', "合作"),
    ('{kw} "recursos recomendados"', "资源位加链"),
    ('{kw} inurl:recursos', "资源位加链"),
    ('{kw} "lecturas recomendadas"', "资源位加链"),
    ('{kw} "colaboradores"', "投稿"),
    ('{kw} "trabaja con nosotros"', "合作"),
    ('{kw} "directorio"', "目录"),
]

_DE: list[tuple[str, str]] = [
    ('{kw} "Gastbeitrag"', "投稿"),
    ('{kw} "Gastartikel"', "投稿"),
    ('{kw} "schreibe für uns"', "投稿"),
    ('{kw} "Gastautor"', "投稿"),
    ('{kw} "nützliche Links"', "资源位加链"),
    ('{kw} blog', "合作"),
    ('{kw} "Linksammlung"', "资源位加链"),
    ('{kw} "empfohlene Tools"', "资源位加链"),
    ('{kw} "Gastbeitrag schreiben"', "投稿"),
    ('{kw} "beste Blogs"', "合作"),
    ('{kw} intitle:Ressourcen', "资源位加链"),
    ('{kw} "weiterführende Links"', "资源位加链"),
    ('{kw} "Partner werden"', "合作"),
    ('{kw} "Leseempfehlungen"', "资源位加链"),
    ('{kw} "Kooperation"', "合作"),
    ('{kw} "Verzeichnis"', "目录"),
]

_IT: list[tuple[str, str]] = [
    ('{kw} "scrivi per noi"', "投稿"),
    ('{kw} "guest post"', "投稿"),
    ('{kw} "collabora con noi"', "投稿"),
    ('{kw} "articolo ospite"', "投稿"),
    ('{kw} "link utili"', "资源位加链"),
    ('{kw} blog', "合作"),
    ('{kw} intitle:risorse', "资源位加链"),
    ('{kw} "strumenti consigliati"', "资源位加链"),
    ('{kw} "diventa autore"', "投稿"),
    ('{kw} "migliori blog"', "合作"),
    ('{kw} "risorse utili"', "资源位加链"),
    ('{kw} inurl:risorse', "资源位加链"),
    ('{kw} "letture consigliate"', "资源位加链"),
    ('{kw} "proponi un articolo"', "投稿"),
    ('{kw} "partnership"', "合作"),
    ('{kw} "directory"', "目录"),
]

_PT: list[tuple[str, str]] = [
    ('{kw} "escreva para nós"', "投稿"),
    ('{kw} "guest post"', "投稿"),
    ('{kw} "artigo convidado"', "投稿"),
    ('{kw} "colabore conosco"', "投稿"),
    ('{kw} "links úteis"', "资源位加链"),
    ('{kw} blog', "合作"),
    ('{kw} intitle:recursos', "资源位加链"),
    ('{kw} "ferramentas recomendadas"', "资源位加链"),
    ('{kw} "seja um colaborador"', "投稿"),
    ('{kw} "melhores blogs"', "合作"),
    ('{kw} "recursos úteis"', "资源位加链"),
    ('{kw} inurl:recursos', "资源位加链"),
    ('{kw} "leituras recomendadas"', "资源位加链"),
    ('{kw} "publique conosco"', "投稿"),
    ('{kw} "parceria"', "合作"),
    ('{kw} "diretório"', "目录"),
]

_JA: list[tuple[str, str]] = [
    ('{kw} "寄稿"', "投稿"),
    ('{kw} "ゲスト投稿"', "投稿"),
    ('{kw} "執筆者募集"', "投稿"),
    ('{kw} "参考リンク"', "资源位加链"),
    ('{kw} ブログ', "合作"),
    ('{kw} "おすすめツール"', "资源位加链"),
    ('{kw} "寄稿募集"', "投稿"),
    ('{kw} "リンク集"', "资源位加链"),
    ('{kw} "ライター募集"', "投稿"),
    ('{kw} "おすすめブログ"', "合作"),
    ('{kw} "関連リンク"', "资源位加链"),
    ('{kw} "相互リンク"', "合作"),
    ('{kw} "掲載依頼"', "合作"),
    ('{kw} "役立つサイト"', "资源位加链"),
    ('{kw} "提携"', "合作"),
    ('{kw} "まとめ" サイト', "目录"),
]

_ZH: list[tuple[str, str]] = [
    ('{kw} "欢迎投稿"', "投稿"),
    ('{kw} "征稿"', "投稿"),
    ('{kw} "友情链接"', "合作"),
    ('{kw} "推荐工具"', "资源位加链"),
    ('{kw} 博客', "合作"),
    ('{kw} "延伸阅读"', "资源位加链"),
    ('{kw} "作者招募"', "投稿"),
    ('{kw} "相关链接"', "资源位加链"),
    ('{kw} "投稿须知"', "投稿"),
    ('{kw} "资源导航"', "目录"),
    ('{kw} "推荐阅读"', "资源位加链"),
    ('{kw} "专栏作者"', "投稿"),
    ('{kw} "工具推荐"', "资源位加链"),
    ('{kw} "商务合作"', "合作"),
    ('{kw} "导航站"', "目录"),
    ('{kw} "优质博客"', "合作"),
]

# 语言码 → 足迹表。取 language_code 的主语言段（zh-CN → zh），未命中回落英文。
FOOTPRINTS_BY_LANG: dict[str, list[tuple[str, str]]] = {
    "en": _EN, "fr": _FR, "es": _ES, "de": _DE,
    "it": _IT, "pt": _PT, "ja": _JA, "zh": _ZH,
}

# 向后兼容：老调用点若还 import FOOTPRINTS，拿到的是英文表。
FOOTPRINTS = _EN


def footprints_for(lang: str) -> list[tuple[str, str]]:
    base = (lang or "en").strip().lower().replace("_", "-").split("-")[0]
    return FOOTPRINTS_BY_LANG.get(base, _EN)


def build_footprints(keyword: str, n: int, lang: str = "en") -> list[tuple[str, str]]:
    kw = keyword.strip()
    return [(tpl.format(kw=kw), hint) for tpl, hint in footprints_for(lang)[: max(1, n)]]
