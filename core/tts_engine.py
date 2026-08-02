"""TTS 引擎：封装音色解析、长文本分片与多段拼接。"""

import os
import re
import json

from astrbot.api import logger

from ..cosyvoice.client import CosyVoiceClient, CosyVoiceServerError
from ..utils import audio

# 插件根目录（core/ 的上一级），用于解析相对参考音频路径
_PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 默认参考音频目录：插件内的 voices/ 文件夹
VOICES_DIR = os.path.join(_PLUGIN_ROOT, "voices")


def is_speakable(text: str) -> bool:
    """判断文本是否值得拿去合成语音：

    - 空 / 纯空白 → 否；
    - 占位符 ``[]`` ``{}`` ``null`` ``None`` ``nil`` ``undefined`` → 否；
    - 仅由括号 / 空白 / 引号构成（如 ``[ ]`` ``{ }``） → 否；
    - 其余（含正常中文、标点） → 是。
    """
    if not text:
        return False
    t = text.strip()
    if not t:
        return False
    if t in ("[]", "{}", "null", "None", "nil", "undefined", "null"):
        return False
    if re.fullmatch(r"[\s\[\]\{\}\(\)\"']*", t):
        return False
    return True


class TtsEngine:
    def __init__(self, config: dict, client: CosyVoiceClient):
        self.config = config
        self.client = client
        self.voices: dict = {}
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
        """把 str / dict / list 归一化为 { 音色名: {prompt_wav, prompt_text} }。"""
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
                }
        return d

    def list_voices(self) -> list:
        return sorted(self.voices.keys())

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
    def _seg_window(self) -> int:
        """分段字数窗口（仅由 segment_len 决定，与 max_text_len 解耦）。

        - segment_len > 0：按用户配置的分段字数（配合 segment_punct 在窗口内命中标点处切），
          窗口就是 segment_len 本身，不受 max_text_len 影响。
        - segment_len <= 0（关闭分段）：返回 0，由调用方回退到旧 max_text_len 逻辑。
        """
        return int(self.config.get("segment_len", 0) or 0)

    def _seg_hard_cap(self) -> int:
        """单段绝对硬上限（仅作用于「窗口内无命中符号时的硬切」）。

        由 max_text_len 决定，与 segment_len 独立：即使分段窗口很大，单段也不会超过此值，
        防止极长无标点文本把整段一次性甩给服务端。0 表示不限制。
        仅在 segment_len > 0（新分段逻辑）时作为兜底生效；旧逻辑由 _legacy_window 处理。
        """
        return int(self.config.get("max_text_len", 0) or 0)

    def _legacy_window(self) -> int:
        """旧分段逻辑窗口：仅在 segment_len 关闭（<=0）时生效，沿用 max_text_len。"""
        return int(self.config.get("max_text_len", 0) or 0)

    def _seg_first_window(self) -> int:
        """首段字数窗口：segment_first_len > 0 时用它，否则回退到普通 segment_len。"""
        first = int(self.config.get("segment_first_len", 0) or 0)
        if first > 0:
            return first
        return self._seg_window()

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
        text = (text or "").strip()
        if not text:
            return []

        window = self._seg_window()
        if window <= 0:
            # 分段关闭：回退到旧 max_text_len 逻辑（按句切 + 超长硬切）
            return self._legacy_split(text)

        # 新分段逻辑：窗口 = segment_len，首段可用独立的 segment_first_len。
        # max_text_len 不参与窗口，仅作 truncate 模式下「无标点硬切」的兜底上限。
        hard_cap = self._seg_hard_cap()
        punct = self._seg_punct_class()
        punct_re = re.compile(punct)
        n = len(text)
        chunks: list = []
        start = 0
        is_first = True
        while start < n:
            w = self._seg_first_window() if is_first else window
            end = min(start + w, n)
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
                if mode == "seek":
                    # 窗口内无标点：往后（超出窗口）找第一个命中的分段符号
                    nxt = punct_re.search(text, end)
                    if nxt is not None:
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
        return chunks

    def _legacy_split(self, text: str) -> list:
        """旧分段逻辑（segment_len 关闭时回退）：按句边界切，超 max_text_len 则硬切。"""
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
    async def synthesize(self, text: str, voice_name: str | None = None) -> str | None:
        """合成文本并返回 wav 文件路径；无可用音色或失败返回 None。"""
        name, prompt_wav, prompt_text = self.resolve_voice(voice_name)
        if name is None:
            logger.warning(
                f"[cosyvoice] 未配置任何可用音色，跳过语音合成。"
                f"运行实例读到的 raw voices={repr(self.config.get('voices'))[:300]}"
            )
            return None

        chunks = [c for c in self.split_text(text) if is_speakable(c)]
        if not chunks:
            logger.debug("[cosyvoice] 无有效可合成文本，跳过语音合成")
            return None

        try:
            pcms = []
            kwargs = self._wav_kwargs(prompt_wav, prompt_text)
            for ch in chunks:
                pcm = await self.client.synthesize(ch, mode="zero_shot", **kwargs)
                if pcm:
                    pcms.append(pcm)
            if not pcms:
                return None
            combined = b"".join(pcms)
            return audio.pcm_to_wav_file(combined, self.client.sample_rate, self.client.cache_dir)
        except CosyVoiceServerError:
            # 服务器失联是「环境故障」而非「内容问题」，向上抛给调用方给出专门提示
            raise
        except Exception as e:  # noqa: BLE001
            logger.error(f"[cosyvoice] 语音合成失败: {e}")
            return None

    async def iter_segment_wavs(self, text: str, voice_name: str | None = None):
        """逐段合成，依次 yield 每段生成的临时 wav 文件路径。

        用于「不合并」模式：每段生成完即可发给用户，无需等全部完成。
        服务器失联（CosyVoiceServerError）会向上抛出；单段推理失败则跳过该段继续。
        """
        name, prompt_wav, prompt_text = self.resolve_voice(voice_name)
        if name is None:
            logger.warning(
                f"[cosyvoice] 未配置任何可用音色，跳过语音合成。"
                f"运行实例读到的 raw voices={repr(self.config.get('voices'))[:300]}"
            )
            return
        chunks = [c for c in self.split_text(text) if is_speakable(c)]
        if not chunks:
            logger.debug("[cosyvoice] 无有效可合成文本，跳过语音合成")
            return
        kwargs = self._wav_kwargs(prompt_wav, prompt_text)
        for ch in chunks:
            try:
                pcm = await self.client.synthesize(ch, mode="zero_shot", **kwargs)
            except CosyVoiceServerError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.error(f"[cosyvoice] 单段合成失败已跳过: {e}")
                continue
            if pcm:
                yield audio.pcm_to_wav_file(pcm, self.client.sample_rate, self.client.cache_dir)
