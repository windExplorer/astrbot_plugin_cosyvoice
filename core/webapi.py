"""WebUI 后端 API：为 AstrBot 插件 Pages 提供 CosyVoice 管理接口。

路由由 main.py 在插件 initialize() 阶段通过 context.register_web_api() 注册，
统一前缀 ``/astrbot_plugin_cosyvoice/``（route 需带插件名；Page 端 bridge
endpoint 不带前缀）。

约定：
- 所有 handler 使用 astrbot.api.web 的 request / json_response / error_response；
- 会话（unified_msg_origin）维度的开关/音色/发送方式直接复用插件内存态
  （plugin._sessions / _voices / _sendmodes），写操作调用插件的 _save_*
  持久化到 data/ 目录（与聊天指令 /tts_on 等同源，热生效、重启不丢）；
- 配置（v2.4.0 起）：GET /config 返回 _conf_schema.json + 当前值（schema 驱动，
  界面永不缺键）；POST /config 写入 AstrBot 注入的 AstrBotConfig 并 save_config
  持久化——与官方「插件配置」弹窗同一份文件，双向一致、热生效；
- 音色（voices）由 WebUI 音色库（data/tts_voices_lib.json）+ 配置 voices 合成生效，
  增删改走 voices/create|update|delete，不经过配置接口。
"""

from __future__ import annotations

import json
import os

from astrbot.api import logger
from astrbot.api.web import error_response, json_response, request

# 与 main.py 的 PLUGIN_ID 保持一致；route 前缀必须带插件名
PLUGIN_ROUTE_PREFIX = "astrbot_plugin_cosyvoice"


def register_web_apis(plugin) -> None:
    """把 WebUI 后端 API 注册到插件 Context。

    :param plugin: CosyVoicePlugin 实例（提供 engine/_sessions/_voices/…）。
    """
    ctx = plugin.context
    p = PLUGIN_ROUTE_PREFIX

    ctx.register_web_api(f"/{p}/overview", _overview(plugin), ["GET"], "CosyVoice 概览（服务端健康/全局开关）")
    ctx.register_web_api(f"/{p}/config", _get_config(plugin), ["GET"], "CosyVoice 配置（schema 驱动读取）")
    ctx.register_web_api(f"/{p}/config", _save_config(plugin), ["POST"], "CosyVoice 配置（保存，热生效）")
    ctx.register_web_api(f"/{p}/segment_preview", _segment_preview(plugin), ["POST"], "分段试算（纯切分不触服务端）")
    ctx.register_web_api(f"/{p}/voices", _list_voices(plugin), ["GET"], "CosyVoice 音色列表")
    ctx.register_web_api(f"/{p}/voices/create", _create_voice(plugin), ["POST"], "新增音色")
    ctx.register_web_api(f"/{p}/voices/update", _update_voice(plugin), ["POST"], "编辑音色")
    ctx.register_web_api(f"/{p}/voices/delete", _delete_voice(plugin), ["POST"], "删除音色")
    ctx.register_web_api(f"/{p}/voices/default", _set_default_voice(plugin), ["POST"], "设置默认音色")
    ctx.register_web_api(f"/{p}/sessions", _list_sessions(plugin), ["GET"], "会话语音状态列表")
    ctx.register_web_api(f"/{p}/sessions/set", _set_session(plugin), ["POST"], "按会话设置语音开关/音色/发送方式")
    ctx.register_web_api(f"/{p}/sessions/delete", _delete_session(plugin), ["POST"], "删除会话语音状态")
    ctx.register_web_api(f"/{p}/sessions/clear", _clear_sessions(plugin), ["POST"], "清空全部会话语音状态")
    ctx.register_web_api(f"/{p}/avatars", _list_avatars(plugin), ["GET"], "批量取会话头像（data URI）")
    ctx.register_web_api(f"/{p}/synthesize", _synthesize(plugin), ["GET", "POST"], "合成试听（wav 下载或 inline data URI）")
    ctx.register_web_api(f"/{p}/translate", _translate_config(plugin), ["GET", "POST"], "翻译配置（读取/保存）")
    ctx.register_web_api(f"/{p}/translate/test", _translate_test(plugin), ["POST"], "翻译配置测试")
    logger.info(f"[cosyvoice] WebUI API 已注册（前缀 /api/plug/{p}/）")


