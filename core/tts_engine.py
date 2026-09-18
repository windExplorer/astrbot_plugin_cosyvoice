"""TTS 引擎：封装音色解析、长文本分片与多段拼接。"""

import os
import re
import json
import time
import asyncio

from astrbot.api import logger

from ..cosyvoice.client import CosyVoiceClient, CosyVoiceServerError, QueueFullError
from .markup import SPECIAL_TOKENS, inject_markup
from ..utils import audio

# 插件根目录（core/ 的上一级），用于解析相对参考音频路径
_PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 默认参考音频目录：插件内的 voices/ 文件夹
VOICES_DIR = os.path.join(_PLUGIN_ROOT, "voices")


# 平台/框架对「历史消息中的媒体」的序列化占位符标签，如：
#   <pc_history_media images="1" />
#   <history_media ... />、<image ... /> 等
# 它们本应渲染成图片/音频，但会以普通文本混入消息流，若不剔除会被当成正文朗读/显示。
# 匹配规则：
#   1) 自闭合标签 <... />（<pc_history_media images="1" /> 即此形态）；
#   2) 含 media/image/img/record 关键词的尖括号标签。
_SELF_CLOSE_TAG_RE = re.compile(r"<[a-zA-Z_][^<>]*?/>", re.S)
_MEDIA_KEY_TAG_RE = re.compile(r"<[^<>]*(?:media|image|img|record)[^<>]*>", re.I)
# AstrBot 工具调用序列化：标识符（工具名）紧接一个 JSON 对象，且对象以 "name" 键开头，
# 如 comfyui_draw{"name":"comfyui_draw","args":{...}}。在 tool_loop 最终响应的
# completion_text 里会混入这类内容，必须完整剔除（否则被当正文朗读/显示）。
_LLM_IDENT_RE = re.compile(r"[A-Za-z_]\w*")
# agent runner 的系统调用标记，如 <system_call>{...}</system_call>（LLM 工具调用泄漏）。
# 若未被框架正确消费会以纯文本混入正文，必须在合成/显示前剔除。
# 兼容多种泄漏形态：成对闭合、自闭合、孤立开标签（无内容/内容未闭合）、孤立收尾标签。
_SYSTEM_CALL_RE = re.compile(
    r"<system_call\b[^>]*>.*?</system_call>"  # 成对闭合
    r"|<system_call\b[^>]*/?>"                 # 自闭合或孤立开标签
    r"|</system_call\s*>",                     # 孤立收尾标签
    re.I | re.S,
)
# LLM 推理/元标记泄漏：<think>...</think> 及其自闭合/缺失闭合变体、</think> 残留等。
# 这类标记若未被框架剥除，会以纯文本混入最终回复，应在合成/显示前剔除。
_THINK_TAG_RE = re.compile(r"<think\b[^>]*>.*?</think>|<think\b[^>]*/?>|</think\s*>", re.I | re.S)
# Markdown 围栏代码块（```...``` / ~~~...~~~）：代码念出来是噪音，整块剔除。
_CODE_FENCE_RE = re.compile(r"```[\s\S]*?```|~~~[\s\S]*?~~~")
# Markdown 行内代码（`...`）：命令/变量名念出来无意义，整块剔除。
_INLINE_CODE_RE = re.compile(r"`[^`\n]+`")
# 残余的 HTML/XML 标签（<br/>、<span class="x"> 等）：剔除标签本身，保留标签外文本。
_GENERIC_TAG_RE = re.compile(r"</?[A-Za-z][^>\n]*>")
# Markdown 强调符号（**加粗**、__下划线加粗__）：星号/下划线会被 TTS 念出来或造成怪顿，
# 剔除符号本身、保留文字。单星号/单下划线不动（避免误伤乘号等正常用法）。
_MD_EMPH_RE = re.compile(r"\*\*|__")

# 段尾「未闭合的副语言标记」形态：以 `[` 开头、后面只有标记名字符、到段尾都没等到 `]`。
# 例：`...好呀。[quick_` ——分段硬切正好落在 [quick_breath] 中间就是这个样子。
_TAG_TAIL_RE = re.compile(r"\[([A-Za-z_]*)$")

# 「整段只有副语言标记」形态（允许前后空白）：如 `[breath]`、`[quick_breath][breath]`。
_MARKUP_ONLY_RE = re.compile(r"^(?:\s*\[[A-Za-z_]+\])+\s*$")


def _has_open_markup_tail(s: str) -> bool:
    """段尾是否为「某个白名单副语言标记被截断的前缀」。

    只认前缀匹配（`[quick_` 是 `quick_breath` 的前缀才算），避免误伤普通文本里
    未配对的 `[`（如模型写的「温度[摄氏」这类内容被错误拼接）。
    """
    m = _TAG_TAIL_RE.search(s or "")
    if not m:
        return False
    head = m.group(1).lower()
    return any(tok.startswith(head) for tok in SPECIAL_TOKENS)


def _looks_like_tool_call(text: str, start: int) -> int:
    """从 start 处判断是否形如 `工具名{...}` 的工具调用；是则返回配对大括号后的下标，否则返回 -1。

    要求：标识符 + 可选空白 + `{`，且 `{` 后（跳空白）以 `"name"` 键开头。
    用大括号配对定位整个 JSON 块，避免 JSON 内标点被分段器切碎后残留。
    """
    m = _LLM_IDENT_RE.match(text, start)
    if not m:
        return -1
    j = m.end()
    k = j
    n = len(text)
    while k < n and text[k] in " \t\n\r":
        k += 1
    if k >= n or text[k] != "{":
        return -1
    probe = k + 1
    while probe < n and text[probe] in " \t\n\r":
        probe += 1
    if text[probe : probe + 6].lower() != '"name"':
        return -1
    depth = 0
    p = k
    while p < n:
        if text[p] == "{":
            depth += 1
        elif text[p] == "}":
            depth -= 1
            if depth == 0:
                return p + 1
        p += 1
    return n


