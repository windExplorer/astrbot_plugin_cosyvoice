"""CosyVoice 语音标记注入（规范见 docs/cosyvoice_tts_markup_guide.md）。

两类标记：
- 第一类（拼 tts_text，直接朗读）：[laughter] [breath] [quick_breath] [sigh] [cough] ...
- 第二类（拼 prompt_text 前缀，情绪/方言）：本期不做（阶段 C 未启用）。

本模块只做「第一类 + 规则 1/2 自动插标」，且**只作用于合成文本**，绝不进入
display_text / prompt_text / 结果链文字。
"""
import re

# ---------- 标记白名单（不朗读剥离，反而要保留进合成）----------
SPECIAL_TOKENS = [
    "laughter", "breath", "quick_breath", "sigh", "cough", "hissing",
    "lipsmack", "clucking", "accent", "noise", "vocalized-noise", "mn",
    "laugh", "chuckle", "snicker", "sob", "groan",
]
_TOK_ALT = "|".join(re.escape(t) for t in SPECIAL_TOKENS)
# 英文方括号：[token]
_BRACKET_TOKEN_RE = re.compile(r"\[" + _TOK_ALT + r"\]")
# 英文尖括号：<laughter>...</laughter>
_ANGLE_TOKEN_RE = re.compile(r"<" + _TOK_ALT + r">.*?</" + _TOK_ALT + r">")
# 合并：供 _strip_brackets 跳过这些标记（避免被当「不朗读括号」误删）
MARKUP_WHITELIST_RE = re.compile(
    r"\[" + _TOK_ALT + r"\]|<" + _TOK_ALT + r">.*?</" + _TOK_ALT + r">"
)

# ---------- 规则 1：标点换气 ----------
_PUNCT_BREATH = {
    "zh": ("。！？!?", "[breath]"),
    "ja": ("。！？!?", "[breath]"),
    "en": (".!?", "[breath]"),
}
_PUNCT_QUICK = {
    "zh": ("，、；：,;:", "[quick_breath]"),
    "ja": ("、，；：,;:", "[quick_breath]"),
    "en": (",;:", "[quick_breath]"),
}
# 英文句末点排除缩写 / 小数
_EN_ABBREV_RE = re.compile(
    r"\b(?:Mr|Mrs|Ms|Dr|Prof|St|vs|etc|Inc|Jr|Sr|No|Co|Corp)\.$", re.IGNORECASE
)
_EN_NUM_RE = re.compile(r"\d+\.\d+")
_MIN_BREATH_GAP = 8  # 两个 [quick_breath] 最小间隔字数


def _split_keep_markup(text: str):
    """把文本切成 (kind, seg)：mark=已知标记原样，text=需插标的普通文本。"""
    parts = []
    pos = 0
    for m in MARKUP_WHITELIST_RE.finditer(text):
        if m.start() > pos:
            parts.append(("text", text[pos:m.start()]))
        parts.append(("mark", m.group(0)))
        pos = m.end()
    if pos < len(text):
        parts.append(("text", text[pos:]))
    return parts


def _insert_breath_in_plain(seg: str, lang: str) -> str:
    end_punct, end_tok = _PUNCT_BREATH.get(lang, _PUNCT_BREATH["zh"])
    quick_punct, quick_tok = _PUNCT_QUICK.get(lang, _PUNCT_QUICK["zh"])
    end_set = set(end_punct)
    quick_set = set(quick_punct)
    out = []
    last = -100
    i = 0
    n = len(seg)
    while i < n:
        ch = seg[i]
        out.append(ch)
        # 标记内部不处理（理论 seg 已无标记，保险）
        if ch in ("[", "<"):
            i += 1
            continue
        nxt = seg[i + 1] if i + 1 < n else ""
        if ch in end_set and nxt not in end_set and nxt not in quick_set:
            if lang == "en" and ch == ".":
                ctx = seg[max(0, i - 14): i + 1]
                if _EN_ABBREV_RE.search(ctx) or _EN_NUM_RE.search(ctx):
                    i += 1
                    continue
            out.append(end_tok)
            last = len(out)
        elif ch in quick_set and nxt not in end_set and nxt not in quick_set:
            if len(out) - last >= _MIN_BREATH_GAP:
                out.append(quick_tok)
                last = len(out)
        i += 1
    return "".join(out)