# ---------- 概览 ----------
def _overview(plugin):
    async def handler():
        plugin._refresh_cfg()
        cfg = plugin.config
        import time

        # 服务端健康：读取 Router 的节点实时状态
        servers = []
        router = plugin.client
        try:
            nodes = getattr(router, "_nodes", None)
            if isinstance(nodes, (list, tuple)):
                for n in nodes:
                    if not isinstance(n, dict):
                        continue
                    cooldown = float(n.get("cooldown_until", 0.0) or 0.0)
                    remaining = max(0.0, cooldown - time.time())
                    failed = int(n.get("failed", 0) or 0)
                    servers.append({
                        "url": n.get("url", ""),
                        "enabled": True,
                        "default": bool(n.get("default", False)),
                        "weight": n.get("weight", 1),
                        "failed": failed,
                        "cooldown_remaining": round(remaining, 1),
                        "status": "cooldown" if remaining > 0 else ("degraded" if failed else "ok"),
                    })
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[cosyvoice] 读取服务端节点状态失败: {e}")
        if not servers:
            servers = [{
                "url": cfg.get("base_url", ""),
                "enabled": True,
                "default": True,
                "weight": 1,
                "failed": 0,
                "cooldown_remaining": 0.0,
                "status": "ok",
            }]

        # 熔断冷却（插件级，与服务端节点冷却不同）
        cooldown_until = getattr(plugin, "_server_cooldown_until", 0.0)
        remaining = max(0.0, cooldown_until - time.time()) if cooldown_until else 0.0

        # 前端概览面板（OverviewPanel）期望的扁平字段：与前端契约对齐
        sm_mode = cfg.get("send_mode", "both")
        client_ready = not bool(getattr(plugin, "_server_down", False))
        client_error = ""
        if getattr(plugin, "_server_cooldown_until", 0.0) and time.time() < plugin._server_cooldown_until:
            client_error = "语音服务端冷却中（近期合成失败），稍后自动恢复"
        send_modes = ["语音+文字", "仅语音"] if sm_mode == "both" else ["仅语音", "语音+文字"]

        return json_response({
            # ---- OverviewPanel 概览（扁平字段，与前端契约对齐）----
            "client_ready": client_ready,
            "client_error": client_error,
            "base_url": cfg.get("base_url", ""),
            "default_voice": plugin._effective_default_voice(),
            "send_mode": sm_mode,
            "concurrent_sessions": int(cfg.get("tts_concurrency", 1) or 1),
            "servers_count": len(servers),
            "voices_count": len(plugin.engine.voices),
            "auto_tts_enabled": bool(cfg.get("auto_tts", False)),
            "auto_tts_reply": bool(cfg.get("auto_tts", False)),
            "auto_tts_keywords": list(cfg.get("trigger_keywords", []) or []),
            "auto_tts_mention": bool(cfg.get("enable_user_trigger", True)),
            "send_modes": send_modes,
            "recent_events": list(getattr(plugin, "_recent_events", []) or []),
            # ---- 兼容/扩展字段（其他面板或后续用）----
            "servers": servers,
            "cooldown_remaining": round(remaining, 1),
            "server_down": bool(getattr(plugin, "_server_down", False)),
            "config": {
                "auto_tts": bool(cfg.get("auto_tts", False)),
                "send_mode": cfg.get("send_mode", "both"),
                "tts_scope": cfg.get("tts_scope", "llm_only"),
                "enable_llm_tool": bool(cfg.get("enable_llm_tool", True)),
                "enable_user_trigger": bool(cfg.get("enable_user_trigger", True)),
                "default_voice": plugin._effective_default_voice(),
                "sample_rate": int(cfg.get("sample_rate", 24000)),
                "base_url": cfg.get("base_url", ""),
                "server_voices_dir": cfg.get("server_voices_dir", "") or "",
                "segment_merge": bool(cfg.get("segment_merge", False)),
                "tts_concurrency": int(cfg.get("tts_concurrency", 1) or 1),
                "tts_cooldown_sec": float(cfg.get("tts_cooldown_sec", 30)),
                "timeout": int(cfg.get("timeout", 150)),
            },
            "voice_count": len(plugin.engine.voices),
            "session_count": len(plugin._sessions),
        })

    return handler


# ---------- 配置（schema 驱动，可查看/编辑） ----------
# 配置组映射：key → 组名。刻意与 _conf_schema.json 的键**全量**对齐——
# 新增配置键只需改这张表（漏掉的键自动落进「其他」组，不会从界面上消失）。
_CONFIG_GROUPS: dict[str, list[str]] = {
    "服务端": [
        "base_url", "servers", "server_voices_dir", "sample_rate",
        "timeout", "tts_queue_max_position",
    ],
    "触发与范围": [
        "auto_tts", "tts_scope", "agent_intermediate_tts", "enable_llm_tool",
        "enable_user_trigger", "trigger_keywords", "text_keywords",
        "blocklist", "allowlist", "translate_display_mode",
    ],
    "发送与展示": [
        "send_mode", "text_after_voice", "skip_bracket_tts",
        "send_via_file_service", "file_service_transcode_mp3",
        "file_service_mp3_bitrate", "file_service_token_ttl",
    ],
    "分段": [
        "segment_len", "segment_first_len", "segment_punct",
        "segment_first_nopunct", "segment_nopunct", "segment_merge",
        "max_text_len", "split_by_newline", "newline_min_len",
        "text_split_keep_newline",
    ],
    "音效标记": ["auto_breath", "auto_effect", "effect_intensity"],
    "服务与容错": [
        "default_voice", "tts_concurrency", "tts_max_retry",
        "tts_retry_backoff", "tts_cooldown_sec",
    ],
}