def clean_tool_calls(text: str) -> str:
    """剔除文本中的 LLM 工具调用序列化（如 ``comfyui_draw{"name":...}``）。"""
    if not text:
        return text
    out = []
    i = 0
    n = len(text)
    while i < n:
        end = _looks_like_tool_call(text, i)
        if end > i:
            i = end
            continue
        out.append(text[i])
        i += 1
    return "".join(out).strip()


def clean_media_placeholders(text: str) -> str:
    """剔除文本中的媒体占位符标签（图片/音频/历史媒体等），返回净化后的文本。"""
    if not text:
        return text
    t = _SELF_CLOSE_TAG_RE.sub("", text)
    t = _MEDIA_KEY_TAG_RE.sub("", t)
    return t.strip()


def clean_tts_text(text: str) -> str:
    """回复文本综合净化：剔除代码块/行内代码 + 媒体占位符标签 + 工具调用序列化 + 系统调用/思考标记 + HTML 标签。"""
    if not text:
        return text
    t = _CODE_FENCE_RE.sub(" ", text)          # 围栏代码块整块剔除（先于标签清理，避免块内标签干扰）
    t = _SYSTEM_CALL_RE.sub("", t)
    t = _THINK_TAG_RE.sub("", t)
    t = clean_media_placeholders(t)
    t = _INLINE_CODE_RE.sub(" ", t)            # 行内代码剔除
    t = _GENERIC_TAG_RE.sub(" ", t)            # 残余 HTML/XML 标签剔除（保留标签外文本）
    t = _MD_EMPH_RE.sub("", t)                 # Markdown 强调符号（** / __）剔除
    return clean_tool_calls(t)


def is_speakable(text: str) -> bool:
    """判断文本是否值得拿去合成语音：

    - 空 / 纯空白 → 否；
    - 占位符 ``[]`` ``{}`` ``null`` ``None`` ``nil`` ``undefined`` → 否；
    - 仅由括号 / 空白 / 引号构成（如 ``[ ]`` ``{ }``） → 否；
    - 仅含媒体占位符标签（如 ``<pc_history_media images="1" />``）→ 否；
    - 仅含工具调用序列化（如 ``comfyui_draw{"name":...}``）→ 否；
    - 其余（含正常中文、标点） → 是。
    """
    if not text:
        return False
    t = clean_tts_text(text)
    if not t:
        return False
    if t in ("[]", "{}", "null", "None", "nil", "undefined", "null"):
        return False
    if re.fullmatch(r"[\s\[\]\{\}\(\)\"']*", t):
        return False
    # 无任何可读字符（字母/数字/汉字/假名/谚文）→ 纯标点、纯符号、纯 emoji → 不合成，
    # 否则服务端只会念出一段静音（用户看到的「空消息也送去转语音」即此类）。
    if not re.search(r"[\w\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]", t):
        return False
    return True


# 上游/框架错误回显特征：LLM 提供方限流、超时、Traceback 等被塞进回复链时携带的标记。
# 这类文本不该转语音——bot 念「错误代码 429 / 请求id xxx」毫无意义。
_ERROR_TEXT_RE = re.compile(
    r"RateLimitError|APIError|AuthenticationError|TimeoutError|ConnectionError"
    r"|InternalServerError|BadRequestError|PermissionDeniedError"
    r"|错误代码[:：]\s*\d{3}|错误类型[:：]|错误消息[:：]|请求id[:：]|请求ID[:：]"
    r"|Traceback \(most recent call last\)|HTTP/\d\.\d\s+\d{3}",
    re.I,
)


def is_error_text(text: str) -> bool:
    """判断文本是否为上游/框架的错误回显（429 限流、超时、Traceback 等）。"""
    if not text:
        return False
    return bool(_ERROR_TEXT_RE.search(text))


