"""语音投递：走 AstrBot 文件服务 URL，绕开 OneBot 适配器的 base64 化。

【为什么需要这个】
AstrBot 的 aiocqhttp 适配器对 Image/Record 一律调用 ``convert_to_base64()``：

    if isinstance(segment, Image | Record):
        bs64 = await segment.convert_to_base64()
        return {"type": ..., "data": {"file": f"base64://{bs64}"}}

而 ``Record.convert_to_base64()`` 内部是
``MediaResolver(..., media_type="audio").to_base64(target_format="wav")``，
**会强制转成 wav 再编码**。于是：
  - 传 URL 也没用（会被下载后转 wav 再 base64）；
  - 传 mp3 也没用（会被转回 wav）。
体积只由「时长」决定（24k/16bit/单声道 wav ≈ 2.88MB/分钟），
长语音经 base64 膨胀 33% 后被 NapCat 判为「文件太大」。

【怎么做】
把音频（优先 mp3，体积约 1/8）注册到 AstrBot 自带的令牌文件服务，得到
``{callback_api_base}/api/file/<token>``。该路由位于 dashboard 鉴权白名单
（``allowed_endpoint_prefixes`` 含 ``/api/file``），**免登录**、token 保护、
带超时，协议端可直接拉取，全程不经过 base64。

【为什么用 patch】
插件有 20+ 处发送点（主动推送 ``_realtime_send`` + 结果链 ``yield``），逐一改造
既容易漏、又会侵入业务逻辑。这里只在适配器入口替换一次：命中 Record 时产出
URL；任何异常都回退框架原逻辑，保证最差情况等同改动前。

【启用条件】缺一不可，否则完全不影响原有行为：
  1) 插件配置 ``send_via_file_service = true``
  2) AstrBot 全局配置 ``callback_api_base`` 非空，且协议端（NapCat）能访问该地址
"""

from __future__ import annotations

from typing import Any, Callable

from astrbot.api import logger

from .audio import cleanup_file, schedule_cleanup, wav_to_mp3

_installed = False
_original_func: Callable | None = None
_original_cls: Any = None
_cfg_getter: Callable[[], dict] | None = None