# 已分组的 key 反查集合（用于把未覆盖的键归入「其他」）
_GROUPED_KEYS = {k for keys in _CONFIG_GROUPS.values() for k in keys}

_CONF_SCHEMA_CACHE: dict = {"path": None, "data": None}


def _load_conf_schema(plugin) -> dict:
    """读取插件根目录的 _conf_schema.json（带缓存；读不到返回空 dict）。"""
    import os as _os

    path = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
                         "_conf_schema.json")
    cached = _CONF_SCHEMA_CACHE
    if cached["path"] == path and cached["data"] is not None:
        return cached["data"]
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[cosyvoice] 读取 _conf_schema.json 失败（配置页将退化为纯展示）: {e}")
        data = {}
    _CONF_SCHEMA_CACHE["path"] = path
    _CONF_SCHEMA_CACHE["data"] = data
    return data


def _build_config_groups(cfg: dict) -> dict:
    """按 _CONFIG_GROUPS 归组；未覆盖的键自动归入「其他」，保证不漏键。"""
    groups: dict[str, list[str]] = {}
    for gname, keys in _CONFIG_GROUPS.items():
        hit = [k for k in keys if k in cfg]
        if hit:
            groups[gname] = hit
    others = [k for k in cfg if k not in _GROUPED_KEYS and k != "voices"]
    if others:
        groups["其他"] = others
    return groups


def _coerce_config_value(key: str, value, sch: dict):
    """按 schema 类型强转/校验单个配置值。合法返回 (True, 值)；非法返回 (False, 错误信息)。"""
    t = str(sch.get("type") or "string").strip().lower()
    if t == "bool":
        if isinstance(value, bool):
            return True, value
        s = str(value).strip().lower()
        if s in ("true", "1", "yes", "on"):
            return True, True
        if s in ("false", "0", "no", "off", ""):
            return True, False
        return False, f"{key}: 无法识别的布尔值 {value!r}"
    if t == "int":
        try:
            return True, int(float(value))
        except (TypeError, ValueError):
            return False, f"{key}: 必须是整数，收到 {value!r}"
    if t == "float":
        try:
            return True, float(value)
        except (TypeError, ValueError):
            return False, f"{key}: 必须是数字，收到 {value!r}"
    if t == "string":
        s = str(value)
        options = sch.get("options")
        if options and s not in options:
            return False, f"{key}: 只能是 {'/'.join(map(str, options))} 之一，收到 {s!r}"
        return True, s
    if t == "list":
        if not isinstance(value, list):
            return False, f"{key}: 必须是数组，收到 {type(value).__name__}"
        return True, [str(v) for v in value]
    if t == "template_list":
        # 目前只有 servers 用到：条目结构 {url, enabled, default, weight}
        if not isinstance(value, list):
            return False, f"{key}: 必须是数组，收到 {type(value).__name__}"
        out = []
        for i, item in enumerate(value):
            if not isinstance(item, dict):
                return False, f"{key}[{i}]: 必须是对象"
            out.append({
                "url": str(item.get("url") or "").strip(),
                "enabled": bool(item.get("enabled", True)),
                "default": bool(item.get("default", False)),
                "weight": _coerce_config_value(f"{key}[{i}].weight", item.get("weight", 1),
                                               {"type": "int"})[1],
            })
        return True, out
    return True, value  # 未知类型放行（保持原值）


def _get_config(plugin):
    async def handler():
        plugin._refresh_cfg()
        cfg = plugin.config
        return json_response({
            "values": dict(cfg),
            "schema": _load_conf_schema(plugin),
            "groups": _build_config_groups(cfg),
            "effective_default_voice": plugin._effective_default_voice(),
        })

    return handler


