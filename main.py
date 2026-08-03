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
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

try:  # 仅用于类型标注，缺失也不影响运行
    from astrbot.api.provider import LLMResponse
except Exception:  # noqa: BLE001
    LLMResponse = object  # type: ignore

from .core.tts_engine import TtsEngine, is_speakable
from .cosyvoice.client import CosyVoiceClient, CosyVoiceServerError
from .utils import audio

PLUGIN_ID = "astrbot_plugin_cosyvoice"

# 语音服务器连不上时统一给用户的提示（大模型也需要能看懂这是服务器故障）
SERVER_DOWN_TIP = "🎙️ 语音服务器失联了，可以稍后再试或者联系管理员~（文字照常发送）"
# 服务端报错后的熔断冷却时长（秒）：冷却期内本插件不再向服务端发请求，只发文字，
# 避免服务端已坏时每条消息都去打、反复刷 ReadError。冷却到期后再试一次，成功则恢复。
SERVER_COOLDOWN_SEC = 30.0


@register(PLUGIN_ID, "Yours", "接入本地 CosyVoice3，让机器人以可配置音色朗读回复", "1.0.0")
class CosyVoicePlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        # AstrBot 在实例化插件时通过 __init__ 注入完整配置（含 template_list 类型）。
        # 部分版本也保留 context.get_config()，这里做兼容：优先用注入配置，回退到 get_config()。
        self._injected_config = config if config is not None else (self.context.get_config() or {})
        self.config = self._injected_config
        self.client = CosyVoiceClient(
            base_url=self.config.get("base_url", "http://127.0.0.1:50002"),
            sample_rate=int(self.config.get("sample_rate", 24000)),
            timeout=int(self.config.get("timeout", 150)),
        )
        self.engine = TtsEngine(
            self.config, self.client,
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

    # ---------- 事件标记辅助 ----------
    def _key(self, event: AstrMessageEvent):
        return getattr(event, "message_id", None) or event.unified_msg_origin

    def _set_flag(self, event: AstrMessageEvent, k: str, v=True):
        self._flags.setdefault(self._key(event), {})[k] = v

    def _get_flag(self, event: AstrMessageEvent, k: str, default=False):
        return self._flags.get(self._key(event), {}).get(k, default)

    def _clear(self, event: AstrMessageEvent):
        self._flags.pop(self._key(event), None)

    def _mark_server_ok(self):
        """合成成功：解除熔断冷却，并复位失联提示标志，便于下次真的失联时再提示。"""
        self._server_cooldown_until = 0.0
        self._server_down = False

    def _trip_breaker(self, chain: list):
        """合成失败：进入熔断冷却，冷却期内本插件不再向服务端发请求（只发文字）。

        首次进入冷却时在当前链追加一条一次性提示，避免每条消息都刷屏。
        冷却时长见 SERVER_COOLDOWN_SEC。
        """
        self._server_cooldown_until = time.time() + SERVER_COOLDOWN_SEC
        if not self._server_down:
            self._server_down = True
            chain.append(Comp.Plain(SERVER_DOWN_TIP))


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
            base = bool(is_llm and (auto or want or session_on))
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

    async def _realtime_send(self, event: AstrMessageEvent, records: list):
        """后台补发一段语音：优先主动推送一条独立消息，让语音不依赖结果链、不被阻塞。

        发送顺序（取第一个可用且成功的）：
        1) event.send：部分平台/版本支持的事件级主动发送；
        2) self.context.send_message(unified_msg_origin, chain)：AstrBot 官方主动消息 API，
           通用性最好，但「某些平台可能不支持主动消息发送」。

        不再回退到 result.chain.extend：本插件语音在后台任务中发送，此时 on_decorating_result
        早已 return、结果链已被 AstrBot 发出，extend 是无效操作且会静默丢语音；故不可用时
        直接记 WARNING，由调用方决定是否影响（voice_only 下文字已由 LLM 历史兜底，不丢上下文）。
        """
        chain = event.chain_result(records)
        send = getattr(event, "send", None)
        if callable(send):
            try:
                await send(chain)
                return
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[cosyvoice] event.send 实时发送失败，尝试 context.send_message: {e}")
        try:
            await self.context.send_message(event.unified_msg_origin, chain)
            return
        except Exception as e:  # noqa: BLE001
            logger.warning(
                f"[cosyvoice] 语音后台补发失败（平台可能不支持主动消息）: {e}"
                f" | unified_msg_origin={event.unified_msg_origin}"
            )

    # ---------- LLM 回复钩子：标记 + 关键词触发 ----------
    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, resp: LLMResponse):
        self._refresh_cfg()
        self._set_flag(event, "is_llm", True)

        # 记住本轮模型原文，供结果链文本缺失/异常（如混入 [] 占位符）时回退合成。
        # 无论是否为空都覆盖写入，避免回退时误用上一轮的真实文本（tts_on 下会念错内容）。
        resp_text = getattr(resp, "completion_text", None) or getattr(resp, "text", "") or ""
        if isinstance(resp_text, list):
            resp_text = "".join(str(x) for x in resp_text)
        self._last_llm[event.unified_msg_origin] = resp_text

        cfg = self.config

        # 关键词触发
        if cfg.get("enable_user_trigger", True):
            msg = event.message_str or ""
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

    # ---------- 装饰结果钩子：不阻塞管线，文字立刻发，语音后台补 ----------
    @filter.on_decorating_result()
    async def on_decorating_result(self, event: AstrMessageEvent):
        cfg = self._refresh_cfg()

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
        if not is_speakable(full_text):
            # 结果链文本无效（空 / [] 占位符等）：回退用本轮模型原文合成
            fb = self._last_llm.get(event.unified_msg_origin, "")
            if is_speakable(fb):
                logger.debug("[cosyvoice] 结果链文本无效，回退使用本轮模型原文合成语音")
                full_text = fb
            else:
                return

        # 已含语音则不重复
        if any(isinstance(c, Comp.Record) for c in chain):
            return

        # 服务端熔断冷却期：仅发文字，后台也不再打服务端
        if self._server_cooldown_until and time.time() < self._server_cooldown_until:
            return

        # 本轮同一条消息若已成功合成过（框架可能重复触发 on_decorating_result），
        # 直接跳过，避免重复打服务端、服务端过载、以及把偶发失败误报成「服务器失联」。
        origin = event.unified_msg_origin
        done = self._decorated.get(origin, set())
        if full_text in done:
            return

        voice = self._get_flag(event, "voice", None) or self._session_voice(event)
        send_mode = cfg.get("send_mode", "both")
        merge = bool(cfg.get("segment_merge", False))
        voice_name, _, _ = self.engine.resolve_voice(voice)

        # voice_only：纯内存操作（很快、不阻塞），把原文从结果链移除，只让后台发语音；
        # 文字已由 LLM completion_text 存入会话历史，不丢上下文。both 模式保留原文。
        if send_mode == "voice_only":
            result.chain = [c for c in chain if not isinstance(c, Comp.Plain)]

        logger.info(
            f"[cosyvoice] 文字先发，语音转入后台合成 | 音色={voice_name} "
            f"总长度={len(full_text)}字 模式={'合并' if merge else '逐段发送'} "
            f"send_mode={send_mode}"
        )

        # 关键：本钩子【不 await 合成】，文字立刻交给 AstrBot 发出，避免占用事件循环、
        # 卡住同一会话/全局的其他消息。语音合成放到后台任务，合成完再主动 event.send 补发。
        # 这样「等一会儿」可接受，但绝不阻塞其他事件。
        task = asyncio.ensure_future(
            self._background_speak(event, full_text, voice, send_mode, merge, origin)
        )
        # 避免「未等待的 Task 异常」警告刷屏
        task.add_done_callback(self._log_task_exc)
        self._clear(event)

    def _log_task_exc(self, task):
        try:
            exc = task.exception()
        except (asyncio.CancelledError, Exception):
            exc = None
        if exc and not isinstance(exc, asyncio.CancelledError):
            logger.error(f"[cosyvoice] 后台语音任务异常: {exc}")

    async def _background_speak(
        self, event: AstrMessageEvent, full_text: str,
        voice, send_mode: str, merge: bool, origin: str,
    ):
        """后台补发语音：不阻塞 on_decorating_result，文字已由 AstrBot 先发出。

        合成成功后主动 event.send 逐段/整段语音；失败仅记日志，不影响已发出的文字。
        结果链在钩子返回后已由 AstrBot 发送，此处不再改动结果链（避免无效写入）。
        voice_only 模式下文字已从结果链移除，若语音彻底失败，退化为补发文字，
        避免前端静默（文字仍由 LLM 历史兜底，这里只是确保用户看得到）。
        """
        cfg = self.config
        try:
            if merge:
                path = await self.engine.synthesize(full_text, voice)
                if not path:
                    logger.warning("[cosyvoice] 后台合成失败，仅发送文本")
                    await self._fallback_text(event, full_text, send_mode)
                    return
                audio.schedule_cleanup(path)
                await self._realtime_send(event, [Comp.Record(file=path, url=path)])
            else:
                sent_any = False
                async for wav in self.engine.iter_segment_wavs(full_text, voice):
                    sent_any = True
                    rec = Comp.Record(file=wav, url=wav)
                    audio.schedule_cleanup(wav)
                    await self._realtime_send(event, [rec])
                if not sent_any:
                    logger.warning("[cosyvoice] 后台合成失败，仅发送文本")
                    await self._fallback_text(event, full_text, send_mode)
                    return
            # 整轮成功后才登记幂等 + 解除冷却
            self._mark_server_ok()
            self._decorated.setdefault(origin, set()).add(full_text)
            bucket = self._decorated.get(origin)
            if bucket and len(bucket) > 20:
                self._decorated[origin] = set(list(bucket)[-20:])
            logger.info("[cosyvoice] 后台语音合成完成，已补发")
        except CosyVoiceServerError:
            logger.warning("[cosyvoice] 语音服务器失联，进入冷却，仅发送文本")
            self._trip_breaker([])
            await self._fallback_text(event, full_text, send_mode)
        except Exception as e:
            logger.warning(f"[cosyvoice] 后台语音合成失败（本条跳过，下条重试）: {e}")
            await self._fallback_text(event, full_text, send_mode)

    async def _fallback_text(self, event: AstrMessageEvent, full_text: str, send_mode: str):
        """语音彻底失败时，把文字补发回去，避免 voice_only 模式前端静默。

        - both 模式：文字已在结果链由 AstrBot 正常发出，无需补发，直接返回。
        - voice_only 模式：文字已从结果链移除，若语音失败则需主动把文字发出来，
          让用户至少看得到（文字也由 LLM completion_text 存入会话历史，不丢上下文）。
        使用 context.send_message（官方主动消息 API），你平台已确认支持。
        """
        if send_mode != "voice_only":
            return
        if not full_text:
            return
        try:
            await self.context.send_message(
                event.unified_msg_origin, event.chain_result([Comp.Plain(full_text)])
            )
            logger.info("[cosyvoice] voice_only 语音失败，已退化为补发文字")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[cosyvoice] voice_only 失败兜底补发文字也失败: {e}")

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

        # 服务端熔断冷却期内，直接提示，不再去打已坏的服务端
        if self._server_cooldown_until and time.time() < self._server_cooldown_until:
            yield event.plain_result(SERVER_DOWN_TIP)
            return

        self._set_flag(event, "suppress", True)
        send_mode = self.config.get("send_mode", "both")
        merge = bool(self.config.get("segment_merge", False))
        try:
            if merge:
                path = await self.engine.synthesize(text, self._session_voice(event))
                if not path:
                    yield event.plain_result("哎呀，话到嘴边卡壳了，这次没念出来，稍后再试试？")
                    return
                if send_mode == "both":
                    yield event.chain_result([Comp.Plain(text), Comp.Record(file=path, url=path)])
                else:
                    yield event.chain_result([Comp.Record(file=path, url=path)])
                audio.schedule_cleanup(path)
            else:
                # 不合并：边合成边逐段 yield（每段独立一条消息，服务端返回一段即发一段）
                if send_mode == "both":
                    yield event.plain_result(text)
                sent = False
                async for wav in self.engine.iter_segment_wavs(text, self._session_voice(event)):
                    sent = True
                    yield event.chain_result([Comp.Record(file=wav, url=wav)])
                    audio.schedule_cleanup(wav)
                if not sent:
                    yield event.plain_result("哎呀，话到嘴边卡壳了，这次没念出来，稍后再试试？")
        except CosyVoiceServerError:
            self._trip_breaker([])
            yield event.plain_result(SERVER_DOWN_TIP)
            return
        except Exception as e:
            # 偶发错误（如连接中途断开）不进熔断，仅提示本条失败，可重试
            logger.warning(f"[cosyvoice] /tts 合成失败（本条跳过）: {e}")
            yield event.plain_result("哎呀，话到嘴边卡壳了，这次没念出来，稍后再试试？")
            return

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
        lines = [
            f"自动语音开关：{mode_hint}",
            f"当前聊天音色：{session_voice or '（未单独设置）'}",
            f"全局默认音色：{default_voice or '（未配置）'}",
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

        Args:
            text(string): 需要朗读的文本内容
            voice(string): 可选，指定音色名；留空则使用默认音色
        """
        self._refresh_cfg()
        if not self.config.get("enable_llm_tool", True):
            yield event.plain_result("语音功能还没开呢，先把它打开我就能念啦～")
            return

        # 服务端熔断冷却期内，直接提示，不再去打已坏的服务端
        if self._server_cooldown_until and time.time() < self._server_cooldown_until:
            yield event.plain_result(SERVER_DOWN_TIP)
            return

        self._set_flag(event, "suppress", True)
        if voice:
            self._set_flag(event, "voice", voice)

        if not is_speakable(text):
            # 模型可能没把正文放进 text 参数（如传入 [] 占位符），回退用本轮模型原文
            fb = self._last_llm.get(event.unified_msg_origin, "")
            if is_speakable(fb):
                text = fb
            else:
                yield event.plain_result("你想让我念点啥呀？把文字发给我就行～")
                return

        target_voice = voice or self._session_voice(event)
        send_mode = self.config.get("send_mode", "both")
        merge = bool(self.config.get("segment_merge", False))
        try:
            if merge:
                path = await self.engine.synthesize(text.strip(), target_voice)
                if not path:
                    yield event.plain_result("话到嘴边卡壳了，这次没念出来，待会儿再试试？")
                    return
                if send_mode == "both":
                    yield event.chain_result([Comp.Plain(text), Comp.Record(file=path, url=path)])
                else:
                    yield event.chain_result([Comp.Record(file=path, url=path)])
                audio.schedule_cleanup(path)
            else:
                # 不合并：边合成边逐段 yield（每段独立一条消息，服务端返回一段即发一段）
                if send_mode == "both":
                    yield event.plain_result(text)
                sent = False
                async for wav in self.engine.iter_segment_wavs(text.strip(), target_voice):
                    sent = True
                    yield event.chain_result([Comp.Record(file=wav, url=wav)])
                    audio.schedule_cleanup(wav)
                if not sent:
                    yield event.plain_result("话到嘴边卡壳了，这次没念出来，待会儿再试试？")
        except CosyVoiceServerError:
            self._trip_breaker([])
            yield event.plain_result(SERVER_DOWN_TIP)
            return
        except Exception as e:
            # 偶发错误（如连接中途断开）不进熔断，仅提示本条失败，可重试
            logger.warning(f"[cosyvoice] text_to_speech 合成失败（本条跳过）: {e}")
            yield event.plain_result("话到嘴边卡壳了，这次没念出来，待会儿再试试？")
            return

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
        await self.client.close()
