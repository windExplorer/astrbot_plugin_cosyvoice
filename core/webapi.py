"""WebUI 后端 API：为 AstrBot 插件 Pages 提供 CosyVoice 管理接口。

路由由 main.py 在插件 initialize() 阶段通过 context.register_web_api() 注册，
统一前缀 ``/astrbot_plugin_cosyvoice/``（route 需带插件名；Page 端 bridge
endpoint 不带前缀）。

约定：
- 所有 handler 使用 astrbot.api.web 的 request / json_response / error_response；
- 会话（unified_msg_origin）维度的开关/音色/发送方式直接复用插件内存态
  （plugin._sessions / _voices / _sendmodes），写操作调用插件的 _save_*
  持久化到 data/ 目录（与聊天指令 /tts_on 等同源，热生效、重启不丢）；
- 音色（voices）由配置 _conf_schema.json 的 template_list 提供，WebUI 只读 +
  快捷操作（设默认/隐藏），不在此写入配置（配置热更由 Dashboard 配置页负责）。
"""

from __future__ import annotations

import json

from astrbot.api import logger
from astrbot.api.web import error_response, json_response, request

# 与 main.py 的 PLUGIN_ID 保持一致；route 前缀必须带插件名
PLUGIN_ROUTE_PREFIX = "astrbot_plugin_cosyvoice"


def json_dumps(value) -> str:
    """安全地转 JSON 字符串（供配置展示用）。"""
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def register_web_apis(plugin) -> None:
    """把 WebUI 后端 API 注册到插件 Context。

    :param plugin: CosyVoicePlugin 实例（提供 engine/_sessions/_voices/…）。
    """
    ctx = plugin.context
    p = PLUGIN_ROUTE_PREFIX

    ctx.register_web_api(f"/{p}/overview", _overview(plugin), ["GET"], "CosyVoice 概览（服务端健康/全局开关）")
    ctx.register_web_api(f"/{p}/config", _get_config(plugin), ["GET"], "CosyVoice 配置（分组只读）")
    ctx.register_web_api(f"/{p}/voices", _list_voices(plugin), ["GET"], "CosyVoice 音色列表")
    ctx.register_web_api(f"/{p}/voices/create", _create_voice(plugin), ["POST"], "新增音色")
    ctx.register_web_api(f"/{p}/voices/update", _update_voice(plugin), ["POST"], "编辑音色")
    ctx.register_web_api(f"/{p}/voices/delete", _delete_voice(plugin), ["POST"], "删除音色")
    ctx.register_web_api(f"/{p}/voices/default", _set_default_voice(plugin), ["POST"], "设置默认音色")
    ctx.register_web_api(f"/{p}/voices/hidden", _set_voice_hidden(plugin), ["POST"], "隐藏/显示音色")
    ctx.register_web_api(f"/{p}/sessions", _list_sessions(plugin), ["GET"], "会话语音状态列表")
    ctx.register_web_api(f"/{p}/sessions/set", _set_session(plugin), ["POST"], "按会话设置语音开关/音色/发送方式")
    ctx.register_web_api(f"/{p}/sessions/delete", _delete_session(plugin), ["POST"], "删除会话语音状态")
    ctx.register_web_api(f"/{p}/sessions/clear", _clear_sessions(plugin), ["POST"], "清空全部会话语音状态")
    ctx.register_web_api(f"/{p}/sessions/batch_off", _batch_off(plugin), ["POST"], "批量关闭会话语音")
    ctx.register_web_api(f"/{p}/synthesize", _synthesize(plugin), ["GET", "POST"], "合成试听（返回 wav 下载）")
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