def _save_config(plugin):
    """保存插件配置（写入 AstrBot 注入的 AstrBotConfig 并持久化，热生效）。

    与官方「插件配置」弹窗写的是同一份配置文件（data/config/<名>_config.json），
    双向一致；保存后 _refresh_cfg() 让引擎/路由立即吃到新值。
    """
    async def handler():
        payload = await request.json(default={})
        if not isinstance(payload, dict) or not payload:
            return error_response("缺少要保存的配置键值对", status_code=400)
        schema = _load_conf_schema(plugin)
        if not schema:
            return error_response("无法读取 _conf_schema.json，配置编辑不可用", status_code=500)

        coerced: dict = {}
        for key, value in payload.items():
            key = str(key)
            if key == "voices":
                return error_response("音色请在「音色管理」页维护，不能通过配置接口修改", status_code=400)
            sch = schema.get(key)
            if sch is None:
                return error_response(f"未知配置键「{key}」（不在 _conf_schema.json 中）", status_code=400)
            ok, result = _coerce_config_value(key, value, sch)
            if not ok:
                return error_response(result, status_code=400)
            coerced[key] = result
        if not coerced:
            return error_response("没有可保存的配置键", status_code=400)

        conf = plugin._injected_config
        if conf is None or not isinstance(conf, dict):
            return error_response("插件配置对象不可用（非 AstrBotConfig 注入）", status_code=500)
        conf.update(coerced)
        saved = False
        try:
            save_async = getattr(conf, "save_config_async", None)
            if save_async is not None:
                await save_async()
                saved = True
            else:
                conf.save_config()
                saved = True
        except Exception as e:  # noqa: BLE001
            # 异步保存失败再试同步保存（AstrBotConfig 两者都有；极端情况落盘失败仍热生效）
            try:
                conf.save_config()
                saved = True
            except Exception as e2:  # noqa: BLE001
                logger.error(f"[cosyvoice] WebUI 保存配置落盘失败: {e2}（首次异常: {e}）")
                return error_response(f"配置已热生效但落盘失败：{e2}", status_code=500)
        plugin._refresh_cfg()
        logger.info(f"[cosyvoice] WebUI 保存配置 {list(coerced)}（落盘={saved}）")
        return json_response({
            "ok": True,
            "values": {k: plugin.config.get(k) for k in coerced},
        })

    return handler


# ---------- 分段试算（纯切分，不触服务端） ----------
def _segment_preview(plugin):
    async def handler():
        payload = await request.json(default={})
        text = str(payload.get("text") or "").strip()
        if not text:
            return error_response("缺少试算文本 text", status_code=400)
        if len(text) > 20000:
            return error_response("试算文本过长（>20000 字），请截取片段", status_code=400)
        plugin._refresh_cfg()
        engine = plugin.engine
        try:
            voice_segs = engine.split_text(text)
            pairs = engine.split_text_pairs(text)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[cosyvoice] 分段试算异常: {e}")
            return error_response(f"切分异常：{e}", status_code=500)
        cfg = plugin.config
        effective = {
            "segment_len": cfg.get("segment_len", "0"),
            "segment_first_len": cfg.get("segment_first_len", "0"),
            "segment_punct": (str(cfg.get("segment_punct", "") or "").strip() or "。！？.!?（默认）"),
            "segment_first_nopunct": cfg.get("segment_first_nopunct", "truncate"),
            "segment_nopunct": cfg.get("segment_nopunct", "truncate"),
            "segment_merge": bool(cfg.get("segment_merge", False)),
            "max_text_len": plugin.engine._seg_hard_cap(),
            "split_by_newline": bool(cfg.get("split_by_newline", True)),
            "newline_min_len": engine._newline_min_len(),
            "text_split_keep_newline": bool(cfg.get("text_split_keep_newline", True)),
        }
        return json_response({
            "voice_segs": [{"text": s, "len": len(s)} for s in voice_segs],
            "pairs": [
                {
                    "voice": v,
                    "len": len(v),
                    "texts": [{"text": t, "len": len(t)} for t in ts],
                }
                for v, ts in pairs
            ],
            "effective": effective,
        })

    return handler
def _list_voices(plugin):
    async def handler():
        plugin._refresh_cfg()
        default = plugin._effective_default_voice()
        result = []
        for name, v in plugin.engine.voices.items():
            result.append({
                "name": name,
                "prompt_wav": v.get("prompt_wav", ""),
                "prompt_text": v.get("prompt_text", ""),
                "language": v.get("language", "") or "",
                "hidden": bool(v.get("hidden", False)),
                # 音色级副语言标记开关（false=该音色不注入 [breath] 等标记）
                "markup": bool(v.get("markup", True)),
                "is_default": name == default,
                "in_lib": name in plugin._voices_lib,  # 是否由 WebUI 管理
                # 本地能否解析到参考音频（排查用）
                "wav_resolved": bool(plugin.engine.resolve_wav(v.get("prompt_wav", ""))),
            })
        return json_response({"voices": result, "default_voice": default})

    return handler