class TtsEngine:
    def __init__(self, config: dict, client: CosyVoiceClient, translator=None, concurrency: int = 1):
        self.config = config
        self.client = client
        # 翻译适配器（可选）：在合成前把非目标语种的文本翻成目标语言（默认汉语）。
        self.translator = translator
        self.voices: dict = {}
        # 最近一次合成失败的原因（供 WebUI 试听接口透传，避免只显示「无有效音频」兜底文案）
        self.last_failure = ""
        # 失败类型：config=配置问题（无音色等）、content=内容问题（无有效可合成文本）、
        # server=服务端故障。仅 server 才应触发熔断冷却——
        # 配置/内容问题重试也不会好转，进冷却只会让「插件刚重载、配置尚未就绪」
        # 被误判成服务器失联，并静默停发语音 30s（且给出误导性的失联提示）。
        self.last_failure_kind = ""
        # 全局合成并发信号量：限制同时打到 TTS 服务端的请求数。
        # 弱服务端（GPU 推理）扛不住多并发，多用户同时触发时不限制会把服务端打爆
        # （连接排队 + 读超时雪崩）。默认 1 = 完全串行，最稳；可调大若服务端够强。
        # 注意：信号量只在「真正请求服务端」时占用，等待期间是协程挂起，
        # 不占用 AstrBot 事件循环，因此不会卡住其他消息。
        self._sem = asyncio.Semaphore(max(1, int(concurrency or 1)))
        self.update_voices(config.get("voices", {}))

    # ---------- 路径解析 ----------
    def resolve_wav(self, prompt_wav: str) -> str:
        """解析参考音频路径：

        - 绝对路径且存在 → 直接使用；
        - 相对路径 → 依次尝试 ``voices/<name>``、``插件根/<name>``、原样；
        - 都不存在 → 返回原值（交由 client 报 FileNotFoundError）。
        """
        if not prompt_wav:
            return prompt_wav
        if os.path.isabs(prompt_wav) and os.path.exists(prompt_wav):
            return prompt_wav
        for cand in (os.path.join(VOICES_DIR, prompt_wav), os.path.join(_PLUGIN_ROOT, prompt_wav), prompt_wav):
            if os.path.exists(cand):
                return cand
        return prompt_wav

    def _wav_kwargs(self, prompt_wav: str, prompt_text: str) -> dict:
        """根据配置决定参考音频与参考文本的传递方式。

        - 配置了 server_voices_dir 且本地找不到该文件 → 走服务端本地路径（prompt_wav_path），
          不占用带宽上传大文件；
        - 否则 → 走 AstrBot 服务端本地文件上传（prompt_wav）。

        参考文本约束（严格分离，防止 prompt_text 被污染）：
        - prompt_text 仅来自 voices.<音色>.prompt_text（由 resolve_voice 提供），
          与 LLM system prompt / 对话历史 / 角色设定完全隔离，绝不拼接。
        - prompt_text 非空时才作为表单字段发送；为空则【完全不带该字段】，
          交由服务端从 voices.json 按文件名自动取（避免空串被服务端当「缺失」处理时的歧义）。
        """
        server_dir = (self.config.get("server_voices_dir") or "").strip()
        # 仅当参考文本确实非空才传递，否则让服务端回退到 voices.json
        pt = (prompt_text or "").strip()
        base = {}
        if pt:
            base["prompt_text"] = pt
        if server_dir and prompt_wav and not os.path.exists(self.resolve_wav(prompt_wav)):
            # 仅在 CosyVoice 服务端放好的参考音频：只传文件名，服务端自己读
            return {"prompt_wav_path": prompt_wav, **base}
        return {"prompt_wav": self.resolve_wav(prompt_wav), **base}

    # ---------- 音色解析 ----------
    def update_voices(self, voices):
        """归一化音色配置为内部 dict：{ 音色名: {prompt_wav, prompt_text} }。

        兼容三种来源：
        - template_list 的 list：每项含 name/prompt_wav/prompt_text
          （AstrBot 会附带 __template_key，忽略即可）；
        - 旧式 dict：{ 音色名: {prompt_wav, prompt_text} }；
        - 历史 text 类型的 JSON 字符串（兼容旧配置）。
        """
        new = self._norm_voices(voices)
        # 内容未变化则跳过重建与日志，避免 on_decorating_result 每次触发都重打「已加载 N 个音色」
        if new == self.voices:
            return
        self.voices = new
        if not self.voices and voices:
            logger.warning(
                f"[cosyvoice] 已读到 voices 配置但解析出 0 个音色，"
                f"请检查每项是否填写了「音色名」。原始内容(前500字): {repr(voices)[:500]}"
            )
        elif self.voices:
            logger.info(f"[cosyvoice] 已加载 {len(self.voices)} 个音色: {list(self.voices.keys())}")

    def _norm_voices(self, voices):
        """把 str / dict / list 归一化为 { 音色名: {prompt_wav, prompt_text, hidden} }。"""
        if isinstance(voices, str):
            s = voices.strip()
            if not s or s in ("{}", "[]"):
                return {}
            try:
                voices = json.loads(s)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[cosyvoice] 音色配置解析失败（应为合法 JSON）：{e}")
                return {}
        d: dict = {}
        if isinstance(voices, dict):
            for k, v in voices.items():
                if isinstance(v, dict):
                    d[str(k)] = {
                        "prompt_wav": v.get("prompt_wav", "") or "",
                        "prompt_text": v.get("prompt_text", "") or "",
                        "hidden": bool(v.get("hidden", False)),
                        "language": (v.get("language") or "").strip().lower(),
                        # 音色级副语言标记开关，缺省 True（注入）
                        "markup": bool(v.get("markup", True)),
                    }
        elif isinstance(voices, list):
            for item in voices:
                if not isinstance(item, dict):
                    continue
                name = (item.get("name") or "").strip()
                if not name:
                    continue
                d[name] = {
                    "prompt_wav": item.get("prompt_wav", "") or "",
                    "prompt_text": item.get("prompt_text", "") or "",
                    "hidden": bool(item.get("hidden", False)),
                    "language": (item.get("language") or "").strip().lower(),
                    # 音色级副语言标记开关，缺省 True（注入）
                    "markup": bool(item.get("markup", True)),
                }
        return d

    def list_voices(self, include_hidden: bool = False) -> list:
        """返回音色名列表；默认过滤隐藏音色（include_hidden=True 时返回全部）。

        隐藏音色仅供管理员/已知名字者手动指定使用（如 /tts_voice 名字），
        不出现在普通用户与 LLM 的音色列表中。
        """
        if include_hidden:
            return sorted(self.voices.keys())
        return sorted(k for k, v in self.voices.items() if not v.get("hidden"))

    def resolve_voice(self, voice_name: str | None = None):
        """返回 (音色名, prompt_wav, prompt_text)。找不到任何音色时返回 (None, None, None)。"""
        voices: dict = self.voices
        name = voice_name or self.config.get("default_voice")
        if name and name in voices:
            v = voices[name]
            return name, v.get("prompt_wav", ""), v.get("prompt_text", "")
        # 回退到第一个可用音色
        if voices:
            first = next(iter(voices))
            v = voices[first]
            return first, v.get("prompt_wav", ""), v.get("prompt_text", "")
        return None, None, None

    # ---------- 长文本分片 ----------
    @staticmethod
    def _parse_len_range(raw, default_max: int = 0) -> tuple:
        """把分段字数配置解析为 (min, max) 区间。

        - ``"30,50"`` → (30, 50)
        - ``"50"`` / ``50`` → (0, 50)：只设最大，最小为 0（不强制合并短段）
        - ``0`` / 空 / 非法 → (0, 0)：关闭分段
        min 同时作为「短段合并下限」：切出的段若 < min 字，会并入相邻段。
        """
        if raw is None:
            return (0, default_max)
        s = str(raw).strip()
        if not s:
            return (0, 0)
        parts = re.split(r"[\s,，]+", s)
        nums = []
        for p in parts:
            p = p.strip()
            if p and p.replace(".", "", 1).isdigit():
                nums.append(int(float(p)))
            if len(nums) >= 2:
                break
        if not nums:
            return (0, 0)
        if len(nums) == 1:
            return (0, nums[0])
        lo, hi = nums[0], nums[1]
        if lo > hi:
            lo, hi = hi, lo
        return (lo, hi)

    def _seg_window(self) -> tuple:
        """分段字数区间 (min, max)，由 segment_len 决定，与 max_text_len 解耦。

        - max > 0：窗口上界 = max，在 [start, start+max) 内命中的最后一个分段符号处切；
          min > 0 时作为短段合并下限。
        - max <= 0（关闭分段）：返回 (0, 0)，由调用方回退到旧 max_text_len 逻辑。
        """
        return self._parse_len_range(self.config.get("segment_len", 0), 0)

    def _seg_hard_cap(self) -> int:
        """单段绝对硬上限（仅作用于「窗口内无命中符号时的硬切」）。

        由 max_text_len 决定，与 segment_len 独立：即使分段窗口很大，单段也不会超过此值，
        防止极长无标点文本把整段一次性甩给服务端。0 表示不限制。
        仅在 segment_len > 0（新分段逻辑）时作为兜底生效；旧逻辑由 _legacy_window 处理。
        """
        return int(self.config.get("max_text_len", 0) or 0)

    def _legacy_window(self) -> int:
        """旧分段逻辑窗口：仅在 segment_len 关闭（max<=0）时生效，沿用 max_text_len。"""
        return int(self.config.get("max_text_len", 0) or 0)

    def _seg_first_window(self) -> tuple:
        """首段字数区间 (min, max)：segment_first_len 配置了用它的，否则回退到普通 segment_len。"""
        raw = self.config.get("segment_first_len", 0) or 0
        if not str(raw).strip() or str(raw).strip() == "0":
            return self._seg_window()
        return self._parse_len_range(raw, 0)

    def _seg_nopunct_mode(self, which: str) -> str:
        """无标点时的处理模式：

        - ``truncate``（默认）：窗口内无命中符号，直接在该窗口末硬切。
        - ``seek``：窗口内无命中符号，继续往后（超出窗口）找【第一个】命中的分段符号切在那里；
          若到结尾都找不到，则整段剩余作为一段。
        ``which`` 取 ``"first"``（首段，读 segment_first_nopunct）或 ``"body"``（普通段，读 segment_nopunct）。
        """
        key = "segment_first_nopunct" if which == "first" else "segment_nopunct"
        val = (self.config.get(key, "") or "").strip().lower()
        return "seek" if val == "seek" else "truncate"

    def _seg_punct_class(self) -> str:
        """把配置的 seg 分段符号拼成一个正则字符类（自动转义）。空则退回常见标点。"""
        raw = (self.config.get("segment_punct", "") or "").strip()
        if not raw:
            raw = "，。！？；：、,.!?;:"
        # 转义每个字符，避免用户输入的正则元字符（如 . * +）产生意外匹配
        esc = "".join(re.escape(ch) for ch in raw)
        return f"[{esc}]"

    def split_text(self, text: str) -> list:
        """分段统一入口。**输入必须是干净文本（不含副语言标记）**：文字分段与语音分段
        都调用本方法，因此两者天然用同一套边界、同一套字数——语音不会比文字多切几段。

        副语言标记由 synthesize / iter_segment_* 在分段【之后】逐段注入（_inject），
        绝不参与分段字数计算（否则 48 字正文带标记后变 98 字，会被窗口误切成多段）。
        本入口末尾的两道保护（_merge_broken_markup / _absorb_lone_markup）只为兜住
        「输入文本里本就带着标记」的边缘情况（如模型自己输出了 [breath]）。
        """
        return self._absorb_lone_markup(self._merge_broken_markup(self._split_text_raw(text)))

    def _split_text_raw(self, text: str) -> list:
        text = (text or "").strip()
        if not text:
            return []

        # 优先按换行预分段（QQ 等多行消息）：一个/连续多个换行都视为边界，
        # 归一化、逐行 strip、去空，避免把换行符念成噪音/静音块，也避免超长行一次合成。
        # 每行独立走窗口分段 + 行内短段合并；过短的行（< newline_min_len 字）先与相邻行合并，
        # 避免「嗯」「好的」这类极短行单独成段、单独占一条语音。
        if self._split_by_newline():
            blocks = self.split_newline_blocks(text)
            if len(blocks) <= 1:
                # 实际没有换行（或短行已全部并入同一块）：退化为整段处理（与原行为一致）
                return self._split_window(blocks[0] if blocks else text)
            chunks: list = []
            for b in blocks:
                chunks.extend(self._split_window(b))
            return chunks

        return self._split_window(text)

    def split_text_pairs(self, text: str) -> list:
        """分段并配对：返回 ``[(语音段, [文字段, ...]), ...]``（both 逐段发送用）。

        语音段与 :meth:`split_text` 的结果**完全一致**（换行短块合并照旧：「嗯」「好的」
        这类短行仍并入相邻块合成，不单独占一条语音）；文字段在
        ``text_split_keep_newline=true``（默认）下**不做换行短块合并**——每个原始换行行
        各自走窗口切分。这样聊天记录里的文字仍按原始换行逐条发出，而不是随合并块连成
        一整段（合并是直接拼接，连换行符都不会留下）。

        文字段按「去空白字符轴」归入其起点所在的语音段，因此文字段数可以多于语音段数，
        顺序仍与语音一一对应；任何异常都退化为一一对应（绝不丢文字、不错位）。
        """
        voice_chunks = self.split_text(text)
        if not voice_chunks:
            return []
        one_to_one = [(c, [c]) for c in voice_chunks]
        # 关掉换行分段（或该特性关闭）时，文字与语音保持原来的一一对应
        if not self._keep_newline_text() or not self._split_by_newline():
            return one_to_one
        rows = [r.strip() for r in re.split(r"\n+", text or "") if r.strip()]
        if len(rows) <= 1:
            return one_to_one
        text_chunks: list = []
        for row in rows:
            text_chunks.extend(self._split_window(row))
        text_chunks = self._absorb_lone_markup(self._merge_broken_markup(text_chunks))
        # 没有发生短块合并（两套分段结果相同）→ 退化为一一对应
        if not text_chunks or text_chunks == voice_chunks:
            return one_to_one
        return self._pair_voice_text(voice_chunks, text_chunks)

    def _keep_newline_text(self) -> bool:
        """配置项 text_split_keep_newline：文字是否保持「按原始换行分段」（默认开启）。

        开启（默认）：语音照旧把过短的换行行并入相邻块合成，但**文字**仍按原始换行
        逐条发送（短行不会被并进上一段文字里）。
        关闭：回到旧行为——文字随语音一起用合并后的分段（短行被并入，且因合并是直接
        拼接，两行文字会连成一句）。
        """
        return bool(self.config.get("text_split_keep_newline", True))

    @staticmethod
    def _ns_spans(chunks: list) -> tuple:
        """把各段映射到「去空白字符轴」上的半开区间（跨套分段配对的公共基准）。

        语音段与文字段都切自同一份干净文本（只是切点不同），剔掉空白后的字符序列
        一致，因此用去空白后的字符位置即可判定「某文字段落在哪个语音段区间内」。
        """
        spans: list = []
        pos = 0
        for c in chunks:
            n = len(re.sub(r"\s+", "", c or ""))
            spans.append((pos, pos + n))
            pos += n
        return spans, pos

    @classmethod
    def _pair_voice_text(cls, voice_chunks: list, text_chunks: list) -> list:
        """把文字段归入「其起点所在的语音段」，返回 ``[(语音段, [文字段...]), ...]``。

        两套分段的去空白总长必须一致才配对；不一致说明切分有偏差（理论上不该发生），
        直接退化为一一对应——宁可回到旧行为，也不让文字丢段或错位。
        """
        vspans, vtotal = cls._ns_spans(voice_chunks)
        tspans, ttotal = cls._ns_spans(text_chunks)
        if vtotal <= 0 or vtotal != ttotal:
            return [(c, [c]) for c in voice_chunks]
        groups: list = []
        ti = 0
        for i, (_vs, ve) in enumerate(vspans):
            members: list = []
            while ti < len(text_chunks) and tspans[ti][0] < ve:
                members.append(text_chunks[ti])
                ti += 1
            groups.append((voice_chunks[i], members or [voice_chunks[i]]))
        # 兜底：尾部未能归属的文字段并入最后一组，保证文字不丢
        while ti < len(text_chunks):
            v, members = groups[-1]
            groups[-1] = (v, members + [text_chunks[ti]])
            ti += 1
        return groups

    def split_newline_blocks(self, text: str) -> list:
        """按换行切块，并把过短的块并入相邻块（供 split_text 预分段与 /tts1 指令共用）。

        - 一个/连续多个换行都视为块边界（``\\n+``），逐块 strip、去空；
        - 块字数 < ``newline_min_len`` 时与相邻块合并（首块并入后一块、末块并入前一块），
          避免「嗯」「好的」这类极短行单独成段、单独发一条语音；
        - 合并结果受单段硬上限 max_text_len 约束（0=不限制）：否则长块会不断吸附短块
          滚成远超配置的超长段（与 _merge_short 的既有约束一致）。
        """
        blocks = [b.strip() for b in re.split(r"\n+", text or "") if b.strip()]
        if not blocks:
            return []
        return self._merge_short(blocks, self._newline_min_len(), self._seg_hard_cap())

    def _newline_min_len(self) -> int:
        """换行分块的短块合并下限（配置 newline_min_len，默认 10 字；0/非法 = 不合并）。"""
        try:
            return max(0, int(float(self.config.get("newline_min_len", 10) or 0)))
        except (TypeError, ValueError):
            return 10

    def _split_window(self, text: str) -> list:
        """按标点窗口对单段文本分段（被 split_text 调用，可能按行多次调用）。"""
        text = (text or "").strip()
        if not text:
            return []

        lo, hi = self._seg_window()
        if hi <= 0:
            # 分段关闭：回退到旧 max_text_len 逻辑（按句切 + 超长硬切）
            chunks = self._legacy_split(text)
        else:
            # 新分段逻辑：窗口上界 = hi，首段可用独立的 segment_first_len 区间。
            # 在 [start, start+hi) 内命中的最后一个分段符号处切；min(lo) 作为短段合并下限。
            # max_text_len 不参与窗口，仅作 truncate 模式下「无标点硬切」的兜底上限。
            # 注意：本方法只应收到【干净文本】（副语言标记在分段之后才逐段注入），
            # 所以窗口/硬切直接用字符数即可，不存在「标记挤爆字数窗口」的问题。
            hard_cap = self._seg_hard_cap()
            punct = self._seg_punct_class()
            punct_re = re.compile(punct)
            n = len(text)
            chunks: list = []
            start = 0
            is_first = True
            while start < n:
                fw_lo, fw_hi = (self._seg_first_window() if is_first else (lo, hi))
                end = min(start + fw_hi, n)
                # 在 [start, end) 窗口内找【最后一个】命中的分段符号：
                # 取窗口内字数范围内最后一个标点，以它前面（含符号）为一段。
                last = None
                for m in punct_re.finditer(text, start, end):
                    last = m
                if last is not None:
                    cut = last.end()
                    seg = text[start:cut].strip()
                    if seg:
                        chunks.append(seg)
                    start = cut
                else:
                    mode = self._seg_nopunct_mode("first" if is_first else "body")
                    # seek 模式受 hard_cap 约束：剩余长度或 seek 目标超出硬上限时放弃 seek，
                    # 退回 truncate 硬切——否则无标点长文（错误信息/英文等）会 seek 出
                    # 远超配置窗口的超长段
                    if mode == "seek" and not (hard_cap > 0 and n - start > hard_cap):
                        # 窗口内无标点：往后（超出窗口）找第一个命中的分段符号
                        nxt = punct_re.search(text, end)
                        if nxt is not None and not (hard_cap > 0 and nxt.end() - start > hard_cap):
                            cut = nxt.end()
                            seg = text[start:cut].strip()
                            if seg:
                                chunks.append(seg)
                            start = cut
                        else:
                            # 到结尾都无标点：剩余整段作为一段
                            seg = text[start:n].strip()
                            if seg:
                                chunks.append(seg)
                            start = n
                    else:
                        # truncate：窗口内无标点，直接在窗口末（受 hard_cap 兜底）硬切
                        cut = min(end, start + hard_cap) if hard_cap > 0 else end
                        seg = text[start:cut].strip()
                        if seg:
                            chunks.append(seg)
                        start = cut
                is_first = False

        # 短段合并：任何 < lo 字的段并入相邻段（优先并入前一段，首段并入后一段）。
        # lo=0 时退化为不合并（保持原分段）。合并结果受窗口上界 hi 约束——
        # 否则长段吸附短段越滚越大，滚出远超配置窗口的超长段。同时剔除空段。
        return self._merge_short(chunks, lo, hi)

    def _split_by_newline(self) -> bool:
        """配置项 split_by_newline：是否优先按换行预分段（默认开启，QQ 多行消息用）。"""
        return bool(self.config.get("split_by_newline", True))

    @staticmethod
    def _merge_broken_markup(chunks: list) -> list:
        """把被段边界切开的副语言标记接回完整（段尾未闭合的标记并入下一段）。

        分段是纯字符/标点驱动的，不感知标记边界：长句硬切（max_text_len）与分段窗口
        （segment_len / segment_first_len 的 truncate 硬切）都可能正好落在
        `[quick_breath]` 中间，得到 `...好呀。[quick_` + `breath]然后...` 两段。
        服务端只认**完整**的 special token，残片会被当普通文本念出来（用户听到的就是
        「breath」这类英文单词），所以这里必须把残片与下一段拼回去。

        判定用「白名单标记前缀」而非「有 `[` 就拼」：模型正文里未配对的 `[`（如
        「温度[摄氏」）不该被误改，只有 `[quick_`（`quick_breath` 的前缀）这类才处理。
        """
        out: list = []
        for c in chunks:
            if out and _has_open_markup_tail(out[-1]):
                out[-1] = out[-1] + c
            else:
                out.append(c)
        return out

    @staticmethod
    def _absorb_lone_markup(chunks: list) -> list:
        """把「整段只有副语言标记」的段并入前一段（首段则丢弃）。

        根因（用户看到的「多余的 1 秒空白语音」）：自动换气标记插在句末标点**之后**
        （`A。` → `A。[breath]`），而分段总是在「窗口内最后一个分段符号」处切——
        最后一句被上一段收走后，剩余的就只有尾随标记，被切成一个只有 `[breath]` 的段。
        它含字母、能通过 is_speakable，被送去合成后服务端产出约 1 秒换气声；
        逐段发送（segment_merge=false）模式下就是一条独立的空白语音。
        处理：并入前一段（换气声跟随前一句音频结尾，听感自然）；首段就是纯标记时
        没有可并的对象，直接丢弃（开头的换气没有意义）。
        """
        out: list = []
        for c in chunks:
            if _MARKUP_ONLY_RE.match(c or ""):
                if out:
                    out[-1] = out[-1] + c
                continue
            out.append(c)
        return out

    @staticmethod
    def _merge_short(chunks: list, min_len: int, max_len: int = 0) -> list:
        """把 < min_len 字的短段并入相邻段，避免「哈哈。」这类超短句单独成段。

        max_len > 0 时为合并结果设上限：并入会使结果超过 max_len 则放弃合并、保持独立段。
        否则长段会不断吸附 <min_len 的短段越滚越大，滚出远超配置窗口的 100+ 字超长段
        （用户配置每段 50/60 却出现 100+ 字语音任务的根因）。
        长度按纯文本字符数计算——本函数只处理【干净文本】（标记在分段之后才注入）。
        """
        if min_len <= 0:
            return [c for c in chunks if c]
        cap = max_len if max_len and max_len > 0 else 0
        out: list = []
        for c in chunks:
            c = (c or "").strip()
            if not c:
                continue
            if not out:
                # 首段直接保留；偏短时等下一段合并（由下方 prev < min_len 分支处理）
                out.append(c)
            else:
                prev = out[-1]
                if (len(prev) < min_len or len(c) < min_len) and (
                    cap <= 0 or len(prev) + len(c) <= cap
                ):
                    # 前一段或本段偏短：合并，避免「哈哈。」这类孤立短段（受 cap 上限约束）
                    out[-1] = prev + c
                else:
                    out.append(c)
        # 若末段仍短于 min_len（后面没段可并），并入前一段（同样受 cap 上限约束）
        if len(out) >= 2 and len(out[-1]) < min_len:
            if cap <= 0 or len(out[-2]) + len(out[-1]) <= cap:
                out[-2] = out[-2] + out[-1]
                out.pop()
        return [c for c in out if c]

    def _legacy_split(self, text: str) -> list:
        """旧分段逻辑（segment_len 关闭时回退）：按句边界切，超 max_text_len 则硬切。

        只处理【干净文本】，故长度即字符数。
        """
        cap = self._legacy_window()
        if cap <= 0:
            return [text]
        chunks: list = []
        buf = ""
        for seg in re.split(r"(?<=[。！？!?\n])", text):
            seg = seg.strip()
            if not seg:
                continue
            if len(buf) + len(seg) <= cap:
                buf += seg
            else:
                if buf:
                    chunks.append(buf)
                if len(seg) > cap:
                    for i in range(0, len(seg), cap):
                        chunks.append(seg[i : i + cap])
                    buf = ""
                else:
                    buf = seg
        if buf:
            chunks.append(buf)
        return chunks

    # ---------- 合成 ----------
    async def _maybe_translate(self, text: str, voice_lang: str | None = None) -> str:
        """合成前按需翻译：把文本翻成目标语种（默认全局 target；传入 voice_lang 时
        翻成该音色语种，实现「中文 → 音色语种」）。任何异常都回退原文。

        voice_lang：所选音色的语种（来自 voices 配置的 language 字段）。
        """
        if self.translator is None:
            return text
        try:
            return await self.translator.maybe_translate(text, target=voice_lang)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[cosyvoice] 翻译接入异常，回退原文: {e}")
            return text

    async def translate_for_voice(self, text: str, voice_name: str | None = None) -> str:
        """按音色语种翻译【纯文本】（翻译只作用于干净文本，标记在分段/翻译之后才注入）。

        顺序很关键：标记必须在翻译**之后**注入。若先注入再翻译（早期指令/工具路径
        就是这样：注入标记后调用未带 pre_translated 的 synthesize），整段（含
        `[breath]` 这类标记）会被送进翻译接口——标记可能被改写/翻译/删除，服务端
        认不出就成了普通文本被念出来。
        """
        return await self._maybe_translate(text, voice_lang=self.voice_language(voice_name))

    def voice_language(self, voice_name: str | None = None) -> str | None:
        """返回所选音色的语种（用于翻译目标语种判定），无可用音色返回 None。"""
        name, _, _ = self.resolve_voice(voice_name)
        if name is None:
            return None
        return (self.voices.get(name) or {}).get("language") or None

    def _inject(self, seg: str, voice_name: str | None) -> str:
        """为一【段】文本注入副语言标记（分段之后调用）。

        统一入口的意义：① 分段只吃干净文本，语音与文字用同一套边界；
        ② 标记不影响分段字数，不会把 48 字的正文撑成 98 字被窗口误切；
        ③ 音色级 `markup=false` 开关按**实际生效的音色**判定（调用方传的 voice 可能
           为 None=用默认音色，此前会让该开关失效）。
        """
        name, _, _ = self.resolve_voice(voice_name)
        if name is None:
            return seg
        vlang = (self.voices.get(name) or {}).get("language") or None
        return inject_markup(seg, vlang, self.config, voice=self.voices.get(name))

    async def synthesize(self, text: str, voice_name: str | None = None, *, pre_translated: bool = False) -> str | None:
        """合成文本并返回 wav 文件路径；无可用音色或失败返回 None。

        ``text`` 必须是【干净文本】（不含副语言标记）：本方法在干净文本上分段，
        再对每段调用 `_inject` 注入标记后才请求服务端。
        """
        self.last_failure = ""
        self.last_failure_kind = ""
        name, prompt_wav, prompt_text = self.resolve_voice(voice_name)
        if name is None:
            self.last_failure = "未配置任何可用音色（请在插件配置/WebUI 里选一个可用音色）"
            self.last_failure_kind = "config"
            logger.warning(
                f"[cosyvoice] 未配置任何可用音色，跳过语音合成。"
                f"运行实例读到的 raw voices={repr(self.config.get('voices'))[:300]}"
            )
            return None
        # 先按音色语种翻译（中文文本 + 外语音色 → 翻成该语种），再分段合成
        vlang = (self.voices.get(name) or {}).get("language") or None
        if not pre_translated:
            text = await self._maybe_translate(text, voice_lang=vlang)

        chunks = [c for c in self.split_text(text) if is_speakable(c)]
        if not chunks:
            self.last_failure = f"无有效可合成文本（被过滤为空白/纯符号）：{text!r}"
            self.last_failure_kind = "content"
            # 提升到 WARNING：否则默认日志级别下这行被吞，WebUI 只会显示「无有效音频/纯符号」兜底文案，难以定位真因
            logger.warning(
                f"[cosyvoice] 无有效可合成文本，跳过语音合成。"
                f"原始文本={text!r} | voice={name!r} language={vlang!r}"
            )
            return None

        try:
            pcms = []
            kwargs = self._wav_kwargs(prompt_wav, prompt_text)
            total = len(chunks)
            for i, ch in enumerate(chunks, 1):
                # 标记在分段之后逐段注入（分段用的是干净文本，与文字分段同一套边界）
                voiced = self._inject(ch, name)
                t0 = time.time()
                logger.info(
                    f"[cosyvoice] 合成 {i}/{total}: \"{voiced[:40]}{'...' if len(voiced)>40 else ''}\""
                )
                async with self._sem:
                    # 整轮只有第一段做「排队过长」判定（探路）：通过则后续段照常合成，
                    # 避免「前半段发出、后半段因排队被弃」的半截语音（合并模式同理）。
                    pcm = await self.client.synthesize(
                        voiced, mode="zero_shot", check_queue=(i == 1), **kwargs
                    )
                if pcm:
                    dt = (time.time() - t0) * 1000
                    logger.info(f"[cosyvoice] 合成 {i}/{total} OK | {dt:.0f}ms {len(pcm)}字节PCM")
                    pcms.append(pcm)
            if not pcms:
                self.last_failure = "所有分段合成均返回空音频（很可能音色参考音频 prompt_wav 路径不对或文件缺失，服务端拒绝零样本合成）"
                self.last_failure_kind = "server"
                logger.warning("[cosyvoice] 合并合成完成：0 段成功，无有效音频")
                return None
            combined = b"".join(pcms)
            logger.info(f"[cosyvoice] 合并合成完成：{len(pcms)}/{total} 段成功，总PCM {len(combined)}字节")
            return audio.pcm_to_wav_file(combined, self.client.sample_rate, self.client.cache_dir)
        except CosyVoiceServerError:
            # 服务器失联是「环境故障」而非「内容问题」，向上抛给调用方给出专门提示
            raise
        except QueueFullError:
            # 服务器繁忙（排队过长）：与失联同属「环境暂不可用」，同样向上抛，
            # 由插件层给出「繁忙稍后再试」的专门提示，而不是按普通失败吞掉
            raise
        except Exception as e:  # noqa: BLE001
            self.last_failure = f"语音合成异常：{e}"
            self.last_failure_kind = "server"
            logger.error(f"[cosyvoice] 语音合成失败: {e}")
            return None

    async def iter_segment_items(self, text: str, voice_name: str | None = None, *, pre_translated: bool = False):
        """逐段合成，依次 yield (段文字, wav路径或None)。

        用于「不合并」模式：每段生成完即可发给用户，无需等全部完成。
        - 合成成功：yield (段文字, wav路径)；段文字为【干净文本】（不含标记，标记
          在分段后逐段注入，仅进合成、不进展示）；
        - 单段推理失败：yield (ch, None)，**文字仍返回**，由调用方保证文字不丢
          （语音缺段但文字完整）。
        服务器失联（CosyVoiceServerError）/ 繁忙（QueueFullError）会向上抛出。

        ``text`` 必须是【干净文本】：分段在干净文本上做，与文字分段同一套边界。
        """
        name, prompt_wav, prompt_text = self.resolve_voice(voice_name)
        if name is None:
            logger.warning(
                f"[cosyvoice] 未配置任何可用音色，跳过语音合成。"
                f"运行实例读到的 raw voices={repr(self.config.get('voices'))[:300]}"
            )
            return
        # 不合并模式同样先按音色语种翻译，再分段合成（保证逐段发送也是译后文本）
        vlang = (self.voices.get(name) or {}).get("language") or None
        if not pre_translated:
            text = await self._maybe_translate(text, voice_lang=vlang)
        chunks = [c for c in self.split_text(text) if is_speakable(c)]
        if not chunks:
            logger.debug("[cosyvoice] 无有效可合成文本，跳过语音合成")
            return
        kwargs = self._wav_kwargs(prompt_wav, prompt_text)
        total = len(chunks)
        for i, ch in enumerate(chunks, 1):
            try:
                # 标记在分段之后逐段注入（分段用的是干净文本，与文字分段同一套边界）
                voiced = self._inject(ch, name)
                t0 = time.time()
                logger.info(
                    f"[cosyvoice] 合成 {i}/{total}: \"{voiced[:40]}{'...' if len(voiced)>40 else ''}\""
                )
                async with self._sem:
                    # 整轮只有第一段做「排队过长」判定（探路）：通过则后续段照常合成，
                    # 避免「前半段发出、后半段因排队被弃」的半截语音（不合并模式）。
                    pcm = await self.client.synthesize(
                        voiced, mode="zero_shot", check_queue=(i == 1), **kwargs
                    )
            except CosyVoiceServerError:
                raise
            except QueueFullError:
                # 繁忙（排队过长）同样视为「环境暂不可用」，中断逐段循环向上抛，
                # 避免后面每段都白等一次排队、拖到超时
                raise
            except Exception as e:  # noqa: BLE001
                logger.error(f"[cosyvoice] 单段合成失败已跳过: {e}")
                yield ch, None
                continue
            if pcm:
                dt = (time.time() - t0) * 1000
                logger.info(f"[cosyvoice] 合成 {i}/{total} OK | {dt:.0f}ms {len(pcm)}字节PCM")
                yield ch, audio.pcm_to_wav_file(pcm, self.client.sample_rate, self.client.cache_dir)
            else:
                yield ch, None

    async def iter_segment_wavs(self, text: str, voice_name: str | None = None, *, pre_translated: bool = False):
        """逐段合成，依次 yield 每段生成的临时 wav 文件路径（忽略失败段）。

        用于「不合并」模式下只需语音、无需逐段文字的路径（voice_only / 手动指令）。
        """
        async for _ch, wav in self.iter_segment_items(text, voice_name, pre_translated=pre_translated):
            if wav:
                yield wav