# ---------- 配置（分组只读，供「配置」tab 展示） ----------
def _get_config(plugin):
    async def handler():
        plugin._refresh_cfg()
        cfg = plugin.config
        groups = {
            "服务端": {
                "服务地址 (base_url)": cfg.get("base_url", ""),
                "服务端列表 (servers)": json_dumps(cfg.get("servers") or []),
                "服务端音频目录 (server_voices_dir)": cfg.get("server_voices_dir", "") or "",
                "采样率 (sample_rate)": cfg.get("sample_rate", 24000),
                "请求超时/秒 (timeout)": cfg.get("timeout", 150),
                "排队阈值 (tts_queue_max_position)": cfg.get("tts_queue_max_position", 8),
            },
            "语音行为": {
                "自动语音 (auto_tts)": bool(cfg.get("auto_tts", False)),
                "发送方式 (send_mode)": cfg.get("send_mode", "both"),
                "语音范围 (tts_scope)": cfg.get("tts_scope", "llm_only"),
                "默认音色 (default_voice)": plugin._effective_default_voice(),
                "LLM 工具 (enable_llm_tool)": bool(cfg.get("enable_llm_tool", True)),
                "关键词触发 (enable_user_trigger)": bool(cfg.get("enable_user_trigger", True)),
                "触发关键词 (trigger_keywords)": cfg.get("trigger_keywords", []),
                "纯文字关键词 (text_keywords)": cfg.get("text_keywords", []),
                "黑名单 (blocklist)": cfg.get("blocklist", []),
                "白名单 (allowlist)": cfg.get("allowlist", []),
                "并发上限 (tts_concurrency)": cfg.get("tts_concurrency", 1),
            },
            "分段与重试": {
                "分段字数 (segment_len)": cfg.get("segment_len", 0),
                "首段字数 (segment_first_len)": cfg.get("segment_first_len", 0),
                "分段符号 (segment_punct)": cfg.get("segment_punct", ""),
                "单段硬上限 (max_text_len)": cfg.get("max_text_len", 200),
                "按换行分段 (split_by_newline)": bool(cfg.get("split_by_newline", True)),
                "合并单条发送 (segment_merge)": bool(cfg.get("segment_merge", False)),
                "失败重试 (tts_max_retry)": cfg.get("tts_max_retry", 0),
                "重试退避 (tts_retry_backoff)": cfg.get("tts_retry_backoff", 0.5),
                "冷却时长/秒 (tts_cooldown_sec)": cfg.get("tts_cooldown_sec", 30),
            },
        }
        return json_response({"groups": groups})

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
        if not name:
            return error_response("音色名不能为空", status_code=400)
        if name in plugin._effective_voices():
            return error_response(f"音色「{name}」已存在", status_code=400)
        plugin._voices_lib[name] = {
            "prompt_wav": prompt_wav,
            "prompt_text": prompt_text,
            "language": language,
            "hidden": hidden,
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
        }))
        if "prompt_wav" in payload:
            entry["prompt_wav"] = str(payload["prompt_wav"] or "").strip()
        if "prompt_text" in payload:
            entry["prompt_text"] = str(payload["prompt_text"] or "").strip()
        if "hidden" in payload:
            entry["hidden"] = bool(payload["hidden"])
        if "language" in payload:
            entry["language"] = str(payload["language"] or "").strip().lower()
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


# ---------- 隐藏/显示音色 ----------
def _set_voice_hidden(plugin):
    async def handler():
        payload = await request.json(default={})
        name = str(payload.get("name") or "").strip()
        hidden = bool(payload.get("hidden", False))
        all_voices = plugin._effective_voices()
        if name not in all_voices:
            return error_response(f"没有「{name}」这个音色", status_code=400)
        # 仅 WebUI 音色库中的音色可持久改 hidden；配置音色由配置页管理（此处仅热生效）
        if name in plugin._voices_lib:
            plugin._voices_lib[name]["hidden"] = hidden
            plugin._save_voices_lib()
        else:
            plugin.engine.voices[name]["hidden"] = hidden
        plugin._refresh_cfg()
        logger.info(f"[cosyvoice] WebUI 切换音色「{name}」hidden={hidden}")
        return json_response({"ok": True})

    return handler


def _fmt_origin(origin: str) -> str:
    """把 unified_msg_origin 解析成易读标签（best-effort）。"""
    parts = str(origin).split(":")
    if len(parts) >= 3:
        return f"{parts[0]} · 群 {parts[1]} · 用户 {parts[2]}"
    if len(parts) == 2:
        return f"{parts[0]} · {parts[1]}"
    return str(origin)


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
            sessions.append({
                "id": origin,
                "user": _fmt_origin(origin),
                "on": on,
                "mode": mode,
                "voice": plugin._voices.get(origin, "") or "默认",
                "prob": prob,
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

        on = payload.get("on")
        if on is not None:
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


# ---------- 批量关闭会话语音 ----------
def _batch_off(plugin):
    async def handler():
        payload = await request.json(default={})
        origins = payload.get("origins")
        if origins is None:
            return error_response("缺少 origins 列表", status_code=400)
        for origin in origins:
            plugin._sessions.pop(str(origin), None)
        plugin._save_sessions()
        return json_response({"ok": True, "closed": len(origins)})

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


# ---------- 合成试听 ----------
def _synthesize(plugin):
    async def handler():
        # 支持 GET(query) 与 POST(json)：bridge.download 走 GET。
        text = str(request.query.get("text") or "").strip()
        voice = str(request.query.get("voice") or "").strip() or None
        if not text:
            payload = await request.json(default={})
            text = str(payload.get("text") or "").strip()
            voice = str(payload.get("voice") or "").strip() or None
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
            return error_response("试听合成失败（无有效音频，可能是纯符号/无音色）", status_code=500)

        # 复制到插件数据目录 data/previews/ 留存（固定文件名：<音色名>.wav）
        previews_dir = _os.path.join(plugin._data_dir(), "previews")
        _os.makedirs(previews_dir, exist_ok=True)
        import shutil

        safe_name = "".join(ch for ch in voice_name if ch.isalnum() or ch in "-_.") or "preview"
        target = _os.path.join(previews_dir, f"{safe_name}.wav")
        try:
            shutil.copyfile(wav_path, target)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[cosyvoice] 试听音频复制到预览目录失败: {e}")
            target = wav_path

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