# ---------- 新增音色（WebUI 音色库） ----------
def _create_voice(plugin):
    async def handler():
        payload = await request.json(default={})
        name = str(payload.get("name") or "").strip()
        prompt_wav = str(payload.get("prompt_wav") or "").strip()
        prompt_text = str(payload.get("prompt_text") or "").strip()
        language = str(payload.get("language") or "").strip().lower()
        hidden = bool(payload.get("hidden", False))
        markup = bool(payload.get("markup", True))
        if not name:
            return error_response("音色名不能为空", status_code=400)
        if name in plugin._effective_voices():
            return error_response(f"音色「{name}」已存在", status_code=400)
        plugin._voices_lib[name] = {
            "prompt_wav": prompt_wav,
            "prompt_text": prompt_text,
            "language": language,
            "hidden": hidden,
            "markup": markup,
        }
        plugin._save_voices_lib()
        plugin._refresh_cfg()
        logger.info(f"[cosyvoice] WebUI 新增音色「{name}」")
        return json_response({"ok": True})

    return handler


# ---------- 编辑音色 ----------
def _update_voice(plugin):
    async def handler():
        payload = await request.json(default={})
        name = str(payload.get("name") or "").strip()
        if not name or name not in plugin._effective_voices():
            return error_response(f"没有「{name}」这个音色", status_code=400)
        entry = dict(plugin._voices_lib.get(name, {
            "prompt_wav": plugin.engine.voices.get(name, {}).get("prompt_wav", ""),
            "prompt_text": plugin.engine.voices.get(name, {}).get("prompt_text", ""),
            "language": plugin.engine.voices.get(name, {}).get("language", ""),
            "hidden": bool(plugin.engine.voices.get(name, {}).get("hidden", False)),
            "markup": bool(plugin.engine.voices.get(name, {}).get("markup", True)),
        }))
        if "prompt_wav" in payload:
            entry["prompt_wav"] = str(payload["prompt_wav"] or "").strip()
        if "prompt_text" in payload:
            entry["prompt_text"] = str(payload["prompt_text"] or "").strip()
        if "hidden" in payload:
            entry["hidden"] = bool(payload["hidden"])
        if "language" in payload:
            entry["language"] = str(payload["language"] or "").strip().lower()
        if "markup" in payload:
            entry["markup"] = bool(payload["markup"])
        plugin._voices_lib[name] = entry
        plugin._save_voices_lib()
        plugin._refresh_cfg()
        logger.info(f"[cosyvoice] WebUI 编辑音色「{name}」")
        return json_response({"ok": True})

    return handler


# ---------- 删除音色（二次确认由前端弹窗保证） ----------
def _delete_voice(plugin):
    async def handler():
        payload = await request.json(default={})
        name = str(payload.get("name") or "").strip()
        if not name:
            return error_response("缺少音色名", status_code=400)
        if name not in plugin._effective_voices():
            return error_response(f"没有「{name}」这个音色", status_code=400)
        # 仅能删除 WebUI 音色库中的音色（配置里的由配置页管理）
        if name not in plugin._voices_lib:
            return error_response(f"「{name}」来自插件配置，请在 AstrBot 配置弹窗中删除", status_code=400)
        del plugin._voices_lib[name]
        plugin._save_voices_lib()
        # 若删的是默认音色，清掉 override
        if plugin._default_voice_override == name:
            plugin._default_voice_override = ""
            plugin._save_default_voice_override("")
        plugin._refresh_cfg()
        logger.info(f"[cosyvoice] WebUI 删除音色「{name}」")
        return json_response({"ok": True})

    return handler


# ---------- 设置默认音色 ----------
def _set_default_voice(plugin):
    async def handler():
        payload = await request.json(default={})
        name = str(payload.get("name") or "").strip()
        all_voices = plugin.engine.list_voices(include_hidden=True)
        if not name or name not in all_voices:
            return error_response(f"没有「{name}」这个音色", status_code=400)
        # 写入插件自有 data/（与 AstrBot 主配置解耦），立即热生效、重启不丢；
        # 聊天/WebUI 均可覆盖。清除用空串走 _set_default_voice_clear（见下）。
        plugin._default_voice_override = name
        plugin._save_default_voice_override(name)
        plugin._refresh_cfg()  # 让 engine.config 的 default_voice 立即更新
        return json_response({"ok": True, "default_voice": name})

    return handler


def _fmt_origin(origin: str) -> str:
    """把 unified_msg_origin 转成人能读的标签：群聊只给群号、私聊只给 QQ 号。"""
    p = _parse_origin(origin)
    if p["is_group"]:
        return f"群 {p['group_id']}"
    if p["user_id"]:
        return f"QQ {p['user_id']}"
    return str(origin)


