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
        """根据配置决定参考音频的传递方式：

        - 配置了 server_voices_dir 且本地找不到该文件 → 走服务端本地路径（prompt_wav_path），
          不占用带宽上传大文件；
        - 否则 → 走 AstrBot 服务端本地文件上传（prompt_wav）。
        """
        server_dir = (self.config.get("server_voices_dir") or "").strip()
        if server_dir and prompt_wav and not os.path.exists(self.resolve_wav(prompt_wav)):
            # 仅在 CosyVoice 服务端放好的参考音频：只传文件名，服务端自己读
            return {"prompt_wav_path": prompt_wav, "prompt_text": prompt_text}
        return {"prompt_wav": self.resolve_wav(prompt_wav), "prompt_text": prompt_text}

    # ---------- 音色解析 ----------
    def update_voices(self, voices):
        """归一化音色配置为内部 dict：{ 音色名: {prompt_wav, prompt_text} }。

        兼容三种来源：
        - template_list 的 list：每项含 name/prompt_wav/prompt_text
          （AstrBot 会附带 __template_key，忽略即可）；
        - 旧式 dict：{ 音色名: {prompt_wav, prompt_text} }；
        - 历史 text 类型的 JSON 字符串（兼容旧配置）。
        """
        self.voices = self._norm_voices(voices)
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
        """分段字数窗口：优先 segment_len，其次 max_text_len 作为兜底硬上限。

        - segment_len > 0：按用户配置的分段字数（配合 segment_punct 在窗口内命中标点处切）。
        - 否则 max_text_len > 0：按旧逻辑的长度上限硬切。
        - 都未配置（<=0）：不分段，整段返回。
        """
        seg = int(self.config.get("segment_len", 0) or 0)
        if seg > 0:
            cap = int(self.config.get("max_text_len", 0) or 0)
            return min(seg, cap) if cap > 0 else seg
        return int(self.config.get("max_text_len", 0) or 0)

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
            return [text]

        punct = self._seg_punct_class()
        punct_re = re.compile(punct)
        n = len(text)
        chunks: list = []
        start = 0
        while start < n:
            end = min(start + window, n)
            # 在 [start, end) 窗口内找【最后一个】命中的分段符号：
            # 取窗口内 30 字范围内最后一个标点，以它前面（含符号）为一段。
            last = None
            for m in punct_re.finditer(text, start, end):
                last = m
            if last is not None:
                # 段 = 从 start 到最后一个命中标点（含），下一段从标点后开始
                cut = last.end()
                seg = text[start:cut].strip()
                if seg:
                    chunks.append(seg)
                start = cut
            else:
                # 窗口内无命中符号：硬切到窗口末（仍 strip 去除首尾空白/换行）
                seg = text[start:end].strip()
                if seg:
                    chunks.append(seg)
                start = end
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
