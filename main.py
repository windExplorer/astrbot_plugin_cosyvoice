"""AstrBot CosyVoice3 语音合成插件入口。

接入本地自建 CosyVoice3 推理服务（官方 QwenAudio/CosyVoice 的 fastapi/server.py），
让机器人以可配置音色朗读回复。支持三种触发方式：
  1) 自动（auto_tts=true 时对符合条件的回复自动合成语音）
  2) 关键词（用户消息含触发词，需 enable_user_trigger）
  3) LLM 工具 text_to_speech / /tts 指令

硬约束：文本绝不因发语音而丢失上下文。默认 send_mode=both（文本+语音都发），
语音仅作为 Record 追加到结果链，原文 Plain 始终保留；LLM 的 completion_text 也由
AstrBot 单独存入会话历史，因此记忆插件与大模型下一轮都能拿到文字。
"""

import re

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

try:  # 仅用于类型标注，缺失也不影响运行
    from astrbot.api.provider import LLMResponse
except Exception:  # noqa: BLE001
    LLMResponse = object  # type: ignore

from .core.tts_engine import TtsEngine
from .cosyvoice.client import CosyVoiceClient
from .utils import audio

PLUGIN_ID = "astrbot_plugin_cosyvoice"


@register(PLUGIN_ID, "Yours", "接入本地 CosyVoice3，让机器人以可配置音色朗读回复", "1.0.0")
class CosyVoicePlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        # AstrBot 在实例化插件时通过 __init__ 注入完整配置（含 template_list 类型）。
        # 部分版本也保留 context.get_config()，这里做兼容：优先用注入配置，回退到 get_config()。
        self._injected_config = config if config is not None else (self.context.get_config() or {})
        self.config = self._injected_config
        self.client = CosyVoiceClient(
            base_url=self.config.get("base_url", "http://127.0.0.1:50000"),
            sample_rate=int(self.config.get("sample_rate", 24000)),
            timeout=int(self.config.get("timeout", 60)),
        )
        self.engine = TtsEngine(self.config, self.client)
        # 每个消息的事件标记（避免并发串台），以 message_id 为键
        self._flags: dict = {}

    async def initialize(self):
        cfg = self._refresh_cfg()
        logger.info(
            f"[cosyvoice] 初始化配置 keys={list(cfg.keys())} "
            f"voices_type={type(cfg.get('voices')).__name__} "
            f"voices_count={len(cfg.get('voices') or [])}"
        )

    # ---------- 事件标记辅助 ----------
    def _key(self, event: AstrMessageEvent):
        return getattr(event, "message_id", None) or event.unified_msg_origin

    def _set_flag(self, event: AstrMessageEvent, k: str, v=True):
        self._flags.setdefault(self._key(event), {})[k] = v

    def _get_flag(self, event: AstrMessageEvent, k: str, default=False):
        return self._flags.get(self._key(event), {}).get(k, default)

    def _clear(self, event: AstrMessageEvent):
        self._flags.pop(self._key(event), None)

    # ---------- 工具方法 ----------
    def _refresh_cfg(self) -> dict:
        live = self.context.get_config() or {}
        # 以注入的完整配置为基线，再用 get_config() 的实时值覆盖。
        # 这样即使 get_config() 不返回 template_list/默认值字段，也不会丢失已配置的音色。
        merged = dict(self._injected_config)
        merged.update({k: v for k, v in live.items() if v is not None})
        self.config = merged
        self.engine.config = merged
        self.engine.update_voices(merged.get("voices") or {})
        return merged

    def _in_scope(self, event: AstrMessageEvent, cfg: dict) -> bool:
        """根据 blocklist/allowlist 判断该会话是否允许转语音。"""
        origin = event.unified_msg_origin
        sender = event.get_sender_id()
        blocklist = cfg.get("blocklist", []) or []
        if any(x in (origin, sender) for x in blocklist):
            return False
        allowlist = cfg.get("allowlist", []) or []
        if allowlist and not any(x in (origin, sender) for x in allowlist):
            return False
        return True

    def _should_tts(self, event: AstrMessageEvent, cfg: dict) -> bool:
        if not self._in_scope(event, cfg):
            return False
        is_llm = self._get_flag(event, "is_llm", False)
        want = self._get_flag(event, "want", False)
        auto = bool(cfg.get("auto_tts", False))
        if cfg.get("tts_scope", "llm_only") == "llm_only":
            return bool(is_llm and (auto or want))
        # all_text：自动开启则全部；否则仅关键词/工具触发
        return bool(auto or want)

    # ---------- LLM 回复钩子：标记 + 关键词触发 ----------
    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, resp: LLMResponse):
        self._refresh_cfg()
        self._set_flag(event, "is_llm", True)

        cfg = self.config

        # 关键词触发
        if cfg.get("enable_user_trigger", True):
            msg = event.message_str or ""
            keywords = cfg.get("trigger_keywords", []) or []
            if any(kw and kw in msg for kw in keywords):
                self._set_flag(event, "want", True)

        # 自动语音
        if cfg.get("auto_tts", False):
            self._set_flag(event, "want", True)

    # ---------- 装饰结果钩子：追加语音到结果链 ----------
    @filter.on_decorating_result()
    async def on_decorating_result(self, event: AstrMessageEvent):
        cfg = self._refresh_cfg()
        key = self._key(event)

        # 本插件的 /tts 指令或 LLM 工具已自行发送语音，避免重复
        if self._get_flag(event, "suppress", False):
            self._clear(event)
            return

        if not self._should_tts(event, cfg):
            return

        result = event.get_result()
        chain = result.chain
        if not chain:
            return

        # 抽取纯文本
        texts = [
            getattr(c, "text", "")
            for c in chain
            if isinstance(c, Comp.Plain) and getattr(c, "text", "")
        ]
        full_text = "".join(texts).strip()
        if not full_text:
            return

        # 已含语音则不重复
        if any(isinstance(c, Comp.Record) for c in chain):
            return

        voice = self._get_flag(event, "voice", None)
        path = await self.engine.synthesize(full_text, voice)
        self._clear(event)
        if not path:
            logger.warning("[cosyvoice] 合成失败，仅发送文本")
            return

        record = Comp.Record(file=path, url=path)
        audio.schedule_cleanup(path)
        send_mode = cfg.get("send_mode", "both")
        if send_mode == "voice_only":
            # 仅发语音：从发送链移除原文，但 LLM 的 completion_text 已由 AstrBot
            # 单独存入会话历史，文字不会丢失（both 模式天然满足，voice_only 依赖此机制）。
            result.chain = [c for c in chain if not isinstance(c, Comp.Plain)] + [record]
        else:
            chain.append(record)

    # ---------- 用户指令：/tts <文本> ----------
    @filter.command("tts")
    async def tts_cmd(self, event: AstrMessageEvent):
        self._refresh_cfg()
        raw = (event.message_str or "").strip()
        text = re.sub(r"^/tts\b", "", raw, flags=re.IGNORECASE).strip()
        if not text:
            yield event.plain_result("用法：/tts <要朗读的文本>")
            return

        self._set_flag(event, "suppress", True)
        path = await self.engine.synthesize(text)
        if not path:
            yield event.plain_result("语音合成失败了，请稍后再试，或检查语音服务配置。")
            return

        if self.config.get("send_mode", "both") == "both":
            yield event.chain_result([Comp.Plain(text), Comp.Record(file=path, url=path)])
        else:
            yield event.chain_result([Comp.Record(file=path, url=path)])
        audio.schedule_cleanup(path)

    # ---------- 用户指令：/tts_voice <音色名> ----------
    @filter.command("tts_voice")
    async def tts_voice_cmd(self, event: AstrMessageEvent):
        self._refresh_cfg()
        raw = (event.message_str or "").strip()
        name = re.sub(r"^/tts_voice\b", "", raw, flags=re.IGNORECASE).strip()
        voices = self.engine.list_voices()
        if not voices:
            yield event.plain_result("尚未配置可用音色，请先在插件设置里添加音色。")
            return
        if not name or name not in voices:
            yield event.plain_result(f"可用音色：{', '.join(voices)}")
            return
        self.config["default_voice"] = name
        self.engine.config = self.config
        yield event.plain_result(f"已切换默认音色为：{name}（重启插件后失效，建议改配置文件）")

    # ---------- LLM 函数调用工具 ----------
    @filter.llm_tool(name="text_to_speech")
    async def text_to_speech_tool(self, event: AstrMessageEvent, text: str, voice: str = ""):
        """将指定文本转换为语音并朗读出来。当用户希望机器人用语音回复、或需要把某段内容念出来时使用。

        Args:
            text(string): 需要朗读的文本内容
            voice(string): 可选，指定音色名；留空则使用默认音色
        """
        self._refresh_cfg()
        if not self.config.get("enable_llm_tool", True):
            yield event.plain_result("语音工具当前未启用。")
            return

        self._set_flag(event, "suppress", True)
        if voice:
            self._set_flag(event, "voice", voice)

        if not text or not text.strip():
            yield event.plain_result("未提供需要朗读的文本。")
            return

        path = await self.engine.synthesize(text.strip(), voice or None)
        if not path:
            yield event.plain_result("语音合成失败了，请稍后再试。")
            return

        if self.config.get("send_mode", "both") == "both":
            yield event.chain_result([Comp.Plain(text), Comp.Record(file=path, url=path)])
        else:
            yield event.chain_result([Comp.Record(file=path, url=path)])
        audio.schedule_cleanup(path)

    async def terminate(self):
        self._flags.clear()