def _parse_origin(origin: str) -> dict:
    """把 unified_msg_origin 解析成结构化字段（best-effort），供 WebUI 友好展示。

    AstrBot 的 UMO 形如 ``<platform_id>:<message_type>:<session_id>``：

      - 群聊：``aiocqhttp:GroupMessage:123456789`` → 会话 ID 是**群号**
      - 私聊：``aiocqhttp:FriendMessage:10001``    → 会话 ID 是**对方 QQ 号**

    历史坑：旧实现误按「平台:群号:QQ号」解析，于是把 ``GroupMessage`` / ``FriendMessage``
    当成了群号 —— 结果①所有会话都被判成群聊、②头像 id 取到 ``FriendMessage`` 这种字符串
    （判数字后为空）→ 头像永远拼不出 URL。此处按消息类型判定群/私聊。

    兼容性：段数少于 3 的旧式/异常 origin（``platform:session_id``）与未识别类型
    （temp/guild 等）一律按**私聊**处理——不猜群号，避免把 QQ 号显示成群号。
    """
    raw = str(origin or "")
    parts = raw.split(":")
    platform = parts[0] if parts else ""
    msg_type = parts[1] if len(parts) >= 3 else ""
    session_id = parts[-1] if len(parts) >= 2 else ""
    low = msg_type.strip().lower()
    if low.startswith("group"):
        is_group, group_id, user_id = True, session_id, ""
    else:
        # friend / private / direct / temp / guild / 未知 / 两段式 → 都是「对人」的会话
        is_group, group_id, user_id = False, "", session_id
    if is_group:
        label = f"群 {group_id}"
    elif user_id:
        label = f"QQ {user_id}"
    else:
        label = raw
    return {
        "platform": platform,
        "msg_type": msg_type,
        "session_id": session_id,
        "group_id": group_id,
        "user_id": user_id,
        "is_group": is_group,
        "label": label,
    }


# ---------- 会话列表 ----------
def _list_sessions(plugin):
    async def handler():
        plugin._refresh_cfg()
        # 汇总会话级状态：开关 / 音色 / 发送方式
        keys = set(plugin._sessions) | set(plugin._voices) | set(plugin._sendmodes)
        sessions = []
        for origin in sorted(keys):
            raw = plugin._sessions.get(origin)
            on = raw is not None and raw is not False
            sm = plugin._sendmodes.get(origin)
            mode = {"both": "语音+文字", "voice_only": "仅语音"}.get(sm, "默认(跟随全局)")
            prob = raw if isinstance(raw, (int, float)) else (1.0 if on else None)
            parsed = _parse_origin(origin)
            is_group = bool(parsed["is_group"])
            sessions.append({
                "id": origin,
                "user": parsed["label"],
                "platform": parsed["platform"],
                "msg_type": parsed["msg_type"],
                "session_id": parsed["session_id"],
                "group_id": parsed["group_id"],
                "user_id": parsed["user_id"],
                "label": parsed["label"],
                "nickname": getattr(plugin, "_nicknames", {}).get(origin, ""),
                # 群名（群聊才有；取不到时前端回退显示群号）
                "group_name": getattr(plugin, "_group_names", {}).get(origin, ""),
                "on": on,
                "mode": mode,
                "voice": plugin._voices.get(origin, "") or "默认",
                "prob": prob,
                # 头像要按「群 / 用户」分别取（群头像用群号、私聊头像用 QQ 号）：
                # 群聊取群头像更有辨识度，私聊取对方头像；配合 /avatars 端点给前端 data URI。
                "avatar_kind": "group" if is_group else "user",
                "avatar_id": parsed["group_id"] if is_group else parsed["user_id"],
                "is_group": is_group,
                # 展示用：概率百分比（None=未开启），前端直接就绪不必再算
                "prob_percent": None if prob is None else int(round(float(prob) * 100)),
            })
        return json_response({"sessions": sessions})

    return handler


