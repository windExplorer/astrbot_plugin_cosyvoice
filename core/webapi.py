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

import asyncio
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
    ctx.register_web_api(f"/{p}/voices", _list_voices(plugin), ["GET"], "CosyVoice 音色列表")
    ctx.register_web_api(f"/{p}/voices/default", _set_default_voice(plugin), ["POST"], "设置默认音色")
    ctx.register_web_api(f"/{p}/voices/hidden", _set_voice_hidden(plugin), ["POST"], "隐藏/显示音色")
    ctx.register_web_api(f"/{p}/sessions", _list_sessions(plugin), ["GET"], "会话语音状态列表")
    ctx.register_web_api(f"/{p}/sessions/set", _set_session(plugin), ["POST"], "按会话设置语音开关/音色/发送方式")
    ctx.register_web_api(f"/{p}/sessions/batch_off", _batch_off(plugin), ["POST"], "批量关闭会话语音")
    ctx.register_web_api(f"/{p}/synthesize", _synthesize(plugin), ["POST"], "试听合成（返回 wav）")
    logger.info(f"[cosyvoice] WebUI API 已注册（前缀 /api/plug/{p}/）")


# ---------- 概览 ----------
def _overview(plugin):
    async def handler():
        plugin._refresh_cfg()
        cfg = plugin.config
        # 服务端健康：优先取 Router 的节点视图
        servers = []
        router = plugin.client
        try:
            node_info = None
            for attr in ("servers", "_servers", "_nodes"):
                if hasattr(router, attr):
                    node_info = getattr(router, attr)
                    break
            if isinstance(node_info, dict):
                for s in node_info.values():
                    if isinstance(s, dict):
                        servers.append({
                            "url": s.get("url", ""),
                            "enabled": bool(s.get("enabled", True)),
                            "default": bool(s.get("default", False)),
                            "weight": s.get("weight", 1),
                            "down_count": s.get("down_count", 0),
                            "status": "down" if s.get("down_count", 0) > 0 else "ok",
                        })
            elif isinstance(node_info, (list, tuple)):
                for s in node_info:
                    if isinstance(s, dict):
                        servers.append({
                            "url": s.get("url", ""),
                            "enabled": bool(s.get("enabled", True)),
                            "default": bool(s.get("default", False)),
                            "weight": s.get("weight", 1),
                        })
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[cosyvoice] 读取服务端节点状态失败: {e}")
        if not servers:
            servers = [{
                "url": cfg.get("base_url", ""),
                "enabled": True,
                "default": True,
                "status": "ok",
            }]

        # 熔断冷却
        cooldown_until = getattr(plugin, "_server_cooldown_until", 0.0)
        import time
        remaining = max(0.0, cooldown_until - time.time()) if cooldown_until else 0.0

        return json_response({
            "servers": servers,
            "cooldown_remaining": round(remaining, 1),
            "server_down": bool(getattr(plugin, "_server_down", False)),
            "config": {
                "auto_tts": bool(cfg.get("auto_tts", False)),
                "send_mode": cfg.get("send_mode", "both"),
                "tts_scope": cfg.get("tts_scope", "llm_only"),
                "enable_llm_tool": bool(cfg.get("enable_llm_tool", True)),
                "default_voice": plugin._effective_default_voice(),
                "sample_rate": int(cfg.get("sample_rate", 24000)),
                "base_url": cfg.get("base_url", ""),
                "server_voices_dir": cfg.get("server_voices_dir", "") or "",
            },
            "voice_count": len(plugin.engine.voices),
            "session_count": len(plugin._sessions),
        })

    return handler


# ---------- 音色列表 ----------
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
                "hidden": bool(v.get("hidden", False)),
                "is_default": name == default,
                # 本地能否解析到参考音频（排查用）
                "wav_resolved": bool(plugin.engine.resolve_wav(v.get("prompt_wav", ""))),
            })
        return json_response({"voices": result, "default_voice": default})

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
        all_voices = plugin.engine.list_voices(include_hidden=True)
        if name not in all_voices:
            return error_response(f"没有「{name}」这个音色", status_code=400)
        # 音色隐藏标记来自配置 template_list，此处不写配置（避免破坏 schema）；
        # 通过 engine 内存热切换，下次配置刷新后恢复。若要持久化请改配置页。
        v = plugin.engine.voices.get(name, {})
        v["hidden"] = hidden
        logger.info(f"[cosyvoice] WebUI 切换音色「{name}」hidden={hidden}（内存态，见说明）")
        return json_response({"ok": True})

    return handler