def inject_breath(text: str, lang: str = "zh") -> str:
    """按语种标点集插入换气标记。连续标点只插一次；[quick_breath] 间隔 ≥8 字。"""
    if not text:
        return text
    return "".join(
        seg if kind == "mark" else _insert_breath_in_plain(seg, lang)
        for kind, seg in _split_keep_markup(text)
    )


# ---------- 规则 2：关键词音效 ----------
_EFFECT_DICT = {
    "zh": {
        "笑声": ("[laughter]", r"笑|哈哈|噗嗤|咯咯|嘻嘻|偷笑|捧腹|乐了"),
        "叹气": ("[sigh]", r"叹气|无奈|唉|哎"),
        "咳嗽": ("[cough]", r"咳嗽|咳了一声"),
    },
    "ja": {
        "笑声": ("[laughter]", r"笑|わら|ふふ|あは|えへ|ごほ"),
        "叹气": ("[sigh]", r"ため息| sigh"),
        "咳嗽": ("[cough]", r"せき|咳"),
    },
    "en": {
        "laugh": ("[laughter]", r"\b(laugh|lol|haha|chuckle|giggle)\b"),
        "sigh": ("[sigh]", r"\b(sigh|ugh)\b"),
        "cough": ("[cough]", r"\bcough\b"),
    },
}
_DENSITY = {"light": 1, "medium": 3, "strong": 5}
_MIN_EFFECT_GAP = 12  # 相邻音效最小间隔字数（句级，粗略）


def _is_false_positive(seg: str, pat: str) -> bool:
    """排除「可笑的是 / 笑嘻嘻的」这类形容用法，仅在情绪词后跟实意停顿才算触发。"""
    m = re.search(pat, seg)
    if not m:
        return True
    after = seg[m.end():].lstrip()
    return bool(after) and after[0] in "的是地得着了"


def inject_effect(text: str, lang: str = "zh", intensity: str = "medium") -> str:
    """按语种词典在句末追加音效标记。密度上限控插入数量；排除形容误触发。"""
    if not text:
        return text
    dict_lang = _EFFECT_DICT.get(lang, _EFFECT_DICT["zh"])
    max_n = _DENSITY.get(intensity, 3)
    end_cls = _PUNCT_BREATH.get(lang, _PUNCT_BREATH["zh"])[0]
    # 按句切（保留句末标点）：用捕获组把标点也切出来
    pieces = re.split(r"([" + re.escape(end_cls) + r"])", text)
    res = []
    inserted = 0
    i = 0
    while i < len(pieces):
        body = pieces[i]
        punc = pieces[i + 1] if i + 1 < len(pieces) else ""
        i += 2
        if not body.strip():
            res.append(body + punc)
            continue
        if inserted < max_n:
            for _label, (token, pat) in dict_lang.items():
                if re.search(pat, body) and not _is_false_positive(body, pat):
                    body = body + token
                    inserted += 1
                    break
        res.append(body + punc)
    return "".join(res)


# ---------- 语种推断（音色 language 优先，否则按字符特征）----------
def detect_lang(text: str, voice_lang: str | None = None) -> str:
    if voice_lang in ("zh", "ja", "en"):
        return voice_lang
    if re.search(r"[぀-ヿ]", text or ""):
        return "ja"
    if re.search(r"[A-Za-z]", text or "") and not re.search(r"[一-鿿]", text or ""):
        return "en"
    return "zh"


def inject_markup(text: str, lang: str, cfg: dict) -> str:
    """统一入口：仅作用于合成文本。

    - auto_breath 默认开（低风险）；auto_effect 默认关。
    - 注入顺序：关键词音效（句末追加）→ 标点换气（标点后插入）。
    """
    if not text:
        return text
    auto_breath = cfg.get("auto_breath", True)
    auto_effect = cfg.get("auto_effect", False)
    if not auto_breath and not auto_effect:
        return text
    t = text
    if auto_effect:
        t = inject_effect(t, lang, cfg.get("effect_intensity", "medium"))
    if auto_breath:
        t = inject_breath(t, lang)
    return t