# ---------- 按会话设置 ----------
def _set_session(plugin):
    async def handler():
        payload = await request.json(default={})
        origin = str(payload.get("origin") or "").strip()
        if not origin:
            return error_response("缺少 origin", status_code=400)

        # 语音开关 / 概率：二者同源（都写 plugin._sessions[origin]），语义与聊天指令
        # /tts_on 一致 —— True=常开（每句都念）、0<p<1=按概率念、不存在=关闭。
        # prob 优先于 on（前端概率滑杆走 prob，老的开关调用仍可只传 on）。
        on = payload.get("on")
        prob = payload.get("prob")
        if prob is not None:
            try:
                p = float(prob)
            except (TypeError, ValueError):
                return error_response("prob 必须是 0~1 之间的数字", status_code=400)
            if p <= 0:
                plugin._sessions.pop(origin, None)  # 0% 等同关闭
            elif p >= 1.0:
                plugin._sessions[origin] = True  # 常开
            else:
                plugin._sessions[origin] = round(p, 4)
            plugin._save_sessions()
        elif on is not None:
            if bool(on):
                plugin._sessions[origin] = True
            else:
                plugin._sessions.pop(origin, None)
            plugin._save_sessions()

        voice = payload.get("voice")
        if voice is not None:
            voice = str(voice).strip()
            all_voices = plugin.engine.list_voices(include_hidden=True)
            if voice and voice not in all_voices:
                return error_response(f"没有「{voice}」这个音色", status_code=400)
            if voice:
                plugin._voices[origin] = voice
            else:
                plugin._voices.pop(origin, None)
            plugin._save_voices()

        send_mode = payload.get("send_mode")
        if send_mode is not None:
            send_mode = str(send_mode).strip()
            if send_mode in ("both", "voice_only"):
                plugin._sendmodes[origin] = send_mode
                plugin._save_sendmodes()
            elif send_mode == "":
                plugin._sendmodes.pop(origin, None)
                plugin._save_sendmodes()
            else:
                return error_response("send_mode 只能是 both / voice_only / 空", status_code=400)

        return json_response({"ok": True})

    return handler


# ---------- 删除单个会话语音状态 ----------
def _delete_session(plugin):
    async def handler():
        payload = await request.json(default={})
        origin = str(payload.get("id") or payload.get("origin") or "").strip()
        if not origin:
            return error_response("缺少 id", status_code=400)
        plugin._sessions.pop(origin, None)
        plugin._voices.pop(origin, None)
        plugin._sendmodes.pop(origin, None)
        plugin._save_sessions()
        plugin._save_voices()
        plugin._save_sendmodes()
        return json_response({"ok": True})

    return handler


# ---------- 清空全部会话语音状态 ----------
def _clear_sessions(plugin):
    async def handler():
        plugin._sessions.clear()
        plugin._voices.clear()
        plugin._sendmodes.clear()
        plugin._save_sessions()
        plugin._save_voices()
        plugin._save_sendmodes()
        return json_response({"ok": True})

    return handler


# 试听预览保留数量：超出后按修改时间淘汰最旧的，避免 data/previews/ 无限增长
_PREVIEW_KEEP = 20


def _cleanup_previews(dirpath: str, keep: int = _PREVIEW_KEEP) -> None:
    """按修改时间淘汰多余的试听预览 wav，仅保留最近 keep 个。"""
    try:
        files = [
            os.path.join(dirpath, f)
            for f in os.listdir(dirpath)
            if f.lower().endswith(".wav")
        ]
        if len(files) <= keep:
            return
        files.sort(key=lambda p: os.path.getmtime(p))
        for p in files[:-keep]:
            try:
                os.remove(p)
            except Exception:  # noqa: BLE001
                pass
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[cosyvoice] 清理试听预览失败: {e}")


