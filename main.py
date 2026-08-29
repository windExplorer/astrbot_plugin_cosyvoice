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
import time
import random
import asyncio

import astrbot.api.message_components as Comp
# MessageChain 不在 message_components 模块中（各 AstrBot 版本位置不同），做兼容性导入
try:
    from astrbot.api.all import MessageChain
except ImportError:  # noqa: BLE001
    try:
        from astrbot.core.message.message_event_result import MessageChain
    except ImportError:  # noqa: BLE001
        from astrbot.api.message_components import MessageChain
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

try:  # 仅用于类型标注，缺失也不影响运行
    from astrbot.api.provider import LLMResponse
except Exception:  # noqa: BLE001
    LLMResponse = object  # type: ignore

from .core.tts_engine import TtsEngine, is_speakable, clean_media_placeholders, clean_tts_text
from .core.markup import inject_markup, MARKUP_WHITELIST_RE
from .core.webapi import register_web_apis
from .core.translator import Translator
from .cosyvoice.client import CosyVoiceClient, CosyVoiceServerError, QueueFullError
from .cosyvoice.router import CosyVoiceRouter
from .utils import audio

PLUGIN_ID = "astrbot_plugin_cosyvoice"

# 双语识别与处理：当回复同时含「汉字」与「外语文字（假名/字母/数字/谚文等）」时，
# 视为「外文原文 + 中文翻译」双语回复——语音应只念外文部分（剥离中文），
# 文字展示仍按 translate_display_mode 保留双语，满足「看中文、听外文」。
_CJK_RE = re.compile(r"[㐀-䶿一-鿿豈-﫿]")
_FOREIGN_RE = re.compile(r"[A-Za-z0-9぀-ヿ가-힯]")


# 非拉丁外文字（假名/谚文/西里尔/泰文等）：出现即说明存在「外文原文」，不受字母数量限制
_FOREIGN_SCRIPT_RE = re.compile(r"[぀-ヿ가-힯Ѐ-ӿ฀-๿]")
_LATIN_RE = re.compile(r"[A-Za-z]")
# 仅含拉丁字母/数字时，需要至少这么多拉丁字母才算双语：
# 避免中文回复里夹带 OK / WiFi / 数字（如「7 点」）被误判为双语
# （一旦误判会跳过翻译、把中文剥离后只念那几个字母）
_BILINGUAL_MIN_LATIN = 15


def _strip_chinese(text: str) -> str:
    """去除文本中的汉字，保留外文、数字与标点（双语回复时只朗读外文）。"""
    return _CJK_RE.sub("", text)


def _is_bilingual(text: str) -> bool:
    """是否为「外文原文 + 中文翻译」双语回复。

    判定需要「含汉字」且「外文成分足够」：
    - 含假名/谚文/西里尔/泰文等非拉丁外文字 → 直接算（日/韩/俄等原文）；
    - 否则（只有拉丁字母/数字）要求拉丁字母达到一定数量，
      避免把中文回复里夹杂的 OK / WiFi / 数字（如「7 点」）误判成双语——
      误判会跳过翻译、把中文剥离后只念出那几个字母。
    """
    if not (_CJK_RE.search(text or "") and _FOREIGN_RE.search(text or "")):
        return False
    if _FOREIGN_SCRIPT_RE.search(text):
        return True
    return len(_LATIN_RE.findall(text)) >= _BILINGUAL_MIN_LATIN


def _has_foreign(text: str) -> bool:
    """文本去除中文后是否仍含可朗读的外文（假名/字母/数字/谚文）。

    用于判断某段是否值得发声：双语回复里纯中文段去中文后只剩标点/emoji，
    不算「有外文」，应只发文字、不合成语音（避免把 :，~ 🎵 这类残留当噪音念出）。
    """
    return bool(_FOREIGN_RE.search(text or ""))

# 语音服务器连不上时统一给用户的提示（大模型也需要能看懂这是服务器故障）。
# 提示均为「独立发送」的消息（主动推送 / 指令结果 / 工具补发），前面无正文，
# 不再加前导换行，避免消息开头出现空白行。
SERVER_DOWN_TIP = "🎙️（语音服务器失联了，可以稍后再试或联系管理员，文字照常发送）"
# 语音服务器繁忙（排队位置超过阈值 tts_queue_max_position）时给用户的提示
SERVER_BUSY_TIP = "🎙️（语音服务器正忙，排队的消息有点多，可以稍后再试，文字照常发送）"


