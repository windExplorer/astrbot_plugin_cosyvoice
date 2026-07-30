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
        - 新版 text 类型：JSON 字符串（在配置面板 textarea 粘贴），自动 json.loads；
        - 旧式 dict：{ 音色名: {prompt_wav, prompt_text} }
        - list（template_list）：每项含 name/prompt_wav/prompt_text
          （AstrBot 会附带 __template_key，忽略即可）
        """
        # 配置项现为 text 类型，voices 可能是用户粘贴的 JSON 字符串
        if isinstance(voices, str):
            s = voices.strip()
            if not s or s in ("{}", "[]"):
                self.voices = {}
                return
            try:
                voices = json.loads(s)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[cosyvoice] voices 配置解析失败（应为合法 JSON）：{e}")
                self.voices = {}
                return
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
        self.voices = d
        # 诊断：读到了配置却解析为 0 个，多半是子项 name(音色名) 为空
        if not d and voices:
            logger.warning(
                f"[cosyvoice] 已读到 voices 配置({type(voices).__name__})但解析出 0 个音色，"
                f"请检查每项是否填写了「音色名」。原始内容(前500字): {repr(voices)[:500]}"
            )
        elif d:
            logger.info(f"[cosyvoice] 已加载 {len(d)} 个音色: {list(d.keys())}")

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
    _SENT_SPLIT = re.compile(r"(?<=[。！？!?\n])")

    def split_text(self, text: str) -> list:
        max_len = int(self.config.get("max_text_len", 0) or 0)
        text = (text or "").strip()
        if not text:
            return []
        if max_len <= 0:
            return [text]

        chunks: list = []
        buf = ""
        for seg in self._SENT_SPLIT.split(text):
            seg = seg.strip()
            if not seg:
                continue
            if len(buf) + len(seg) <= max_len:
                buf += seg
            else:
                if buf:
                    chunks.append(buf)
                # 单句超长则硬切
                if len(seg) > max_len:
                    for i in range(0, len(seg), max_len):
                        chunks.append(seg[i : i + max_len])
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

        chunks = self.split_text(text)
        if not chunks:
            logger.debug("[cosyvoice] 文本为空，跳过语音合成")
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