def _global_config_value(key: str) -> str:
    """读 AstrBot 全局配置项（与框架内部 astrbot_config 用法保持一致）。"""
    try:
        from astrbot.core import astrbot_config

        return str(astrbot_config.get(key, "") or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def is_available() -> tuple[bool, str]:
    """返回 (是否可用, 原因)。用于启动时给出明确日志，避免「开了配置却没生效」还查不出原因。"""
    base = _global_config_value("callback_api_base")
    if not base:
        return False, "AstrBot 全局配置 callback_api_base 为空（外部服务无法访问 AstrBot 的文件）"
    try:
        from astrbot.core import file_token_service  # noqa: F401
    except Exception as e:  # noqa: BLE001
        return False, f"无法导入框架文件服务：{e}"
    try:
        from astrbot.core.platform.sources.aiocqhttp import aiocqhttp_message_event  # noqa: F401
    except Exception as e:  # noqa: BLE001
        return False, f"无法导入 aiocqhttp 适配器：{e}"
    return True, "ok"


async def _record_url(segment: Any, cfg: dict) -> str | None:
    """把 Record 指向的本地音频注册到文件服务，返回可对外拉取的 URL。

    返回 None 表示「不该/不能走 URL」，调用方回退框架原逻辑（base64）。
    """
    base = _global_config_value("callback_api_base").rstrip("/")
    if not base:
        return None

    src = str(getattr(segment, "file", "") or getattr(segment, "url", "") or "").strip()
    if not src:
        return None
    # 已经是 URL / base64 的，交给框架原逻辑处理（避免我们再下载一次）
    if "://" in src:
        return None

    import os

    if not os.path.exists(src):
        return None

    path = src
    converted: str | None = None
    # 默认不转码：走 URL 后若仍报「文件太大」，才需要用它把文件体积降下来
    if bool(cfg.get("file_service_transcode_mp3", False)):
        try:
            bitrate = int(cfg.get("file_service_mp3_bitrate", 64) or 64)
        except Exception:  # noqa: BLE001
            bitrate = 64
        converted = await wav_to_mp3(src, bitrate)
        if converted:
            path = converted

    try:
        ttl = int(cfg.get("file_service_token_ttl", 900) or 900)
    except Exception:  # noqa: BLE001
        ttl = 900

    try:
        from astrbot.core import file_token_service

        token = await file_token_service.register_file(path, timeout=ttl)
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[cosyvoice] 注册文件服务失败，回退 base64 发送: {e}")
        if converted:
            cleanup_file(converted)
        return None

    if not token:
        if converted:
            cleanup_file(converted)
        return None

    # mp3 临时文件：等 token 过期（协议端拉取窗口）之后再删，避免还没拉走就被清理
    if converted:
        schedule_cleanup(converted, delay=float(ttl) + 30.0)

    logger.info(
        f"[cosyvoice] 语音改走文件服务 URL（{'mp3' if converted else 'wav'}，"
        f"token 有效期 {ttl}s，不再经 base64）"
    )
    return f"{base}/api/file/{token}"


def install(cfg_getter: Callable[[], dict]) -> bool:
    """安装适配器 patch（幂等）。cfg_getter 为读取插件最新配置的函数。"""
    global _installed, _original_func, _original_cls, _cfg_getter

    if _installed:
        _cfg_getter = cfg_getter
        return True

    available, reason = is_available()
    if not available:
        logger.warning(f"[cosyvoice] 未启用「文件服务 URL 发送语音」：{reason}")
        return False

    try:
        from astrbot.api.message_components import Record
        from astrbot.core.platform.sources.aiocqhttp import (
            aiocqhttp_message_event as mod,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[cosyvoice] 未启用「文件服务 URL 发送语音」：导入失败 {e}")
        return False

    cls = mod.AiocqhttpMessageEvent
    raw = cls.__dict__.get("_from_segment_to_dict")
    if not isinstance(raw, staticmethod):
        # 框架结构变了：宁可不生效，也不要破坏发送
        logger.warning(
            "[cosyvoice] 未启用「文件服务 URL 发送语音」："
            "框架 _from_segment_to_dict 结构非预期（可能版本已变），保持原样发送"
        )
        return False
    original_func = raw.__func__

    async def patched(segment: Any) -> dict:
        cfg = _cfg_getter() if _cfg_getter else {}
        if isinstance(segment, Record) and bool(cfg.get("send_via_file_service", False)):
            try:
                url = await _record_url(segment, cfg)
                if url:
                    return {"type": "record", "data": {"file": url}}
            except Exception as e:  # noqa: BLE001
                logger.debug(f"[cosyvoice] Record→URL 失败，回退 base64: {e}")
        return await original_func(segment)

    cls._from_segment_to_dict = staticmethod(patched)
    _installed = True
    _original_func = original_func
    _original_cls = cls
    _cfg_getter = cfg_getter
    logger.info(
        "[cosyvoice] 已启用「文件服务 URL 发送语音」："
        "语音将先转 mp3（可配）再注册为 /api/file/<token>，不再经过 base64"
    )
    return True


def uninstall() -> None:
    """还原适配器原方法（配置关闭热切换时使用）。"""
    global _installed, _original_func, _original_cls, _cfg_getter
    if not _installed or _original_cls is None or _original_func is None:
        return
    try:
        _original_cls._from_segment_to_dict = staticmethod(_original_func)
        logger.info("[cosyvoice] 已还原框架 Record 发送逻辑（关闭文件服务 URL 发送）")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[cosyvoice] 还原框架 Record 发送逻辑失败: {e}")
    finally:
        _installed = False
        _original_func = None
        _original_cls = None
        _cfg_getter = None


def sync(enabled: bool, cfg_getter: Callable[[], dict]) -> None:
    """按配置开关状态同步 patch：开启则安装，关闭则还原。"""
    if enabled:
        install(cfg_getter)
    else:
        uninstall()