@register(PLUGIN_ID, "Yours", "接入本地 CosyVoice3，让机器人以可配置音色朗读回复", "1.0.0")
class CosyVoicePlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        # AstrBot 在实例化插件时通过 __init__ 注入完整配置（含 template_list 类型）。
        # 部分版本也保留 context.get_config()，这里做兼容：优先用注入配置，回退到 get_config()。
        self._injected_config = config if config is not None else (self.context.get_config() or {})
        self.config = self._injected_config
        # 多服务端负载均衡：优先 servers 列表（url/enabled/weight 分流），
        # 为空时回退到 base_url 单机模式（兼容旧配置）。
        self.client = CosyVoiceRouter(
            servers=self.config.get("servers") or [],
            fallback_url=self.config.get("base_url", "http://127.0.0.1:50002"),
            sample_rate=int(self.config.get("sample_rate", 24000)),
            timeout=int(self.config.get("timeout", 150)),
            max_retry=int(self.config.get("tts_max_retry", 0) or 0),
            retry_backoff=float(self.config.get("tts_retry_backoff", 0.5) or 0.5),
            queue_max_position=int(self.config.get("tts_queue_max_position", 8) or 0),
        )
        self._cooldown_sec = float(self.config.get("tts_cooldown_sec", 30))
        # 翻译适配器（须在 engine 之前创建，engine 持有其引用做合成前翻译）
        self._translate_file = os.path.join(self._data_dir(), "translate_config.json")
        self.translator = Translator(self._load_translate_cfg())
        self.engine = TtsEngine(
            self.config, self.client,
            translator=self.translator,
            concurrency=int(self.config.get("tts_concurrency", 1) or 1),
        )
        # 每个消息的事件标记（避免并发串台），以 message_id 为键
        self._flags: dict = {}
        # 本轮模型生成的原文（按会话），用于「结果链文本无效」时回退合成
        self._last_llm: dict = {}
        # 本轮用户原始消息（按会话），供「用户要求用文字回复」的抑制判定跨钩子使用，
        # 避免 on_decorating_result 阶段 message_str 已不可用时漏判 text_keywords。
        self._last_user_msg: dict = {}
        # 语音服务器熔断（冷却）状态：服务端报错后进入冷却期，冷却期内直接跳过合成、
        # 只发文字，不再每条消息都去打已经坏掉的服务端（避免刷屏无效请求 / 反复 ReadError）。
        # _server_cooldown_until 为冷却到期的时间戳（0 表示正常）；_server_down 仅控制提示刷屏。
        self._server_cooldown_until = 0.0
        self._server_down = False
        # 本轮已合成的文本集合（origin -> {text}），防止 on_decorating_result 被框架
        # 重复触发时重复合成、重复打服务端导致服务端过载 / 误报失联。
        self._decorated: dict = {}
        # 会话级语音开关（按群持久记忆）：unified_msg_origin -> True
        data_dir = self._data_dir()
        self._session_file = os.path.join(data_dir, "tts_sessions.json")
        self._sessions = self._load_sessions()
        # 会话级音色（按群/私聊持久记忆）：unified_msg_origin -> 音色名
        self._voice_file = os.path.join(data_dir, "tts_voices.json")
        self._voices = self._load_voices()
        # 会话级发送方式（按群/私聊持久记忆）：unified_msg_origin -> "both"|"voice_only"
        # 未设置 = 跟随全局 send_mode（配置项「语音发送方式」）
        self._sendmode_file = os.path.join(data_dir, "tts_sendmodes.json")
        self._sendmodes = self._load_sendmodes()
        # WebUI 设置的最默认音色（按插件自有 data/ 持久，与 AstrBot 主配置解耦）：
        # 存在时优先于配置项 default_voice；聊天/WebUI 都可覆盖。
        self._default_voice_file = os.path.join(data_dir, "tts_default_voice.json")
        self._default_voice_override = self._load_default_voice_override()
        # WebUI 音色库（新增/编辑/删除的音色）：data/ 持久，优先于 AstrBot 配置 voices。
        # 让 WebUI 能独立管理音色，不被 _refresh_cfg 用配置覆盖。
        self._voices_lib_file = os.path.join(data_dir, "tts_voices_lib.json")
        self._voices_lib = self._load_voices_lib()
        # WebUI 概览「最近事件」环形缓冲（进程内，不持久化；最多 20 条）
        self._recent_events = []
        # 会话级昵称（按群/私聊持久记忆）：unified_msg_origin -> 昵称（best-effort，取自事件 sender_name）
        self._nickname_file = os.path.join(data_dir, "tts_nicknames.json")
        self._nicknames = self._load_nicknames()

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
            f"voices_count={len(self.engine.voices)}"
        )
        logger.info(
            f"[cosyvoice] 持久数据目录={os.path.dirname(self._session_file)} "
            f"开关文件存在={os.path.exists(self._session_file)} "
            f"音色文件存在={os.path.exists(self._voice_file)}"
        )
        # 注册 WebUI 后端 API（AstrBot 插件 Pages）
        try:
            register_web_apis(self)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[cosyvoice] WebUI API 注册失败（不影响语音功能）: {e}")

    # ---------- 事件标记辅助 ----------
    def _key(self, event: AstrMessageEvent):
        return getattr(event, "message_id", None) or event.unified_msg_origin

    def _set_flag(self, event: AstrMessageEvent, k: str, v=True):
        self._flags.setdefault(self._key(event), {})[k] = v

    def _get_flag(self, event: AstrMessageEvent, k: str, default=False):
        return self._flags.get(self._key(event), {}).get(k, default)

    def _clear(self, event: AstrMessageEvent, clear_llm: bool = False):
        """清理本条消息的事件标记。

        :param clear_llm: 是否同时清理本条消息残留的 LLM 原文与用户原消息缓存。
            on_decorating_result 是每轮消息的最后钩子，处理完即清，避免「上一轮的大模型
            原文」残留到下一轮、把非大模型消息（其他插件固定文案等）误判为 LLM 回复转语音。
        """
        self._flags.pop(self._key(event), None)
        if clear_llm:
            origin = event.unified_msg_origin
            self._last_llm.pop(origin, None)
            self._last_user_msg.pop(origin, None)

    def _push_event(self, ok: bool, msg: str):
        """记录一条『最近事件』（进程内环形缓冲，供 WebUI 概览展示）。

        仅内存保存（最多 20 条，最新在前），不持久化；插件重载即清空，
        定位为实时健康流水，而非审计日志。
        """
        ev = getattr(self, "_recent_events", None)
        if ev is None:
            ev = []
            self._recent_events = ev
        ev.insert(0, {
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "ok": bool(ok),
            "msg": str(msg)[:200],
        })
        if len(ev) > 20:
            del ev[20:]

    def _mark_server_ok(self):
        """合成成功：解除熔断冷却，并复位失联提示标志，便于下次真的失联时再提示。"""
        self._server_cooldown_until = 0.0
        self._server_down = False

    def _trip_breaker(self, chain: list = None):
        """合成失败：进入熔断冷却，冷却期内本插件不再向服务端发请求（只发文字）。

        冷却时长见配置 tts_cooldown_sec。原「在链上追加提示」因后台任务里结果链已发出而无效，
        故提示改由 _enter_cooldown 主动发送；此处仅记录冷却时间，供 /tts 指令等同步路径复用。
        """
        self._server_cooldown_until = time.time() + getattr(self, "_cooldown_sec", 30.0)

    async def _enter_cooldown(
        self, event: AstrMessageEvent, send_mode: str, full_text: str,
        tip: str = SERVER_DOWN_TIP, text_in_chain: bool = False,
    ):
        """合成失败：进冷却 + 发一次性提示 + 回退文字（文字已从结果链移除时才补发）。

        - 冷却期内本插件不再向服务端发任何请求（on_decorating_result 直接走回退分支）。
        - 首次进入冷却时主动发一条 tip 提示（用 context.send_message，平台已支持）。
          默认提示为失联文案（SERVER_DOWN_TIP）；繁忙（排队过长）场景传入 SERVER_BUSY_TIP。
        - text_in_chain=False（文字已从结果链移除，如 voice_only / 不合并 both）时
          把文字补发回去，避免前端静默；True（合并 both 文字已在结果链）则跳过。
        """
        logger.info(
            f"[cosyvoice] 进入冷却 | send_mode={send_mode} text_in_chain={text_in_chain}"
        )
        self._trip_breaker()
        self._push_event(False, tip)
        if not self._server_down:
            self._server_down = True
            try:
                # 直接传组件列表：chain_result() 会改写事件自身结果链，主动推送无需也不应改动它
                await self.context.send_message(
                    event.unified_msg_origin, MessageChain([Comp.Plain(tip)])
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[cosyvoice] 失联提示发送失败: {e}")
        await self._fallback_text(event, full_text, send_mode, text_in_chain)


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

    def _load_nicknames(self) -> dict:
        try:
            with open(self._nickname_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_nicknames(self):
        os.makedirs(os.path.dirname(self._nickname_file), exist_ok=True)
        with open(self._nickname_file, "w", encoding="utf-8") as f:
            json.dump(self._nicknames, f, ensure_ascii=False, indent=2)

    def _session_enabled(self, event: AstrMessageEvent) -> bool:
        return event.unified_msg_origin in self._sessions

    def _session_prob(self, event: AstrMessageEvent) -> float | None:
        """返回该会话的语音发送概率：1.0=常开（一直发），0~1=概率触发，None=未开启。

        兼容旧数据：存 True / "always" 视为 1.0；存数字（str/float）视为对应概率。
        """
        v = self._sessions.get(event.unified_msg_origin, None)
        if v is None or v is False:
            return None
        if v is True or (isinstance(v, str) and v.strip().lower() in ("always", "true", "1")):
            return 1.0
        try:
            p = float(v)
        except (TypeError, ValueError):
            return 1.0
        if p >= 1.0:
            return 1.0
        if p <= 0.0:
            return 0.0
        return p

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

    # ---------- WebUI 音色库（新增/编辑/删除，优先生效） ----------
    def _load_voices_lib(self) -> dict:
        try:
            with open(self._voices_lib_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
        return {}

    def _save_voices_lib(self):
        os.makedirs(os.path.dirname(self._voices_lib_file), exist_ok=True)
        with open(self._voices_lib_file, "w", encoding="utf-8") as f:
            json.dump(self._voices_lib, f, ensure_ascii=False, indent=2)

    def _effective_voices(self) -> dict:
        """实际生效的音色库：WebUI 音色库（data/）优先，合并配置 voices 为基底。
        返回 { 音色名: {prompt_wav, prompt_text, hidden} }。
        配置 voices 可能是 list（template_list）或 dict 或 JSON 字符串，
        统一用 engine 的归一化逻辑转成 dict，避免 dict(list) 报错。
        """
        config_voices = self.config.get("voices") or {}
        try:
            merged = dict(self.engine._norm_voices(config_voices))
        except Exception:  # noqa: BLE001
            merged = {}
        for name, v in self._voices_lib.items():
            merged[name] = v
        return merged

    # ---------- WebUI 设置的默认音色（与 AstrBot 主配置解耦，data/ 持久） ----------
    def _load_default_voice_override(self) -> str:
        try:
            with open(self._default_voice_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            return str(data.get("default_voice") or "").strip()
        except Exception:
            return ""

    def _save_default_voice_override(self, name: str = ""):
        os.makedirs(os.path.dirname(self._default_voice_file), exist_ok=True)
        with open(self._default_voice_file, "w", encoding="utf-8") as f:
            json.dump({"default_voice": name}, f, ensure_ascii=False, indent=2)

    def _effective_default_voice(self) -> str:
        """实际默认音色：WebUI override 优先，否则取配置 default_voice。"""
        if self._default_voice_override:
            return self._default_voice_override
        return str(self.config.get("default_voice", "") or "")

    def _session_voice(self, event: AstrMessageEvent) -> str | None:
        return self._voices.get(event.unified_msg_origin)

    # ---------- 会话级发送方式（按群持久记忆，/tts_type 设置） ----------
    def _load_sendmodes(self) -> dict:
        try:
            with open(self._sendmode_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_sendmodes(self):
        os.makedirs(os.path.dirname(self._sendmode_file), exist_ok=True)
        with open(self._sendmode_file, "w", encoding="utf-8") as f:
            json.dump(self._sendmodes, f, ensure_ascii=False, indent=2)

    def _session_send_mode(self, event: AstrMessageEvent) -> str | None:
        """本会话单独设置的发送方式：返回 "both"/"voice_only"；未设置返回 None（跟随全局）。"""
        v = self._sendmodes.get(event.unified_msg_origin)
        return v if v in ("both", "voice_only") else None

    def _effective_send_mode(self, event: AstrMessageEvent, cfg: dict) -> str:
        """实际生效的发送方式：会话单独设置（/tts_type）优先，否则取全局 send_mode。"""
        v = self._session_send_mode(event)
        if v is not None:
            return v
        return cfg.get("send_mode", "both")

    # ---------- 工具方法 ----------
    def _load_translate_cfg(self) -> dict:
        """读取翻译配置（data/translate_config.json，自有持久，与 AstrBot 主配置解耦）。"""
        try:
            with open(self._translate_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_translate_cfg(self, cfg: dict):
        """保存翻译配置并热更新 translator（无需重启立即生效）。"""
        with open(self._translate_file, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        self.translator.reload(cfg)

    def _refresh_cfg(self) -> dict:
        live = self.context.get_config() or {}
        # 以注入的完整配置为基线，再用 get_config() 的实时值覆盖。
        # 这样即使 get_config() 不返回 template_list/默认值字段，也不会丢失已配置的音色。
        merged = dict(self._injected_config)
        merged.update({k: v for k, v in live.items() if v is not None})
        # WebUI 设置的默认音色（data/ 持久）优先于配置项 default_voice
        if self._default_voice_override:
            merged["default_voice"] = self._default_voice_override
        self.config = merged
        self.engine.config = merged
        # 引擎音色 = 配置 voices + WebUI 音色库（WebUI 新增/编辑/删除优先）
        effective_voices = self._effective_voices()
        self.engine.update_voices(effective_voices)
        # 多服务端配置热更新：servers 列表或 base_url 变化时重建分流节点。
        self._refresh_servers(merged)
        # 冷却时长（秒）：服务端失联后多久内不再发任何 TTS 请求、直接回退文字。
        self._cooldown_sec = float(merged.get("tts_cooldown_sec", 30))
        return merged

    def _refresh_servers(self, merged: dict):
        """多服务端节点热更新：仅当 servers/base_url 与当前不一致时才重建。

        AstrBot 每条消息都可能调用 _refresh_cfg，不能每次都重建 client
        （会反复销毁/新建 httpx 连接池）。这里对比签名，变化才触发。
        """
        servers = merged.get("servers") or []
        fallback = (merged.get("base_url") or "http://127.0.0.1:50002").strip().rstrip("/")
        qmp = int(merged.get("tts_queue_max_position", 8) or 0)
        sig = (
            repr(sorted(servers, key=lambda s: str(s.get("url"))))
            + "|" + fallback
            + "|qmp=" + str(qmp)
        )
        if getattr(self, "_server_sig", None) == sig:
            return
        self._server_sig = sig
        try:
            # 仅在为 Router 时重建；兼容个别情况下仍是旧 client 的极端场景
            if isinstance(self.client, CosyVoiceRouter):
                self.client.update_servers(
                    servers,
                    sample_rate=int(merged.get("sample_rate", 24000)),
                    queue_max_position=qmp,
                )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[cosyvoice] 更新服务端节点失败: {e}")

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
        prob = self._session_prob(event)
        session_on = prob is not None

        # 用户明确要求「用文字回复」（如"用文字告诉/用文字发我"）时，本条不转语音。
        # 直接在此判定并返回 False，不依赖 on_decorating_result 阶段的 suppress 标志，
        # 避免 tts_on 下 message_str 不可用时漏判、仍把文字转成语音。
        # 注意：仅当用户本轮确实发了含抑制词的消息才生效（使用跨钩子保存的原消息兜底）。
        if cfg.get("enable_user_trigger", True):
            user_msg = (event.message_str or "").strip() or self._last_user_msg.get(
                event.unified_msg_origin, ""
            )
            text_kw = cfg.get("text_keywords", []) or []
            if user_msg and any(kw and kw in user_msg for kw in text_kw):
                logger.debug(
                    "[cosyvoice] 用户要求文字回复（命中 text_keywords），本条跳过语音"
                )
                return False

        if cfg.get("tts_scope", "llm_only") == "llm_only":
            # llm_only：只转「大模型回复」的语音（默认语义，勿放宽成"会话开了就全转"）。
            # 判定依据（按优先级）：
            #   1) llm_this_round：本轮确实触发过 on_llm_response 的标记（最精确，随本轮
            #      _flags 清理，不会残留到下一轮）；
            #   2) is_llm：on_llm_response 设置的标志；
            #   3) llm_recorded：_last_llm 存在本轮大模型原文（钩子路径差异兜底）。
            # 绝不使用「上一轮残留的 _last_llm」——否则同会话里其他插件的固定文案
            # （非大模型消息）也会被误转语音。
            llm_this_round = self._get_flag(event, "llm_this_round", False)
            llm_recorded = bool(self._last_llm.get(event.unified_msg_origin))
            is_llm_reply = bool(is_llm or llm_this_round or llm_recorded)
            base = bool(is_llm_reply and (auto or want or session_on))
        else:
            # all_text：自动开启则全部；否则仅关键词/工具触发或本会话已开
            base = bool(auto or want or session_on)

        # 概率触发：仅当「纯靠 tts_on 会话开关」触发（非 auto、非明确关键词/工具触发）时，
        # 才按会话概率决定是否发语音；auto_tts 或用户明确触发的仍照常发。
        if base and session_on and prob is not None and prob < 1.0 and not (auto or want):
            if random.random() >= prob:
                logger.debug(
                    f"[cosyvoice] 本轮语音概率未命中（p={prob}），仅发文字"
                )
                return False
        return base

    @staticmethod
    def _to_records(paths: list) -> list:
        """把 wav 路径列表转成 Record 组件列表（并登记清理）。"""
        recs = []
        for p in paths:
            recs.append(Comp.Record(file=p, url=p))
            audio.schedule_cleanup(p)
        return recs

    async def _realtime_send(self, event: AstrMessageEvent, records: list) -> bool:
        """主动推送一条消息（文字段 / 语音段 / 组合），返回是否发送成功。

        发送顺序（取第一个可用且成功的）：
        1) self.context.send_message(unified_msg_origin, chain)：AstrBot 官方主动消息 API（首选，真正投递）；
        2) event.send：部分平台/版本支持的事件级主动发送（兜底，理由见注意③）。

        :return: True=发送成功；False=两条通道都失败（调用方可降级处理，如分开补发）。
        注意：① 这里不能调用 event.chain_result()——那会改写事件自身的结果链，
        在 LLM 工具（tool_loop）执行期间调用会与 agent runner 的结果处理冲突，
        导致语音被静默丢弃（且 event.send 不抛异常，工具会误报「已发送」）。
        直接传组件列表即可，事件自身结果保持不动。
        ② 部分平台对「主动推送的组合消息（Plain+Record）」可能只展示语音、
        静默丢弃文字，因此调用方应尽量用单组件消息（文字、语音分开发）。
        ③ event.send 不能作为首选通道：它只是把消息并入事件结果链，而后台补发语音时
        事件早已响应完成，该链不会再被发送；且它【不抛异常】，会打印「主动推送成功
        (event.send)」却让用户什么也收不到。必须以 context.send_message 为首选。
        """
        types = [type(c).__name__ for c in records]
        # 统一包成 MessageChain：两条通道内部都会访问 .chain
        chain = MessageChain(list(records))
        # 首选：官方主动消息 API，真正调用平台投递（见注意③）
        try:
            await self.context.send_message(event.unified_msg_origin, chain)
            logger.info(f"[cosyvoice] 主动推送成功(context.send_message) 组件={types}")
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[cosyvoice] context.send_message 失败（尝试 event.send）: {e}")
        # 兜底：少数平台/版本仅支持事件级主动发送
        try:
            send = getattr(event, "send", None)
            if callable(send):
                await send(chain)
                logger.info(f"[cosyvoice] 主动推送成功(event.send) 组件={types}")
                return True
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[cosyvoice] event.send 也失败: {e} | 组件={types}")
        logger.warning(
            f"[cosyvoice] 主动推送失败（两条通道均不可用）| 组件={types}"
            f" | unified_msg_origin={event.unified_msg_origin}"
        )
        return False

    # ---------- LLM 回复钩子：标记 + 关键词触发 ----------
    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, resp: LLMResponse):
        self._refresh_cfg()
        self._set_flag(event, "is_llm", True)
        # 本轮确有大模型回复的标记：随 _flags 在本轮结束被 _clear 清掉，
        # 用于 _should_tts 精确判断「本条是否 LLM 回复」，避免把上一轮残留的
        # _last_llm 原文误当成本轮、导致其他插件的固定文案也被转语音。
        self._set_flag(event, "llm_this_round", True)

        # 记住本轮模型原文，供结果链文本缺失/异常（如混入 [] 占位符）时回退合成。
        # 无论是否为空都覆盖写入，避免回退时误用上一轮的真实文本（tts_on 下会念错内容）。
        resp_text = getattr(resp, "completion_text", None) or getattr(resp, "text", "") or ""
        if isinstance(resp_text, list):
            resp_text = "".join(str(x) for x in resp_text)
        # 综合净化：剔除媒体占位符 + LLM 工具调用序列化。
        # tool_loop_agent_runner 的最终 completion_text 可能混入工具调用 JSON
        # （如 comfyui_draw{"name":...}），必须剔除，否则会被当正文朗读。
        clean_text = clean_tts_text(resp_text)
        tool_calls = getattr(resp, "tool_calls", None) or getattr(resp, "tools", None) or []
        if tool_calls and not clean_text:
            # 纯工具调用轮（无正文）：不覆盖 _last_llm，保留上一轮干净文本，
            # 避免回退合成时念出工具调用序列化
            logger.debug(
                f"[cosyvoice] 本轮 LLM 响应为纯工具调用（{len(tool_calls)} 个），"
                "跳过写入 _last_llm"
            )
        else:
            self._last_llm[event.unified_msg_origin] = clean_text

        cfg = self.config

        # 关键词触发（先剔除媒体占位符标签，避免 <pc_history_media images="1" /> 之类干扰判定）
        if cfg.get("enable_user_trigger", True):
            msg = clean_media_placeholders(event.message_str or "")
            # 跨钩子保存用户原消息，供 _should_tts 的 text_keywords 抑制判定兜底使用
            if msg:
                self._last_user_msg[event.unified_msg_origin] = msg
            keywords = cfg.get("trigger_keywords", []) or []
            if any(kw and kw in msg for kw in keywords):
                self._set_flag(event, "want", True)

        # 自动语音
        if cfg.get("auto_tts", False):
            self._set_flag(event, "want", True)

        # 用户明确要求「用文字回复」：本条覆盖语音模式，只发文字（不改变 tts_on 开关）
        if cfg.get("enable_user_trigger", True):
            text_kw = cfg.get("text_keywords", []) or []
            if any(kw and kw in msg for kw in text_kw):
                self._set_flag(event, "suppress", True)

    # ---------- 括号内容不朗读（模块级工具） ----------
    _BRACKET_RE = re.compile(r"[（(\[](.*?)[）)\]]", re.DOTALL)

    def _strip_brackets(self, text: str) -> str:
        """移除所有被括号包裹的内容（含括号本身），用于不进入语音合成。

        但需保留语音标记白名单（如 [laughter] [breath]），否则会被当成「不朗读括号」误删。
        做法：先把白名单标记替换成占位符，剥离普通括号后再还原。
        """
        if not text:
            return text
        if MARKUP_WHITELIST_RE.search(text):
            ph = {}
            cnt = [0]

            def _protect(m):
                key = f"\x00COSY{cnt[0]}\x00"
                cnt[0] += 1
                ph[key] = m.group(0)
                return key

            tmp = MARKUP_WHITELIST_RE.sub(_protect, text)
            tmp = self._BRACKET_RE.sub("", tmp)
            for k, v in ph.items():
                tmp = tmp.replace(k, v)
            return tmp
        return self._BRACKET_RE.sub("", text)

    def _extract_brackets(self, text: str) -> str:
        """提取所有括号内容，按出现顺序拼接成可单独发送的文字（换行分隔）。"""
        parts = [p.strip() for p in self._BRACKET_RE.findall(text) if p and p.strip()]
        return "\n".join(parts)

    # ---------- 装饰结果钩子：不阻塞管线，文字立刻发，语音后台补 ----------
    @filter.on_decorating_result()
    async def on_decorating_result(self, event: AstrMessageEvent):
        cfg = self._refresh_cfg()

        # 记录会话昵称（best-effort，取自事件 sender_name），供 WebUI 会话列表友好展示；
        # 放在最前，确保即使被 suppress / 未命中 TTS 也照样记录（用户只想给任意会话配音色）。
        origin = event.unified_msg_origin
        nick = getattr(event, "sender_name", "") or ""
        if nick and self._nicknames.get(origin) != nick:
            self._nicknames[origin] = nick
            self._save_nicknames()

        # 本插件的 /tts 指令或 LLM 工具已自行发送语音，避免重复
        if self._get_flag(event, "suppress", False):
            self._clear(event, clear_llm=True)
            return

        if not self._should_tts(event, cfg):
            # 本条不转语音（未开启/用户要求文字/概率未命中等），同样清理 LLM 残留，
            # 防止上一轮大模型原文污染下一轮、把非大模型消息误转语音。
            self._clear(event, clear_llm=True)
            return

        result = event.get_result()
        chain = result.chain
        if not chain:
            return

        # 抽取纯文本（先综合净化：剔除媒体占位符标签 <pc_history_media images="1" />
        # 与 LLM 工具调用序列化，避免把图片/历史媒体占位/工具调用当正文朗读或显示）
        texts = [
            clean_tts_text(getattr(c, "text", ""))
            for c in chain
            if isinstance(c, Comp.Plain) and getattr(c, "text", "")
        ]
        full_text = "".join(texts).strip()
        if not is_speakable(full_text):
            # 结果链文本无效（空 / [] 占位符等）：回退用本轮模型原文合成（同样净化）
            fb = clean_tts_text(self._last_llm.get(event.unified_msg_origin, ""))
            if is_speakable(fb):
                logger.debug("[cosyvoice] 结果链文本无效，回退使用本轮模型原文合成语音")
                full_text = fb
            else:
                return

        # 已含语音则不重复
        if any(isinstance(c, Comp.Record) for c in chain):
            return

        voice = self._get_flag(event, "voice", None) or self._session_voice(event)
        # 发送方式：本会话单独设置优先（/tts_type），否则跟随全局 send_mode
        send_mode = self._effective_send_mode(event, cfg)
        merge = bool(cfg.get("segment_merge", False))
        # 文字是否保留在结果链：合并 both 模式保留（AstrBot 一次性发全文 + 整条语音）；
        # voice_only 或 不合并 both 都从结果链移除，改由后台逐段发「文字段+语音段」。
        text_in_chain = bool(send_mode == "both" and merge)

        # 服务端熔断冷却期：不再向服务端发任何请求，直接回退文字，避免一直卡着连文字也不发。
        # 文字在结果链（合并 both）会正常发出；已移除（voice_only / 不合并 both）需补发回去。
        if self._server_cooldown_until and time.time() < self._server_cooldown_until:
            # 冷却期内原本静默回退文字，导致「为什么突然不发语音了」完全无从排查。
            # 这里补一条 WARNING，带上剩余冷却秒数，默认日志级别即可看见。
            logger.warning(
                f"[cosyvoice] 处于语音冷却期，本次不合成语音、只发文字 | "
                f"剩余 {self._server_cooldown_until - time.time():.0f}s | "
                f"冷却由上一次合成失败触发（时长见配置 tts_cooldown_sec，默认 30s）"
            )
            if not text_in_chain:
                await self._fallback_text(event, full_text, send_mode, text_in_chain)
            return

        # 本轮同一条消息若已成功合成过（框架可能重复触发 on_decorating_result），
        # 直接跳过，避免重复打服务端、服务端过载、以及把偶发失败误报成「服务器失联」。
        origin = event.unified_msg_origin
        done = self._decorated.get(origin, set())
        if full_text in done:
            return

        voice_name, _, _ = self.engine.resolve_voice(voice)

        # 从结果链移除原文（纯内存操作，很快、不阻塞）：voice_only 只后台发语音；
        # 不合并 both 由后台逐段发「文字段+语音段」，文字跟随语音走、不一次性刷全文。
        # 文字均已由 LLM completion_text 存入会话历史，不丢上下文；语音失败时 _fallback_text 兜底补发。
        # —— 自动翻译：根据 translate_display_mode 决定聊天气泡文字，语音始终念译文 ——
        # 语音始终念译文（自动翻译场景：原文用外语音色念不通顺），只有文字展示受模式控制。
        display_text = full_text
        audio_text = None
        seg_items = None  # 翻译多段时：(原文段, 译文段) 列表，供不合并模式逐段发送
        bilingual = _is_bilingual(full_text)
        if self.translator is not None:
            vlang = self.engine.voice_language(voice)
            tmode = (cfg.get("translate_display_mode") or "both").strip().lower()
            if bilingual:
                # 双语（外文 + 中文翻译）：语音只念外文（去中文），显示保留双语原文，
                # 不做翻译（避免中文音色把外文翻成中文念出来，也避免整段把中文念出来）。
                audio_text = _strip_chinese(full_text)
                segs = self.engine.split_text(full_text)
                # 多段/单段都走逐段发送：每段语音取「去中文后的外文」；纯中文段（去中文后无外文）
                # 发送时跳过语音、只发文字（见 _background_speak 的 _has_foreign 判断）。
                seg_items = [(seg, _strip_chinese(seg)) for seg in segs]
                display_text = full_text
            elif tmode == "original":
                # 只显示原文：不翻译，分段留给语音侧按 segment_punct 处理
                pass
            else:
                # 按「换行 + 句末标点」切原文，逐段单独翻译（每段是完整语义单元，翻译更准）
                segs = self.engine.split_text(full_text)
                if len(segs) > 1:
                    pairs = []
                    for seg in segs:
                        try:
                            t = await self.translator.maybe_translate(seg, target=vlang)
                        except Exception as e:  # noqa: BLE001
                            logger.warning(f"[cosyvoice] 分段翻译异常，回退该段原文: {e}")
                            t = seg
                        pairs.append((seg, t))
                    # 仅当「确有段被翻译」才走译文排版；否则视为不需要翻译，
                    # 保持纯原文展示（不加「原文：」前缀，也不切译文语音）。
                    if any(t != o for o, t in pairs):
                        audio_text = "\n".join(t for _, t in pairs)
                        if tmode == "translated":
                            display_text = audio_text
                        else:  # both：仅真正翻译的段才接「中文：xxx」
                            display_text = "\n".join(
                                (f"{t}\n中文：{o}" if t != o else o) for o, t in pairs
                            )
                        seg_items = pairs
                else:
                    # 单行无多段：整块翻译（保持原行为，避免无谓的逐段调用）
                    try:
                        translated = await self.translator.maybe_translate(full_text, target=vlang)
                    except Exception as e:  # noqa: BLE001
                        logger.warning(f"[cosyvoice] 翻译接入异常，回退原文: {e}")
                        translated = full_text
                    if translated and translated != full_text:
                        audio_text = translated
                        display_text = translated if tmode == "translated" else f"{translated}\n中文：{full_text}"
                        # 单段译文也要走逐段发送分支：否则会把 display_text（译文+换行+中文：原文）
                        # 再按换行二次切分，导致文字被拆成两条、语音重复发送。
                        # 逐段分支的行为：语音=译文+副语言标签；文字=译文 + 换行 + 中文：原文（一条发出）。
                        seg_items = [(full_text, translated)]

        if not text_in_chain:
            result.chain = [c for c in chain if not isinstance(c, Comp.Plain)]
        else:
            # 合并 both：结果链保留文字，改为按 translate_display_mode 展示
            # （原文 / 译文 / 译文+换行+原文：）
            for c in result.chain:
                if isinstance(c, Comp.Plain):
                    c.text = display_text

        # 括号内容不朗读：仅从「语音合成文本」剥离括号内容。
        # - 合并模式：文字留在结果链（含括号），语音不念，不单独补发；
        # - 逐段模式：分段改在「剥离括号后的文本」上做，括号内容单独补发一条文字消息，
        #   避免括号内的句末标点把句子切断、造成文字出现半截句且语音/文字段数错位。
        speak_text = full_text
        bracket_text = ""
        if cfg.get("skip_bracket_tts", True):
            bracket_text = self._extract_brackets(full_text)
            if bracket_text:
                speak_text = self._strip_brackets(full_text)
        # 未翻译时合成文本按括号剥离规则取原文；翻译命中时 audio_text 已是译文
        if audio_text is None:
            audio_text = speak_text

        logger.info(
            f"[cosyvoice] 语音转入后台合成 | 音色={voice_name} "
            f"总长度={len(full_text)}字 模式={'合并' if merge else '逐段发送'} "
            f"send_mode={send_mode} text_in_chain={text_in_chain}"
        )

        # 关键：本钩子【不 await 合成】，文字立刻交给 AstrBot 发出（若保留在链上），避免占用
        # 事件循环、卡住同一会话/全局的其他消息。语音合成放到后台任务，合成完再主动补发。
        # 这样「等一会儿」可接受，但绝不阻塞其他事件。
        task = asyncio.ensure_future(
            self._background_speak(
                event, display_text, voice, send_mode, merge, origin, text_in_chain,
                audio_text=audio_text, seg_items=seg_items, bilingual=bilingual,
                bracket_text=bracket_text,
            )
        )
        # 避免「未等待的 Task 异常」警告刷屏
        task.add_done_callback(self._log_task_exc)
        self._clear(event, clear_llm=True)

    def _log_task_exc(self, task):
        try:
            exc = task.exception()
        except (asyncio.CancelledError, Exception):
            exc = None
        if exc and not isinstance(exc, asyncio.CancelledError):
            logger.error(f"[cosyvoice] 后台语音任务异常: {exc}")

    async def _background_speak(
        self, event: AstrMessageEvent, display_text: str,
        voice, send_mode: str, merge: bool, origin: str, text_in_chain: bool = False,
        audio_text: str | None = None, seg_items: list | None = None,
        bilingual: bool = False, bracket_text: str = "",
    ):
        """后台补发语音：不阻塞 on_decorating_result。

        发送方式与文字归属：
        - 合并 both：文字已在结果链（text_in_chain=True，已为「译文+换行+原文：」），只补发整条语音；
        - 合并 voice_only：文字已移除，只补发整条语音，失败回退补发文字；
        - 不合并 both：文字已移除，先整条发「译文+换行+原文：」文字、再逐段发译文语音；
        - 不合并 voice_only：文字已移除，逐段只发语音，失败回退补发文字。

        bracket_text：skip_bracket_tts 生效时从原文提取的括号内容。逐段模式下分段是在
        「剥离括号后的文本」上做的，故正文任意一段都不含括号，括号内容在正文各段发完后
        单独补发一条文字消息（合并模式不使用——那里文字保留在结果链里、含括号）。

        失败则进入冷却 + 回退文字（见 _enter_cooldown）；冷却期内不再打服务端。
        """
        # 实际合成的文本：audio_text 为译文（翻译命中时）或剥离括号的原文；
        # 空则无正文可念（纯括号），跳过合成
        synth_text = audio_text if audio_text is not None else display_text
        vlang = self.engine.voice_language(voice)
        synth_text = inject_markup(
            synth_text, vlang, self.config, voice=self.engine.voices.get(voice)
        )
        if not synth_text.strip():
            logger.info("[cosyvoice] 正文仅含括号内容，跳过语音合成（括号内容已按模式单独发送）")
            self._mark_server_ok()
            self._decorated.setdefault(origin, set()).add(display_text)
            return
        logger.info(
            f"[cosyvoice] 后台合成开始 | send_mode={send_mode} merge={merge} "
            f"text_in_chain={text_in_chain} 文本长度={len(display_text)}字"
        )
        # 文字与语音的发送先后：true=每段先语音后文字（先听后读）；false=先文字后语音（原行为）。
        # 仅影响不合并 both（逐段发送）模式。
        text_after_voice = bool(self.config.get("text_after_voice", True))
        try:
            if merge:
                path = await self.engine.synthesize(synth_text, voice, pre_translated=True)
                if not path:
                    # 仅服务端故障才冷却，配置/内容类失败不冷却（理由见下方逐段分支）
                    if getattr(self.engine, "last_failure_kind", "server") == "server":
                        logger.warning("[cosyvoice] 后台合成失败（服务端故障），进入冷却并回退文字")
                        await self._enter_cooldown(
                            event, send_mode, display_text, text_in_chain=text_in_chain
                        )
                    else:
                        logger.warning(
                            f"[cosyvoice] 后台合成失败（非服务端故障，不进冷却）: "
                            f"{getattr(self.engine, 'last_failure', '') or '未知原因'}"
                        )
                    return
                audio.schedule_cleanup(path)
                await self._realtime_send(event, [Comp.Record(file=path, url=path)])
            else:
                if send_mode == "both" and seg_items:
                    # 翻译多段：逐段发送——每段「译文直接换行接原文：xxx」文字 + 对应段译文语音，
                    # 文本随语音分段、且原文/译文一一对应（分段在原文侧，语义对齐可靠）
                    tmode = (self.config.get("translate_display_mode") or "both").strip().lower()
                    sent_any = False
                    for orig, trans in seg_items:
                        disp = trans if tmode == "translated" else (orig if bilingual else (f"{trans}\n中文：{orig}" if trans != orig else orig))
                        # 仅语音文本注入标记（换气/音效）；展示文字 disp 不带标记
                        trans_voiced = inject_markup(
                            trans, vlang, self.config, voice=self.engine.voices.get(voice)
                        )
                        if text_after_voice:
                            # 先语音后文字：语音成功先发语音，再发对应文字；语音失败也补发文字（不丢）
                            if _has_foreign(trans):
                                wav = await self.engine.synthesize(trans_voiced, voice, pre_translated=True)
                                if wav:
                                    sent_any = True
                                    audio.schedule_cleanup(wav)
                                    try:
                                        await self._realtime_send(event, [Comp.Record(file=wav, url=wav)])
                                    except Exception as e:  # noqa: BLE001
                                        logger.warning(f"[cosyvoice] 单段语音发送失败（跳过该段）: {e}")
                                else:
                                    logger.warning(
                                        f"[cosyvoice] 分段语音合成失败（跳过该段）: "
                                        f"{getattr(self.engine, 'last_failure', '') or '未知原因'}"
                                    )
                            else:
                                # 本段无外文可读（纯中文）：仅发文字、不发声
                                logger.debug("[cosyvoice] 本段无外文，仅发文字（跳过语音）")
                            if not await self._realtime_send(event, [Comp.Plain(disp)]):
                                logger.warning("[cosyvoice] 分段文字发送失败（已尝试补发）")
                        else:
                            # 先文字后语音（原行为）
                            if not await self._realtime_send(event, [Comp.Plain(disp)]):
                                logger.warning("[cosyvoice] 分段文字发送失败（语音仍尝试）")
                            if _has_foreign(trans):
                                wav = await self.engine.synthesize(trans_voiced, voice, pre_translated=True)
                                if wav:
                                    sent_any = True
                                    audio.schedule_cleanup(wav)
                                    try:
                                        await self._realtime_send(event, [Comp.Record(file=wav, url=wav)])
                                    except Exception as e:  # noqa: BLE001
                                        logger.warning(f"[cosyvoice] 单段语音发送失败（跳过该段）: {e}")
                                else:
                                    logger.warning(
                                        f"[cosyvoice] 分段语音合成失败（跳过该段）: "
                                        f"{getattr(self.engine, 'last_failure', '') or '未知原因'}"
                                    )
                            else:
                                logger.debug("[cosyvoice] 本段无外文，仅发文字（跳过语音）")
                    if not sent_any:
                        # 仅服务端故障才冷却，配置/内容类失败不冷却（理由见下方逐段分支）
                        if getattr(self.engine, "last_failure_kind", "server") == "server":
                            logger.warning("[cosyvoice] 后台合成失败（服务端故障），进入冷却并回退文字")
                            await self._enter_cooldown(
                                event, send_mode, display_text, text_in_chain=text_in_chain
                            )
                        else:
                            logger.warning(
                                f"[cosyvoice] 后台合成失败（非服务端故障，不进冷却）: "
                                f"{getattr(self.engine, 'last_failure', '') or '未知原因'}"
                            )
                        return
                elif send_mode == "both":
                    # 未走翻译多段分支时（没开翻译 / original 模式 / 单行翻译）：
                    # 文字按「换行+句末标点」分段逐段发送，与语音逐段对齐。
                    # 语音文本优先用 audio_text（译文/去中文外文），避免把中文翻译也念出来；
                    # 仅 original 模式（audio_text=None）时语音才与 display_text 一致。
                    # 分段源是「剥离括号后的文本」base_text，而非含括号的 display_text：
                    # 括号内容常含句末标点（如「（笑。）」），若在原文上分段会把句子从括号处
                    # 切断，既让文字出现半截句，又造成文字段与语音段数量错位。
                    # 剥离后分段，括号内容改由 bracket_text 单独补发一条文字消息。
                    skip_bracket = bool(self.config.get("skip_bracket_tts", True))
                    base_text = self._strip_brackets(display_text) if skip_bracket else display_text
                    segs = self.engine.split_text(base_text)
                    vsegs = self.engine.split_text(audio_text) if audio_text is not None else segs
                    if len(segs) > 1 or len(vsegs) > 1:
                        sent_any = False
                        for i, seg in enumerate(segs):
                            # 语音段取用：仅当译文分段与文字分段【数量一致】时才逐段对齐取译文。
                            # 段数不一致时（译文与原文分段天然不同），若用 vsegs[min(i, len-1)]
                            # 映射，越界的段会复用最后一段语音，表现为「后面的段落重复念前面那段」。
                            # 故不一致时退回用当前文字段，保证每段念的都是自己的内容。
                            if vsegs and len(vsegs) == len(segs):
                                vseg = vsegs[i]
                            else:
                                vseg = seg
                            # base_text / vseg 在 skip_bracket 时均已剥离括号，无需二次剥离
                            seg_audio = vseg
                            if bilingual:
                                seg_audio = _strip_chinese(seg_audio)
                            seg_audio = inject_markup(
                                seg_audio, vlang, self.config,
                                voice=self.engine.voices.get(voice),
                            )
                            if text_after_voice:
                                # 先语音后文字：语音成功先发语音，再发对应文字；语音失败也补发文字（不丢）
                                wav = await self.engine.synthesize(seg_audio, voice, pre_translated=True)
                                if wav:
                                    sent_any = True
                                    audio.schedule_cleanup(wav)
                                    try:
                                        await self._realtime_send(event, [Comp.Record(file=wav, url=wav)])
                                    except Exception as e:  # noqa: BLE001
                                        logger.warning(f"[cosyvoice] 单段语音发送失败（跳过该段）: {e}")
                                else:
                                    logger.warning(
                                        f"[cosyvoice] 分段语音合成失败（跳过该段）: "
                                        f"{getattr(self.engine, 'last_failure', '') or '未知原因'}"
                                    )
                                if not await self._realtime_send(event, [Comp.Plain(seg)]):
                                    logger.warning("[cosyvoice] 分段文字发送失败（已尝试补发）")
                            else:
                                # 先文字后语音（原行为）
                                if not await self._realtime_send(event, [Comp.Plain(seg)]):
                                    logger.warning("[cosyvoice] 分段文字发送失败（语音仍尝试）")
                                wav = await self.engine.synthesize(seg_audio, voice, pre_translated=True)
                                if wav:
                                    sent_any = True
                                    audio.schedule_cleanup(wav)
                                    try:
                                        await self._realtime_send(event, [Comp.Record(file=wav, url=wav)])
                                    except Exception as e:  # noqa: BLE001
                                        logger.warning(f"[cosyvoice] 单段语音发送失败（跳过该段）: {e}")
                                else:
                                    logger.warning(
                                        f"[cosyvoice] 分段语音合成失败（跳过该段）: "
                                        f"{getattr(self.engine, 'last_failure', '') or '未知原因'}"
                                    )
                        if not sent_any:
                            # 仅「服务端故障」才熔断冷却。配置/内容类失败（无音色、
                            # 无有效可合成文本）冷却毫无意义——重试也不会好转，
                            # 反而会让「插件刚重载、配置尚未就绪」被误判成服务器失联，
                            # 静默停发语音 30s，还给出误导性的失联提示。
                            if getattr(self.engine, "last_failure_kind", "server") == "server":
                                logger.warning("[cosyvoice] 后台合成失败（服务端故障），进入冷却并回退文字")
                                await self._enter_cooldown(
                                    event, send_mode, display_text, text_in_chain=text_in_chain
                                )
                            else:
                                logger.warning(
                                    f"[cosyvoice] 后台合成失败（非服务端故障，不进冷却）: "
                                    f"{getattr(self.engine, 'last_failure', '') or '未知原因'}"
                                )
                            return
                    else:
                        # 单行（无换行/句末标点分段）：按 text_after_voice 决定文字与语音先后
                        if not text_after_voice:
                            # 先文字后语音（原行为，单组件消息平台兼容）
                            if not await self._realtime_send(event, [Comp.Plain(base_text)]):
                                logger.warning("[cosyvoice] 整条文字发送失败（语音仍尝试）")
                        sent_any = False
                        async for wav in self.engine.iter_segment_wavs(synth_text, voice, pre_translated=True):
                            sent_any = True
                            audio.schedule_cleanup(wav)
                            try:
                                await self._realtime_send(event, [Comp.Record(file=wav, url=wav)])
                            except Exception as e:  # noqa: BLE001
                                logger.warning(f"[cosyvoice] 单段语音发送失败（跳过该段）: {e}")
                        if not sent_any:
                            # 仅「服务端故障」才熔断冷却。配置/内容类失败（无音色、
                            # 无有效可合成文本）冷却毫无意义——重试也不会好转，
                            # 反而会让「插件刚重载、配置尚未就绪」被误判成服务器失联，
                            # 静默停发语音 30s，还给出误导性的失联提示。
                            if getattr(self.engine, "last_failure_kind", "server") == "server":
                                logger.warning("[cosyvoice] 后台合成失败（服务端故障），进入冷却并回退文字")
                                await self._enter_cooldown(
                                    event, send_mode, display_text, text_in_chain=text_in_chain
                                )
                            else:
                                logger.warning(
                                    f"[cosyvoice] 后台合成失败（非服务端故障，不进冷却）: "
                                    f"{getattr(self.engine, 'last_failure', '') or '未知原因'}"
                                )
                            return
                        if text_after_voice:
                            # 语音已发，再补发整条文字（先听后读）
                            if not await self._realtime_send(event, [Comp.Plain(base_text)]):
                                logger.warning("[cosyvoice] 整条文字发送失败（已尝试补发）")
                    # 括号内容单独补发一条文字：正文各段（含单行）都取自剥离括号后的
                    # base_text，不含任何括号内容，故此处补发不会与正文重复。
                    if bracket_text:
                        if not await self._realtime_send(event, [Comp.Plain(bracket_text)]):
                            logger.warning("[cosyvoice] 括号内容补发失败（不重试，避免刷屏）")
                else:
                    # voice_only：只发语音段，文字由 LLM completion_text 存会话历史兜底
                    if seg_items:
                        sent_any = False
                        for orig, trans in seg_items:
                            if not _has_foreign(trans):
                                # 纯中文段（去中文后无外文）：voice_only 也不发声
                                continue
                            wav = await self.engine.synthesize(trans, voice, pre_translated=True)
                            if wav:
                                sent_any = True
                                audio.schedule_cleanup(wav)
                                try:
                                    await self._realtime_send(event, [Comp.Record(file=wav, url=wav)])
                                except Exception as e:  # noqa: BLE001
                                    logger.warning(f"[cosyvoice] 单段语音发送失败（跳过该段）: {e}")
                        if not sent_any:
                            # 仅「服务端故障」才熔断冷却。配置/内容类失败（无音色、
                            # 无有效可合成文本）冷却毫无意义——重试也不会好转，
                            # 反而会让「插件刚重载、配置尚未就绪」被误判成服务器失联，
                            # 静默停发语音 30s，还给出误导性的失联提示。
                            if getattr(self.engine, "last_failure_kind", "server") == "server":
                                logger.warning("[cosyvoice] 后台合成失败（服务端故障），进入冷却并回退文字")
                                await self._enter_cooldown(
                                    event, send_mode, display_text, text_in_chain=text_in_chain
                                )
                            else:
                                logger.warning(
                                    f"[cosyvoice] 后台合成失败（非服务端故障，不进冷却）: "
                                    f"{getattr(self.engine, 'last_failure', '') or '未知原因'}"
                                )
                            return
                    else:
                        sent_any = False
                        async for wav in self.engine.iter_segment_wavs(synth_text, voice, pre_translated=True):
                            sent_any = True
                            rec = Comp.Record(file=wav, url=wav)
                            audio.schedule_cleanup(wav)
                            try:
                                await self._realtime_send(event, [rec])
                            except Exception as e:  # noqa: BLE001
                                # 单段发送失败只跳过该段，不影响后续段继续发出
                                logger.warning(f"[cosyvoice] 单段语音发送失败（跳过该段）: {e}")
                        if not sent_any:
                            # 仅「服务端故障」才熔断冷却。配置/内容类失败（无音色、
                            # 无有效可合成文本）冷却毫无意义——重试也不会好转，
                            # 反而会让「插件刚重载、配置尚未就绪」被误判成服务器失联，
                            # 静默停发语音 30s，还给出误导性的失联提示。
                            if getattr(self.engine, "last_failure_kind", "server") == "server":
                                logger.warning("[cosyvoice] 后台合成失败（服务端故障），进入冷却并回退文字")
                                await self._enter_cooldown(
                                    event, send_mode, display_text, text_in_chain=text_in_chain
                                )
                            else:
                                logger.warning(
                                    f"[cosyvoice] 后台合成失败（非服务端故障，不进冷却）: "
                                    f"{getattr(self.engine, 'last_failure', '') or '未知原因'}"
                                )
                            return
            # 整轮成功后才登记幂等 + 解除冷却
            self._mark_server_ok()
            self._decorated.setdefault(origin, set()).add(display_text)
            bucket = self._decorated.get(origin)
            if bucket and len(bucket) > 20:
                self._decorated[origin] = set(list(bucket)[-20:])
            logger.info("[cosyvoice] 后台语音合成完成，已补发")
            self._push_event(True, "后台语音合成完成并已发送")
        except QueueFullError:
            # 排队过长：服务端在线但繁忙，进冷却避免反复打繁忙服务器，提示稍后再试
            logger.warning("[cosyvoice] 语音服务器繁忙（排队过长），进入冷却并回退文字")
            await self._enter_cooldown(
                event, send_mode, display_text, tip=SERVER_BUSY_TIP, text_in_chain=text_in_chain
            )
        except CosyVoiceServerError:
            logger.warning("[cosyvoice] 语音服务器失联，进入冷却并回退文字")
            await self._enter_cooldown(
                event, send_mode, display_text, text_in_chain=text_in_chain
            )
        except Exception as e:
            logger.warning(f"[cosyvoice] 后台语音合成失败（进入冷却并回退文字）: {e}")
            await self._enter_cooldown(
                event, send_mode, display_text, text_in_chain=text_in_chain
            )

    async def _fallback_text(
        self, event: AstrMessageEvent, full_text: str, send_mode: str,
        text_in_chain: bool = False,
    ):
        """语音彻底失败时，把文字补发回去，避免前端静默。

        - text_in_chain=True（合并 both）：文字已在结果链由 AstrBot 正常发出，无需补发。
        - text_in_chain=False（voice_only / 不合并 both）：文字已从结果链移除，
          若语音失败则需主动把文字发出来，让用户至少看得到
          （文字也由 LLM completion_text 存入会话历史，不丢上下文）。
        使用 context.send_message（官方主动消息 API），你平台已确认支持。
        """
        if text_in_chain:
            return
        if not full_text:
            return
        logger.info(
            f"[cosyvoice] 补发完整文字 | send_mode={send_mode} 长度={len(full_text)}字"
        )
        try:
            # 直接传组件列表：chain_result() 会改写事件自身结果链，主动推送无需也不应改动它
            await self.context.send_message(
                event.unified_msg_origin, MessageChain([Comp.Plain(full_text)])
            )
            logger.info("[cosyvoice] 语音失败，已退化为补发文字")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[cosyvoice] 失败兜底补发文字也失败: {e}")

    # ---------- 用户指令：/tts、/tts0、/tts1（均只发语音，不随 send_mode 发文字） ----------
    async def _iter_cmd_audio(self, text: str, voice, mode: str = "default"):
        """按指令模式逐条产出 wav 路径（异常向上抛，由指令层统一提示）：

        - ``whole``：整段一次合成、发一条语音（内部仍会切段拼接防超长）；
        - ``newline``：按换行符分块，每块一条语音；
        - ``default``：按配置 segment_merge（合并成一条 / 分段逐条）。
        """
        if mode == "whole":
            path = await self.engine.synthesize(text, voice)
            if path:
                yield path
            return
        if mode == "newline":
            blocks = [b.strip() for b in re.split(r"\n+", text) if b.strip()]
            for block in blocks:
                path = await self.engine.synthesize(block, voice)
                if path:
                    yield path
            return
        # default：按配置
        if self.config.get("segment_merge", False):
            path = await self.engine.synthesize(text, voice)
            if path:
                yield path
        else:
            async for wav in self.engine.iter_segment_wavs(text, voice):
                yield wav

    async def _tts_cmd_impl(self, event: AstrMessageEvent, text: str, mode: str, empty_hint: str):
        """指令类合成公共逻辑：发送方式遵循 tts_type/send_mode——both 时语音之外补发文字，voice_only 时只发语音。"""
        # 保留原始文本（含括号），用于 both 模式下补发文字；括号内容不进入语音合成
        orig_text = text
        if self.config.get("skip_bracket_tts", True):
            text = self._strip_brackets(text)
            _sv = self._session_voice(event)
            text = inject_markup(
                text, self.engine.voice_language(_sv), self.config,
                voice=self.engine.voices.get(_sv),
            )
            if not text.strip():
                yield event.plain_result("括号里的内容我就不念啦～")
                return
        # 服务端熔断冷却期内，直接提示，不再去打已坏的服务端
        if self._server_cooldown_until and time.time() < self._server_cooldown_until:
            yield event.plain_result(SERVER_DOWN_TIP)
            return
        self._set_flag(event, "suppress", True)
        sent = False
        try:
            async for wav in self._iter_cmd_audio(text, self._session_voice(event), mode):
                sent = True
                yield event.chain_result([Comp.Record(file=wav, url=wav)])
                audio.schedule_cleanup(wav)
            if not sent:
                yield event.plain_result(empty_hint)
            else:
                if self._effective_send_mode(event, self._refresh_cfg()) == "both":
                    yield event.plain_result(orig_text)
                self._push_event(True, "指令语音已发送")
        except QueueFullError:
            # 排队过长：服务端在线但繁忙，进冷却避免反复打繁忙服务器，提示稍后再试
            self._trip_breaker([])
            logger.warning("[cosyvoice] 指令合成服务器繁忙（排队过长），进入冷却")
            self._push_event(False, SERVER_BUSY_TIP)
            yield event.plain_result(SERVER_BUSY_TIP)
        except CosyVoiceServerError:
            self._trip_breaker([])
            self._push_event(False, SERVER_DOWN_TIP)
            yield event.plain_result(SERVER_DOWN_TIP)
        except Exception as e:
            # 偶发错误（如连接中途断开）不进熔断，仅提示本条失败，可重试
            logger.warning(f"[cosyvoice] 指令合成失败（本条跳过）: {e}")
            self._push_event(False, f"指令合成失败：{e}")
            yield event.plain_result(empty_hint)

    @filter.command("tts")
    async def tts_cmd(self, event: AstrMessageEvent):
        self._refresh_cfg()
        raw = (event.message_str or "").strip()
        # 去掉 /tts 命令前缀：兼容 AstrBot 是否已自动剥离命令词，以及带空格/@提及等情况
        text = re.sub(r"^[/\s@]*tts\b\s*", "", raw, flags=re.IGNORECASE).strip()
        if not text:
            yield event.plain_result("试试这样：/tts 后面跟上你想让我念的话～")
            return
        async for res in self._tts_cmd_impl(
            event, text, "default", "哎呀，话到嘴边卡壳了，这次没念出来，稍后再试试？"
        ):
            yield res

    @filter.command("tts0")
    async def tts0_cmd(self, event: AstrMessageEvent):
        """/tts0 文本：整段一次合成、一口气念完（发一条语音，不分段发多条）。"""
        self._refresh_cfg()
        raw = (event.message_str or "").strip()
        text = re.sub(r"^[/\s@]*tts0\b\s*", "", raw, flags=re.IGNORECASE).strip()
        if not text:
            yield event.plain_result("试试这样：/tts0 后面跟上文本，我会一口气念完（发一条语音）～")
            return
        async for res in self._tts_cmd_impl(
            event, text, "whole", "哎呀，话到嘴边卡壳了，这次没念出来，稍后再试试？"
        ):
            yield res

    @filter.command("tts1")
    async def tts1_cmd(self, event: AstrMessageEvent):
        """/tts1 文本：按换行符分段，每段一条语音逐条念（both 时同时发文字）。"""
        self._refresh_cfg()
        raw = (event.message_str or "").strip()
        text = re.sub(r"^[/\s@]*tts1\b\s*", "", raw, flags=re.IGNORECASE).strip()
        if not text:
            yield event.plain_result("试试这样：/tts1 后面跟上多行文本，我会按换行逐段念～")
            return
        async for res in self._tts_cmd_impl(
            event, text, "newline", "哎呀，话到嘴边卡壳了，这次没念出来，稍后再试试？"
        ):
            yield res

    # ---------- 用户指令：/tts_voice <音色名> ----------
    @filter.command("tts_voice")
    async def tts_voice_cmd(self, event: AstrMessageEvent):
        self._refresh_cfg()
        raw = (event.message_str or "").strip()
        # 去掉 /tts_voice 命令前缀（同上，兼容已剥离/未剥离与 @提及）
        name = re.sub(r"^[/\s@]*tts_voice\b\s*", "", raw, flags=re.IGNORECASE).strip()
        # 列表只展示非隐藏音色；切换校验用全部音色（隐藏音色知道名字仍可手动指定）
        all_voices = self.engine.list_voices(include_hidden=True)
        if not all_voices:
            yield event.plain_result("我还没拿到能用的嗓音，暂时开不了口～ 先给我安排一个音色吧。")
            return
        voices = self.engine.list_voices()
        if not name or name not in all_voices:
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

    # ---------- 会话级开关：/tts_on 当前群语音，可带概率参数（如 /tts_on 0.8） ----------
    @filter.command("tts_on")
    async def tts_on_cmd(self, event: AstrMessageEvent):
        self._refresh_cfg()
        origin = event.unified_msg_origin
        raw = (event.message_str or "").strip()
        # 去掉 /tts_on 命令前缀（兼容已剥离/未剥离与 @提及），取参数
        arg = re.sub(r"^[/\s@]*tts_on\b\s*", "", raw, flags=re.IGNORECASE).strip()

        if not arg or arg.lower() in ("always", "true", "1"):
            # 不带参数或显式 always/1：常开（一直发语音）
            self._sessions[origin] = True
            self._save_sessions()
            yield event.plain_result(
                "收到～ 从这个聊天开始，我每句回复都给你念出来啦（想静音就发 /tts_off）。"
            )
            return

        # 尝试解析概率：支持 0.8 / 0.8 / 80% 等形式
        m = re.search(r"(\d+(?:\.\d+)?)\s*%?", arg)
        if not m:
            yield event.plain_result(
                "这个概率我有点没看懂～ 你可以这样用：\n"
                "/tts_on 常开语音\n/tts_on 0.8 有 80% 概率发语音\n/tts_on 1 一直发语音"
            )
            return
        try:
            p = float(m.group(1))
        except ValueError:
            p = -1
        if arg.rstrip().endswith("%"):
            p = p / 100
        if p >= 1.0:
            self._sessions[origin] = True
            self._save_sessions()
            yield event.plain_result("收到～ 这个聊天我会一直给你念出来啦（/tts_off 可静音）。")
            return
        if p <= 0.0:
            yield event.plain_result("概率得大于 0 呀～ 用 /tts_on 0.8 这种，或者不带参数就是一直念。")
            return
        self._sessions[origin] = p
        self._save_sessions()
        yield event.plain_result(
            f"好嘞～ 这个聊天之后约有 {int(p * 100)}% 的概率给你念出来（每次回复随机抽），"
            f"想静音就 /tts_off，想一直念就 /tts_on 不带参数。"
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

    # ---------- 会话级发送方式：/tts_type -1|0|1 ----------
    @filter.command("tts_type")
    async def tts_type_cmd(self, event: AstrMessageEvent):
        self._refresh_cfg()
        origin = event.unified_msg_origin
        raw = (event.message_str or "").strip()
        # 去掉 /tts_type 命令前缀（兼容已剥离/未剥离与 @提及），取参数
        arg = re.sub(r"^[/\s@]*tts_type\b\s*", "", raw, flags=re.IGNORECASE).strip()
        m = re.fullmatch(r"(-1|0|1)", arg or "")
        if not m:
            yield event.plain_result(
                "这个参数我没看懂～ 发送方式这样设：\n"
                "/tts_type -1 跟随全局设置\n"
                "/tts_type 0 只发语音（文字仍写入上下文）\n"
                "/tts_type 1 语音+文字都发"
            )
            return
        val = m.group(1)
        gsm = self.config.get("send_mode", "both")
        gsm_hint = "语音+文字" if gsm == "both" else "仅语音"
        if val == "-1":
            if self._sendmodes.pop(origin, None) is not None:
                self._save_sendmodes()
            yield event.plain_result(
                f"好嘞，这个聊天恢复跟随全局设置了（当前全局是「{gsm_hint}」）。"
            )
        elif val == "0":
            self._sendmodes[origin] = "voice_only"
            self._save_sendmodes()
            yield event.plain_result(
                "好嘞，这个聊天以后只发语音～（文字仍会写入会话上下文，AI 不会失忆；"
                "/tts_type -1 可恢复跟随全局）"
            )
        else:
            self._sendmodes[origin] = "both"
            self._save_sendmodes()
            yield event.plain_result(
                "好嘞，这个聊天以后语音+文字都发～（/tts_type 0 可改为只发语音）"
            )

    # ---------- 查看当前聊天语音开关状态（不暴露敏感信息） ----------
    @filter.command("tts_status")
    async def tts_status_cmd(self, event: AstrMessageEvent):
        self._refresh_cfg()
        prob = self._session_prob(event)
        on = prob is not None
        if not on:
            mode_hint = "未开启 🔇"
        elif prob >= 1.0:
            mode_hint = "常开（每句都念）🔊"
        else:
            mode_hint = f"概率触发（约 {int(prob * 100)}% 发语音）🎲"
        session_voice = self._session_voice(event)
        default_voice = self.config.get("default_voice") or ""
        # 发送方式：会话单独设置（/tts_type）优先，否则跟随全局
        ssm = self._session_send_mode(event)
        gsm = self.config.get("send_mode", "both")
        gsm_hint = "语音+文字" if gsm == "both" else "仅语音"
        if ssm is None:
            sm_hint = f"跟随全局（{gsm_hint}）"
        else:
            sm_hint = ("语音+文字" if ssm == "both" else "仅语音") + f"（全局 {gsm_hint}）"
        lines = [
            f"自动语音开关：{mode_hint}",
            f"当前聊天音色：{session_voice or '（未单独设置）'}",
            f"全局默认音色：{default_voice or '（未配置）'}",
            f"发送方式：{sm_hint}",
        ]
        yield event.plain_result("\n".join(lines))

    # ---------- 使用帮助：/tts_help（只展示常用指令） ----------
    @filter.command("tts_help")
    async def tts_help_cmd(self, event: AstrMessageEvent):
        self._refresh_cfg()
        lines = [
            "CosyVoice 语音 · 常用指令",
            "/tts 文本 —— 让我把这句话念出来（按配置分段）",
            "/tts0 文本 —— 一口气念完，只发一条语音（不分段）",
            "/tts1 文本 —— 按换行符分段逐条念（每条语音对应一行）",
            "/tts_on [/tts_off] —— 开启/关闭本聊天自动语音（可加概率，如 /tts_on 0.8）",
            "/tts_type -1|0|1 —— 本聊天发送方式：-1 跟随全局 / 0 仅语音 / 1 语音+文字",
            "/tts_voice 名字 —— 切换本聊天音色（不带参数可查看可选音色）",
            "/tts_status —— 查看本聊天当前状态",
            "",
            "另外：回复里带「念出来 / 读出来」能触发本条语音；",
            "带「用文字 / 别用语音」则本条只用文字回复。",
        ]
        yield event.plain_result("\n".join(lines))

    # ---------- 导出音色配置为 JSON（备份 / 迁移） ----------
    @filter.command("tts_export")
    async def tts_export_cmd(self, event: AstrMessageEvent):
        self._refresh_cfg()
        voices = self.engine.voices
        if not voices:
            yield event.plain_result(
                "我这儿还没有任何音色配置，导出个寂寞～ 先去插件配置里把 JSON 粘贴进「音色列表」吧。"
            )
            return
        json_str = json.dumps(voices, ensure_ascii=False, indent=2)
        # 生成临时 .json 文件，便于直接保存；发送失败则回退为代码块
        import tempfile

        tmp = os.path.join(tempfile.gettempdir(), "astrbot_cosyvoice_voices.json")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(json_str)
        except Exception:
            tmp = None
        file_comp = getattr(Comp, "File", None)
        if tmp and file_comp is not None:
            try:
                yield event.chain_result([
                    Comp.Plain("这是当前所有音色的 JSON（文件可直接存为 .json）："),
                    file_comp(path=tmp),
                ])
                return
            except Exception:
                pass
        yield event.plain_result(f"当前音色 JSON：\n```json\n{json_str}\n```")

    # ---------- LLM 函数调用工具 ----------
    @filter.llm_tool(name="text_to_speech")
    async def text_to_speech_tool(self, event: AstrMessageEvent, text: str, voice: str = ""):
        """将指定文本转换为语音并朗读出来（临时念一次，不开启长期语音模式）。
        仅在用户明确给出要朗读的具体文本（如「把这段话念出来 / 朗读以下内容」）时使用。
        注意：若用户想「长期用语音交流」，请调用 set_voice_mode，不要调本工具。
        若当前聊天已开启语音模式（/tts_on 或全局 auto_tts）且语音服务端正常，
        本工具会直接拒绝执行，请改用文字正常回复，由插件自动朗读最终回复。
        例外：语音服务端处于冷却/失联时本工具仍会响应——此时自动语音发不出声，
        工具是唯一兜底，不能一并拒绝。

        Args:
            text(string): 需要朗读的文本内容
            voice(string): 可选，指定音色名；留空则使用默认音色
        """
        self._refresh_cfg()
        cfg = self.config
        if not self.config.get("enable_llm_tool", True):
            return "语音功能还没开呢，先把它打开我就能念啦～"

        # 服务端熔断冷却期内，直接提示，不再去打已坏的服务端。
        # 顺序要点：必须排在「已开启语音模式则拒绝工具」之前——
        # 冷却期间自动语音这条路已经发不出声（on_decorating_result 静默回退文字），
        # 若此时还把工具一并拒绝，两条通道会同时失效，用户彻底收不到语音，
        # 表现为「关掉 /tts_on 反而能出声」。冷却时由这里统一给出失联提示。
        if self._server_cooldown_until and time.time() < self._server_cooldown_until:
            await self._realtime_send(event, [Comp.Plain(SERVER_DOWN_TIP)])
            return "语音服务器暂时失联，已用文字回复你。"

        # 已开启「自动语音模式」（本会话 /tts_on 或全局 auto_tts）时，不应再手动调本工具：
        # 自动语音环节会朗读最终回复，手动调用反而会因 suppress 标志杀掉自动语音、
        # 且把回复拆成「调工具前 / 调工具后」多条消息。直接拒绝并引导模型用文字回复即可。
        # 仅在服务端健康时才拒绝（冷却情形已在上面返回）。
        if bool(cfg.get("auto_tts", False)) or self._session_prob(event) is not None:
            return ("当前聊天已开启语音模式，无需调用本工具。请直接把要说的写进文字回复，"
                    "插件会自动把回复合成语音发送。")

        self._set_flag(event, "suppress", True)
        if voice:
            self._set_flag(event, "voice", voice)

        if not is_speakable(text):
            # 模型可能没把正文放进 text 参数（如传入 [] 占位符），回退用本轮模型原文
            fb = clean_tts_text(self._last_llm.get(event.unified_msg_origin, ""))
            if is_speakable(fb):
                text = fb
            else:
                return "用户没有提供要朗读的文本，请让用户先给出具体内容。"
        text = clean_tts_text(text)

        target_voice = voice or self._session_voice(event)
        tts_text = inject_markup(
            text.strip(), self.engine.voice_language(target_voice), self.config,
            voice=self.engine.voices.get(target_voice),
        )
        merge = bool(self.config.get("segment_merge", False))
        try:
            if merge:
                path = await self.engine.synthesize(tts_text, target_voice)
                if not path:
                    await self._realtime_send(event, [Comp.Plain("话到嘴边卡壳了，这次没念出来，待会儿再试试？")])
                    return "语音合成失败，已告知用户。"
                audio.schedule_cleanup(path)
                # 生效发送方式为 both（tts_type/send_mode）时，语音之外补发文字；否则只发语音
                if not await self._realtime_send(event, [Comp.Record(file=path, url=path)]):
                    # 合成成功但推送失败：如实上报，不再谎报「已发送给用户」
                    await self._realtime_send(event, [Comp.Plain("语音合成好了，但发送失败，待会儿再试试？")])
                    return "语音合成成功，但主动推送失败，已用文字告知用户。"
                if self._effective_send_mode(event, cfg) == "both":
                    await self._realtime_send(event, [Comp.Plain(text)])
            else:
                # 不合并：边合成边逐段主动发送（每段独立一条消息，服务端返回一段即发一段）
                sent = False
                sent_ok = False
                async for wav in self.engine.iter_segment_wavs(tts_text, target_voice):
                    sent = True
                    if await self._realtime_send(event, [Comp.Record(file=wav, url=wav)]):
                        sent_ok = True
                    audio.schedule_cleanup(wav)
                if not sent:
                    await self._realtime_send(event, [Comp.Plain("话到嘴边卡壳了，这次没念出来，待会儿再试试？")])
                    return "语音合成失败，已告知用户。"
                if not sent_ok:
                    # 合成成功但推送失败：如实上报，不再谎报「已发送给用户」
                    await self._realtime_send(event, [Comp.Plain("语音合成好了，但发送失败，待会儿再试试？")])
                    return "语音合成成功，但主动推送失败，已用文字告知用户。"
                # both 时语音之外补发文字（voice_only 仍只发语音）
                if self._effective_send_mode(event, cfg) == "both":
                    await self._realtime_send(event, [Comp.Plain(text)])
            self._mark_server_ok()
            self._push_event(True, "已用语音朗读并发送")
            return "已用语音朗读，语音已发送给用户。"
        except QueueFullError:
            # 排队过长：服务端在线但繁忙，进冷却避免反复打繁忙服务器，提示稍后再试
            self._trip_breaker()
            logger.warning("[cosyvoice] text_to_speech 服务器繁忙（排队过长），进入冷却")
            self._push_event(False, SERVER_BUSY_TIP)
            await self._realtime_send(event, [Comp.Plain(SERVER_BUSY_TIP)])
            return "语音服务器繁忙（排队过长），已用文字告知用户。"
        except CosyVoiceServerError:
            self._trip_breaker()
            logger.warning("[cosyvoice] text_to_speech 服务器失联，进入冷却")
            self._push_event(False, SERVER_DOWN_TIP)
            await self._realtime_send(event, [Comp.Plain(SERVER_DOWN_TIP)])
            return "语音服务器失联，已用文字告知用户。"
        except Exception as e:
            # 偶发错误（如连接中途断开）不进熔断，仅提示本条失败，可重试
            logger.warning(f"[cosyvoice] text_to_speech 合成失败（本条跳过）: {e}")
            self._push_event(False, f"语音合成失败：{e}")
            await self._realtime_send(event, [Comp.Plain("话到嘴边卡壳了，这次没念出来，待会儿再试试？")])
            return "语音合成失败，已告知用户。"

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
        # 校验用全部音色（隐藏音色知道名字也可设置）；提示列表只展示非隐藏
        all_voices = self.engine.list_voices(include_hidden=True)
        voices = self.engine.list_voices()
        if not name or name not in all_voices:
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
        await self.client.close()
