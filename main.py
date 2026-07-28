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

import os
import re
import json

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
        # 会话级语音开关（按群持久记忆）：unified_msg_origin -> True
        data_dir = self._data_dir()
        self._session_file = os.path.join(data_dir, "tts_sessions.json")
        self._sessions = self._load_sessions()
        # 会话级音色（按群/私聊持久记忆）：unified_msg_origin -> 音色名
        self._voice_file = os.path.join(data_dir, "tts_voices.json")
        self._voices = self._load_voices()

    def _data_dir(self) -> str:
        """持久数据目录。

        优先写到插件目录之外（AstrBot 的 <root>/data/astrbot_plugin_cosyvoice/），
        避免「重载/重装插件」时插件目录被重新解压、把 data/ 一起清空导致记忆丢失。
        若该位置不可写，回退到插件目录内的 data/。
        """
        plugin_dir = os.path.dirname(os.path.abspath(__file__))
        # AstrBot 标准布局：<root>/data/plugins/<plugin_name>/  -> 取 <root>/data
        external = os.path.join(os.path.dirname(os.path.dirname(plugin_dir)), "astrbot_plugin_cosyvoice")
        for cand in (external, os.path.join(plugin_dir, "data")):
            try:
                os.makedirs(cand, exist_ok=True)
                probe = os.path.join(cand, ".writable_test")
                with open(probe, "w", encoding="utf-8") as f:
                    f.write("1")
                os.remove(probe)
                return cand
            except Exception:
                continue
        return os.path.join(plugin_dir, "data")

    async def initialize(self):
        cfg = self._refresh_cfg()
        logger.info(
            f"[cosyvoice] 初始化配置 keys={list(cfg.keys())} "
            f"voices_type={type(cfg.get('voices')).__name__} "
            f"voices_count={len(cfg.get('voices') or [])}"
        )
        logger.info(
            f"[cosyvoice] 持久数据目录={os.path.dirname(self._session_file)} "
            f"开关文件存在={os.path.exists(self._session_file)} "
            f"音色文件存在={os.path.exists(self._voice_file)}"
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

    # ---------- 会话级语音开关（按群持久记忆） ----------
    def _load_sessions(self) -> dict:
        try:
            with open(self._session_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_sessions(self):
        os.makedirs(os.path.dirname(self._session_file), exist_ok=True)
        with open(self._session_file, "w", encoding="utf-8") as f:
            json.dump(self._sessions, f, ensure_ascii=False, indent=2)

    def _session_enabled(self, event: AstrMessageEvent) -> bool:
        return bool(self._sessions.get(event.unified_msg_origin, False))

    def _load_voices(self) -> dict:
        try:
            with open(self._voice_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_voices(self):
        os.makedirs(os.path.dirname(self._voice_file), exist_ok=True)
        with open(self._voice_file, "w", encoding="utf-8") as f:
            json.dump(self._voices, f, ensure_ascii=False, indent=2)

    def _session_voice(self, event: AstrMessageEvent) -> str | None:
        return self._voices.get(event.unified_msg_origin)

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
        session_on = self._session_enabled(event)
        if cfg.get("tts_scope", "llm_only") == "llm_only":
            return bool(is_llm and (auto or want or session_on))
        # all_text：自动开启则全部；否则仅关键词/工具触发或本会话已开
        return bool(auto or want or session_on)

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

        voice = self._get_flag(event, "voice", None) or self._session_voice(event)
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
        # 去掉 /tts 命令前缀：兼容 AstrBot 是否已自动剥离命令词，以及带空格/@提及等情况
        text = re.sub(r"^[/\s@]*tts\b\s*", "", raw, flags=re.IGNORECASE).strip()
        if not text:
            yield event.plain_result("试试这样：/tts 后面跟上你想让我念的话～")
            return

        self._set_flag(event, "suppress", True)
        path = await self.engine.synthesize(text, self._session_voice(event))
        if not path:
            yield event.plain_result("哎呀，话到嘴边卡壳了，这次没念出来，稍后再试试？")
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
        # 去掉 /tts_voice 命令前缀（同上，兼容已剥离/未剥离与 @提及）
        name = re.sub(r"^[/\s@]*tts_voice\b\s*", "", raw, flags=re.IGNORECASE).strip()
        voices = self.engine.list_voices()
        if not voices:
            yield event.plain_result("我还没拿到能用的嗓音，暂时开不了口～ 先给我安排一个音色吧。")
            return
        if not name or name not in voices:
            cur = self._session_voice(event)
            cur_hint = f"（当前聊天用的是「{cur}」）" if cur else ""
            yield event.plain_result(
                f"我现在会这些嗓音：{', '.join(voices)}（用 /tts_voice 名字 就能换）{cur_hint}"
            )
            return
        # 按当前会话持久记录音色，不影响其他聊天
        origin = event.unified_msg_origin
        self._voices[origin] = name
        self._save_voices()
        yield event.plain_result(
            f"好嘞，这个聊天以后都用「{name}」这个嗓音啦～"
        )

    # ---------- 会话级开关：/tts_on 当前群一直语音，/tts_off 关闭 ----------
    @filter.command("tts_on")
    async def tts_on_cmd(self, event: AstrMessageEvent):
        self._refresh_cfg()
        origin = event.unified_msg_origin
        self._sessions[origin] = True
        self._save_sessions()
        yield event.plain_result(
            "收到～ 从这个聊天开始，我每句回复都给你念出来啦（想静音就发 /tts_off）。"
        )

    @filter.command("tts_off")
    async def tts_off_cmd(self, event: AstrMessageEvent):
        self._refresh_cfg()
        origin = event.unified_msg_origin
        if self._sessions.pop(origin, None) is not None:
            self._save_sessions()
            yield event.plain_result("好嘞，这个聊天我不自动念了，有需要随时 /tts_on 喊我。")
        else:
            yield event.plain_result("这个聊天本来就没开着自动语音呀～")

    # ---------- 查看当前聊天语音开关状态（不暴露敏感信息） ----------
    @filter.command("tts_status")
    async def tts_status_cmd(self, event: AstrMessageEvent):
        self._refresh_cfg()
        on = self._session_enabled(event)
        session_voice = self._session_voice(event)
        default_voice = self.config.get("default_voice") or ""
        lines = [
            f"自动语音开关：{'已开启 🔊' if on else '未开启 🔇'}",
            f"当前聊天音色：{session_voice or '（未单独设置）'}",
            f"全局默认音色：{default_voice or '（未配置）'}",
        ]
        yield event.plain_result("\n".join(lines))

    # ---------- LLM 函数调用工具 ----------
    @filter.llm_tool(name="text_to_speech")
    async def text_to_speech_tool(self, event: AstrMessageEvent, text: str, voice: str = ""):
        """将指定文本转换为语音并朗读出来（临时念一次，不开启长期语音模式）。
        仅在用户明确给出要朗读的具体文本（如「把这段话念出来 / 朗读以下内容」）时使用。
        注意：若用户想「长期用语音交流」，请调用 set_voice_mode，不要调本工具。

        Args:
            text(string): 需要朗读的文本内容
            voice(string): 可选，指定音色名；留空则使用默认音色
        """
        self._refresh_cfg()
        if not self.config.get("enable_llm_tool", True):
            yield event.plain_result("语音功能还没开呢，先把它打开我就能念啦～")
            return

        self._set_flag(event, "suppress", True)
        if voice:
            self._set_flag(event, "voice", voice)

        if not text or not text.strip():
            yield event.plain_result("你想让我念点啥呀？把文字发给我就行～")
            return

        target_voice = voice or self._session_voice(event)
        path = await self.engine.synthesize(text.strip(), target_voice)
        if not path:
            yield event.plain_result("话到嘴边卡壳了，这次没念出来，待会儿再试试？")
            return

        if self.config.get("send_mode", "both") == "both":
            yield event.chain_result([Comp.Plain(text), Comp.Record(file=path, url=path)])
        else:
            yield event.chain_result([Comp.Record(file=path, url=path)])
        audio.schedule_cleanup(path)

    # ---------- LLM 工具：自然语言开关当前会话语音模式 ----------
    @filter.llm_tool(name="set_voice_mode")
    async def set_voice_mode_tool(self, event: AstrMessageEvent, on: bool, reason: str = ""):
        """开启或关闭「当前会话（群聊或私聊均可）」的长期自动语音模式，按会话独立记忆、重启不丢。这是用户想「长期用语音交流」时唯一应调用的工具。
        当用户表达『从此以后我都会发语音消息 / 以后都用语音回复 / 一直用语音跟我说话』时调用 on=true；
        当用户表达『以后不用发语音了 / 用文字回复就行 / 别念了 / 关掉语音』时调用 on=false。
        注意：本工具只切换开关、不朗读内容；开启后模型正常用文字回复即可，插件会自动合成语音。

        Args:
            on(bool): true=开启当前会话自动语音；false=关闭
            reason(string): 可选，简要说明
        """
        self._refresh_cfg()
        origin = event.unified_msg_origin
        if on:
            self._sessions[origin] = True
        else:
            self._sessions.pop(origin, None)
        self._save_sessions()
        if on:
            return "好嘞，从这个聊天开始我都会发语音消息啦～"
        return "没问题，这个聊天我以后就用文字回复啦～"

    # ---------- LLM 工具：切换当前会话音色（持久） ----------
    @filter.llm_tool(name="set_voice")
    async def set_voice_tool(self, event: AstrMessageEvent, name: str):
        """切换「当前会话（群聊或私聊均可）」长期使用的音色，按会话独立记忆、重启不丢。当用户说「以后用 XX 的声音 / 换成 XX 音色 / 用小明的嗓音」时调用。
        注意：本工具只切换音色、不朗读内容；切换后该聊天之后的语音都用这个新音色。
        不确定有哪些音色时，先调用 list_voices 查看可用名称。

        Args:
            name(string): 目标音色名（必须是 list_voices 返回列表中的某个名字）
        """
        self._refresh_cfg()
        if not self.config.get("enable_llm_tool", True):
            return "语音功能还没开呢，先把它打开我就能换音色啦～"
        voices = self.engine.list_voices()
        if not name or name not in voices:
            return f"没有「{name}」这个音色，可用音色有：{', '.join(voices)}"
        self._voices[event.unified_msg_origin] = name
        self._save_voices()
        return f"好嘞，这个聊天以后都用「{name}」这个嗓音啦～"

    # ---------- LLM 工具：列出可用音色 ----------
    @filter.llm_tool(name="list_voices")
    async def list_voices_tool(self, event: AstrMessageEvent):
        """列出当前所有可用音色名称。当用户问「你有哪些声音 / 能换成什么音色 / 都有什么嗓音 / 可以念成谁的声音」时调用，方便用户挑选后配合 set_voice 切换。

        Args:
            无
        """
        self._refresh_cfg()
        voices = self.engine.list_voices()
        if not voices:
            return "我暂时还没有可用的音色，请先配置参考音频。"
        return "当前可用音色：" + "、".join(voices)

    async def terminate(self):
        self._flags.clear()