# ---------- 头像（会话列表用） ----------
def _list_avatars(plugin):
    """批量取头像，返回 ``{id: data URI}``（前端直接塞 ``<img src>``）。

    参数：``?type=user|group&ids=1,2,3&force=0&days=30``。群头像按群号取、私聊头像按
    QQ 号取；抓不到的 id 不会出现在结果里（前端自然退化为首字母色块）。
    """
    async def handler():
        cache = getattr(plugin, "avatars", None)
        if cache is None:
            return json_response({"items": {}, "error": "头像缓存未初始化"})
        kind = str(request.query.get("type") or "user").strip().lower()
        if kind not in ("user", "group"):
            return error_response("type 只能是 user / group", status_code=400)
        raw = str(request.query.get("ids") or "")
        ids = [s.strip() for s in raw.split(",") if s.strip()][:200]
        force = str(request.query.get("force") or "").strip().lower() in ("1", "true", "yes", "on")
        if not ids:
            return json_response({"items": {}, "requested": 0, "got": 0, "stats": cache.stats()})
        try:
            days = int(request.query.get("days") or 0)
        except (TypeError, ValueError):
            days = 0
        try:
            items = await cache.ensure_map(
                kind, ids, force=force, max_age_days=(days if days > 0 else None)
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[cosyvoice] 取头像失败（忽略）: {e}")
            items = {}
        return json_response({
            "items": items,
            "requested": len(ids),
            "got": len(items),
            "stats": cache.stats(),
        })

    return handler


# ---------- 合成试听 ----------
# inline 模式返回 data URI 的体积上限（base64 后），超出提示缩短文本。
# 24kHz/16bit/单声道 wav ≈ 48KB/s，10MB ≈ 3.5 分钟语音，试听场景足够宽裕。
_INLINE_MAX_BYTES = 10 * 1024 * 1024


def _synthesize(plugin):
    async def handler():
        # 支持 GET(query) 与 POST(json)；inline=1 时返回 JSON data URI（页面内直接
        # <audio> 播放）——真实 Dashboard 的 bridge.download 只触发「下载到磁盘」、
        # 拿不到 blob，页面内播放必须走这条 JSON 通道（v2.4.0 修）。
        text = str(request.query.get("text") or "").strip()
        voice = str(request.query.get("voice") or "").strip() or None
        inline = str(request.query.get("inline") or "").strip().lower()
        if not text or not inline:
            try:
                payload = await request.json(default={})
            except Exception:  # noqa: BLE001
                payload = {}
            if not text:
                text = str(payload.get("text") or "").strip()
            if not voice:
                voice = str(payload.get("voice") or "").strip() or None
            if not inline:
                inline = str(payload.get("inline") or "").strip().lower()
        if not text:
            return error_response("缺少试听文本", status_code=400)

        voice_name, prompt_wav, prompt_text = plugin.engine.resolve_voice(voice)
        if voice_name is None:
            return error_response("未配置任何可用音色", status_code=400)

        # 合成：走插件同一套合成管线（并发信号量 + 熔断 + 分片）。
        from astrbot.api.web import file_response

        import os as _os

        try:
            wav_path = await plugin.engine.synthesize(text, voice)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[cosyvoice] WebUI 试听合成失败: {e}")
            return error_response(f"试听合成失败：{e}", status_code=500)
        if not wav_path:
            reason = getattr(plugin.engine, "last_failure", "") or "未知原因（详见插件运行日志）"
            return error_response(f"试听合成失败（无有效音频）：{reason}", status_code=500)

        # 复制到插件数据目录 data/previews/ 留存（固定文件名：<音色名>.wav）
        previews_dir = _os.path.join(plugin._data_dir(), "previews")
        _os.makedirs(previews_dir, exist_ok=True)
        import shutil

        safe_name = "".join(ch for ch in voice_name if ch.isalnum() or ch in "-_.") or "preview"
        target = _os.path.join(previews_dir, f"{safe_name}.wav")
        try:
            shutil.copyfile(wav_path, target)
            # 淘汰多余的历史预览，避免 data/previews/ 随试听次数无限增长
            _cleanup_previews(previews_dir)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[cosyvoice] 试听音频复制到预览目录失败: {e}")
            target = wav_path

        if inline in ("1", "true", "yes", "on"):
            import base64

            try:
                size = _os.path.getsize(target)
                if size > _INLINE_MAX_BYTES:
                    return error_response(
                        f"试听音频过大（{size / 1024 / 1024:.1f}MB），请缩短文本后重试",
                        status_code=400,
                    )
                with open(target, "rb") as f:
                    b64 = base64.b64encode(f.read()).decode("ascii")
                return json_response({
                    "ok": True,
                    "mime": "audio/wav",
                    "data_uri": f"data:audio/wav;base64,{b64}",
                })
            except OSError as e:
                return error_response(f"读取试听音频失败：{e}", status_code=500)

        return file_response(target, filename=f"cosyvoice_{safe_name}.wav", content_type="audio/wav")

    return handler


# ---------- 翻译配置 ----------
def _translate_config(plugin):
    async def handler():
        if request.method == "POST":
            payload = await request.json(default={})
            plugin._save_translate_cfg(payload)
            return json_response({"ok": True})
        # GET：返回当前翻译配置（首次为空时返回默认骨架，便于前端填写）
        cfg = plugin._load_translate_cfg()
        if not cfg:
            cfg = {
                "enabled": False,
                "target": "zh",
                "source": [],
                "api": {
                    "url": "",
                    "method": "POST",
                    "apikey": "",
                    "auth_header": "Authorization",
                    "auth_scheme": "Bearer",
                    "content_type": "json",
                    "extra_headers": [],
                    "params": [],
                    "response_path": "",
                    "timeout": 15,
                },
            }
        return json_response(cfg)

    return handler


def _translate_test(plugin):
    async def handler():
        payload = await request.json(default={})
        sample = str(payload.get("sample") or "").strip()
        if not sample:
            return error_response("请提供测试文本 sample", status_code=400)
        try:
            result = await plugin.translator.test(sample)
        except Exception as e:  # noqa: BLE001
            return error_response(f"翻译测试异常：{e}", status_code=500)
        return json_response(result)

    return handler