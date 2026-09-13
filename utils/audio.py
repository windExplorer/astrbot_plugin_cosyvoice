"""音频格式工具：将 CosyVoice 返回的裸 int16 PCM 字节流封装为 WAV。

官方 QwenAudio/CosyVoice 的 fastapi/server.py 直接返回裸 int16 PCM 字节流（无 WAV 头），
AstrBot 的 Record 组件只接受 wav，因此需要补 WAV 头。
"""

import asyncio
import io
import os
import tempfile
import wave

from astrbot.api import logger


def _tmp_path(suffix: str, cache_dir: str) -> str:
    if cache_dir:
        os.makedirs(cache_dir, exist_ok=True)
        return tempfile.mktemp(suffix=suffix, dir=cache_dir)
    return tempfile.mktemp(suffix=suffix)


def pcm_to_wav_bytes(pcm: bytes, sample_rate: int = 24000) -> bytes:
    """把裸 int16 PCM 字节流封装成标准 WAV 文件的字节内容（内存中完成）。"""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)  # int16 -> 2 bytes
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()


def pcm_to_wav_file(pcm: bytes, sample_rate: int = 24000, cache_dir: str = "") -> str:
    """把裸 int16 PCM 字节流写成一个临时 wav 文件，返回文件路径（Record 可直接引用）。"""
    path = _tmp_path(".wav", cache_dir)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return path


def write_bytes_file(path: str, data: bytes) -> None:
    """将字节写入文件（合成结果落盘用，同步写小文件）。"""
    with open(path, "wb") as f:
        f.write(data)


def cleanup_file(path: str) -> None:
    """安全删除临时文件。"""
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[cosyvoice] 清理临时文件失败: {path} -> {e}")


_FFMPEG_AVAILABLE: bool | None = None


def has_ffmpeg() -> bool:
    """系统是否有可用 ffmpeg（结果缓存，避免每次发送都探测）。"""
    global _FFMPEG_AVAILABLE
    if _FFMPEG_AVAILABLE is None:
        try:
            import shutil

            _FFMPEG_AVAILABLE = shutil.which("ffmpeg") is not None
        except Exception:  # noqa: BLE001
            _FFMPEG_AVAILABLE = False
    return _FFMPEG_AVAILABLE


async def wav_to_mp3(wav_path: str, bitrate: int = 64, cache_dir: str = "") -> str | None:
    """把 wav 转成 mp3（体积约为 wav 的 1/8~1/16），失败返回 None 由调用方回退 wav。

    24kHz/16bit/单声道 wav ≈ 2.88MB/分钟，而语音用 32~64kbps mp3 已足够清晰
    （QQ/OneBot 语音本身支持 mp3），这是减小语音体积最直接的手段。
    """
    if not wav_path or not os.path.exists(wav_path) or not has_ffmpeg():
        return None
    out = _tmp_path(".mp3", cache_dir)
    rate = max(16, min(320, int(bitrate or 64)))
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        wav_path,
        "-codec:a",
        "libmp3lame",
        "-b:a",
        f"{rate}k",
        "-ar",
        "24000",
        "-ac",
        "1",
        out,
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, err = await proc.communicate()
        if proc.returncode != 0 or not os.path.exists(out) or os.path.getsize(out) == 0:
            detail = (err or b"").decode("utf-8", errors="ignore")[:200]
            logger.debug(f"[cosyvoice] wav→mp3 失败（回退 wav）: {detail}")
            cleanup_file(out)
            return None
        return out
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[cosyvoice] wav→mp3 异常（回退 wav）: {e}")
        cleanup_file(out)
        return None


def schedule_cleanup(path: str, delay: float = 60.0) -> None:
    """延迟删除临时文件。

    Record 组件在发送时会读取该 wav 文件，发送完成后即可安全删除，
    因此用事件循环延迟一段时间再清理，避免临时文件在系统目录无限堆积。
    """
    try:
        loop = asyncio.get_running_loop()
        loop.call_later(delay, cleanup_file, path)
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[cosyvoice] 调度清理失败: {path} -> {e}")