# ---------- 会话列表 ----------
def _list_sessions(plugin):
    async def handler():
        plugin._refresh_cfg()
        # 汇总会话级状态：开关 / 音色 / 发送方式
        keys = set(plugin._sessions) | set(plugin._voices) | set(plugin._sendmodes)
        sessions = []
        for origin in sorted(keys):
            prob = plugin._sessions.get(origin)
            on = prob is not None and prob is not False
            sessions.append({
                "origin": origin,
                "on": on,
                "prob": prob if isinstance(prob, (int, float)) else (1.0 if on else None),
                "voice": plugin._voices.get(origin, ""),
                "send_mode": plugin._sendmodes.get(origin) if plugin._sendmodes.get(origin) in ("both", "voice_only") else None,
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


# ---------- 试听合成 ----------
def _synthesize(plugin):
    async def handler():
        payload = await request.json(default={})
        text = str(payload.get("text") or "").strip()
        voice = str(payload.get("voice") or "").strip() or None
        if not text:
            return error_response("缺少试听文本", status_code=400)

        voice_name, prompt_wav, prompt_text = plugin.engine.resolve_voice(voice)
        if voice_name is None:
            return error_response("未配置任何可用音色", status_code=400)

        # 优先走「服务端存音频返回直链」：需要服务端支持 /synthesize_save。
        # 若失败（旧服务端无此端点 / 服务端不可达），回退本地合成 + file_response。
        try:
            url = await asyncio.to_thread(
                _server_preview_url, plugin, voice_name, prompt_wav, prompt_text, text
            )
            if url:
                return json_response({"url": url})
            raise RuntimeError("服务端不支持 /synthesize_save")
        except Exception as e:  # noqa: BLE001
            logger.info(f"[cosyvoice] WebUI 试听走服务端直链失败，回退本地合成: {e}")

        try:
            wav_path = await plugin.engine.synthesize(text, voice)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[cosyvoice] WebUI 试听合成失败: {e}")
            return json_response({"url": None, "reason": f"试听合成失败：{e}"})
        if not wav_path:
            return json_response({"url": None, "reason": "试听合成失败（无有效音频，可能是纯符号/无音色）"})
        # 回退模式：本地合成后经 astrbot.api.web.send_file 返回 wav 二进制
        from astrbot.api.web import file_response

        return file_response(wav_path, filename="cosyvoice_preview.wav", content_type="audio/wav")

    return handler


def _server_preview_url(plugin, voice_name: str, prompt_wav: str, prompt_text: str, text: str) -> str | None:
    """调服务端 /synthesize_save 返回可直连的完整音频 URL；失败返回 None。

    合成的音频保存到 CosyVoice 服务端本地，返回相对 url（/audio/<name>.wav），
    这里拼上配置的 base_url（对外可达地址）供浏览器 <audio> 直连播放。
    """
    import httpx

    base_url = _pick_server_url(plugin)
    if not base_url:
        return None

    data = {"tts_text": text}
    if prompt_text:
        data["prompt_text"] = prompt_text
    # 参考音频模式：
    # - 配置了 server_voices_dir（服务端存音频）→ 传文件名 prompt_wav_path；
    # - 否则本地上传模式 → 把本机 wav 以表单上传。
    server_dir_mode = bool((plugin.config.get("server_voices_dir") or "").strip())
    local_wav = ""
    files = None
    if prompt_wav:
        local_wav = plugin.engine.resolve_wav(prompt_wav)
        if server_dir_mode:
            data["prompt_wav_path"] = os.path.basename(prompt_wav.strip())
        elif os.path.exists(local_wav):
            with open(local_wav, "rb") as f:
                wav_bytes = f.read()
            files = {"prompt_wav": (os.path.basename(local_wav), wav_bytes, "audio/wav")}
        else:
            return None

    try:
        timeout = httpx.Timeout(20.0)
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(f"{base_url}/synthesize_save", data=data, files=files)
            resp.raise_for_status()
            payload = resp.json()
            rel = payload.get("url")
            if not rel:
                return None
            return f"{base_url}{rel}"
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[cosyvoice] 服务端 /synthesize_save 调用失败: {e}")
        return None


def _pick_server_url(plugin) -> str:
    """取一个对外可达的 CosyVoice 服务地址（默认节点 / 首个启用节点 / 配置 base_url）。"""
    router = plugin.client
    try:
        for attr in ("_servers", "servers", "_nodes"):
            nodes = getattr(router, attr, None)
            if isinstance(nodes, dict):
                for s in nodes.values():
                    if isinstance(s, dict) and s.get("enabled", True):
                        return str(s.get("url") or "").rstrip("/")
            elif isinstance(nodes, (list, tuple)):
                for s in nodes:
                    if isinstance(s, dict) and s.get("enabled", True):
                        return str(s.get("url") or "").rstrip("/")
    except Exception:
        pass
    return str(plugin.config.get("base_url", "") or "").rstrip("/")