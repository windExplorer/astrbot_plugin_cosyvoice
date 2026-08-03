#!/usr/bin/env python3
"""CosyVoice TTS 推理 API —— 队列版（兼容 CosyVoice1/2/3）。

与 cosyvoice_api.py 功能完全一致、接口保持一致，额外在**服务端**内置一个
有界队列，专门应对「即时通信机器人 + 多人同时请求」这类高并发冲击 GPU 串行
推理的场景：

    - 所有推理请求进入一个 maxsize 有限的有界队列（默认 8）；
    - 单个 worker 协程串行消费队列，保证同一时刻只跑一个推理任务，
      避免 CUDA 上下文竞争 / 把 event loop 卡死；
    - 队满或入队等待超时（默认 30s）直接返回 429，让调用方退避重试，
      而不是让请求无限堆积拖垮服务；
    - 单次推理超时（默认 120s）返回 504；模型未加载返回 503；
    - 新增 GET /queue 实时查看队列长度、排队中、worker 状态。

机器人 client 只需把端口从 50000 换成 50001（默认）即可，无需改调用代码。

运行：
    uv run python cosyvoice_api_queue.py --model_dir pretrained_models/Fun-CosyVoice3-0.5B-2512

环境变量（可选）：
    COSY_QUEUE_SIZE      队列长度上限（默认 8）
    COSY_QUEUE_WAIT     入队最长等待秒数（默认 30）
    COSY_INFER_TIMEOUT  单次推理超时秒数（默认 120）
"""

import argparse
import asyncio
import json
import os
import sys
import tempfile
import time
from typing import Optional

# Windows 控制台默认 GBK，无法输出 emoji 等字符，强制 stdout/stderr 使用 UTF-8
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# 将 torch 自带的 CUDA 运行库目录前置到 PATH，避免系统已装的其它 CUDA
# 版本（如 v13.x）的 DLL 抢先加载，导致 c10.dll 初始化失败 (WinError 1114)
try:
    import torch  # noqa: F401
    _torch_lib = os.path.join(os.path.dirname(torch.__file__), "lib")
    if os.path.isdir(_torch_lib) and _torch_lib not in os.environ["PATH"]:
        os.environ["PATH"] = _torch_lib + os.pathsep + os.environ["PATH"]
except Exception:
    pass

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT_DIR, "third_party", "Matcha-TTS"))

import numpy as np
import uvicorn
from contextlib import asynccontextmanager
from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import Response

from cosyvoice.cli.cosyvoice import AutoModel
from cosyvoice.utils.file_utils import logging


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _infer_queue, _worker_task
    _infer_queue = asyncio.Queue(maxsize=QUEUE_SIZE)
    _worker_task = asyncio.create_task(_queue_worker())
    logging.info(f"推理队列已就绪: maxsize={QUEUE_SIZE}, wait_timeout={QUEUE_WAIT_TIMEOUT}s, infer_timeout={INFER_TIMEOUT}s")
    try:
        yield
    finally:
        if _worker_task is not None:
            _worker_task.cancel()
            try:
                await _worker_task
            except asyncio.CancelledError:
                pass
        logging.info("推理队列 worker 已停止")


app = FastAPI(title="CosyVoice TTS API (queue)", lifespan=lifespan)

cosyvoice = None
SAMPLE_RATE = 24000
VOICES_DIR = os.path.join(ROOT_DIR, "cosyvoice_voices")
VOICES_JSON = os.path.join(VOICES_DIR, "voices.json")

# ── 队列相关配置（可被环境变量覆盖） ──
QUEUE_SIZE = int(os.environ.get("COSY_QUEUE_SIZE", "8"))
QUEUE_WAIT_TIMEOUT = float(os.environ.get("COSY_QUEUE_WAIT", "30"))
INFER_TIMEOUT = float(os.environ.get("COSY_INFER_TIMEOUT", "120"))

# 有界队列：所有推理请求入队；单 worker 串行消费
_infer_queue: Optional[asyncio.Queue] = None
_worker_task: Optional[asyncio.Task] = None
# worker 状态：'idle' 空闲 / 'busy' 推理中
_worker_state = {"state": "idle", "started_at": None, "current": None}


def _load_model(model_dir: str):
    global cosyvoice, SAMPLE_RATE
    cosyvoice = AutoModel(model_dir=model_dir)
    SAMPLE_RATE = cosyvoice.sample_rate
    methods = [
        m
        for m in (
            "inference_zero_shot",
            "inference_instruct2",
            "inference_instruct",
            "inference_sft",
            "inference_cross_lingual",
        )
        if hasattr(cosyvoice, m)
    ]
    logging.info(f"模型已加载: {model_dir}, sample_rate={SAMPLE_RATE}, 支持方法: {methods}")


def _synthesize(generator):
    """收集生成器输出的 tts_speech，拼接为 int16 PCM 字节。"""
    chunks = []
    for item in generator:
        speech = item.get("tts_speech")
        if speech is None:
            continue
        audio = speech.squeeze().cpu().numpy().astype(np.float32)
        audio = np.clip(audio, -1.0, 1.0)
        chunks.append((audio * 32767.0).astype(np.int16))
    if not chunks:
        return b""
    return np.concatenate(chunks).tobytes()


def _save_upload_wav(upload: UploadFile) -> str:
    """保存上传的 wav 到临时文件，返回路径（不调用 load_wav，模型内部会自己加载）。"""
    suffix = os.path.splitext(upload.filename or "prompt.wav")[1] or ".wav"
    fd, path = tempfile.mkstemp(suffix=suffix)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(upload.file.read())
    except Exception:
        # 写入失败则清理已创建的临时文件，避免堆积
        try:
            os.close(fd)
        except Exception:
            pass
        try:
            os.remove(path)
        except Exception:
            pass
        raise
    return path


def _safe_remove(path: str):
    """尽力删除临时文件，忽略任何异常。"""
    if not path:
        return
    try:
        os.remove(path)
    except Exception:
        pass


def _load_voices_map() -> dict:
    """读取 voices.json（文件名 -> 参考文本）。文件不存在或损坏时返回空字典。"""
    if not os.path.exists(VOICES_JSON):
        return {}
    try:
        with open(VOICES_JSON, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception as e:  # noqa: BLE001
        logging.warning(f"读取 voices.json 失败: {e}")
    return {}


def _resolve_prompt_wav_path(upload: Optional[UploadFile], server_path: str) -> str:
    """解析参考音频路径（返回文件路径字符串，不是 tensor）。

    inference_zero_shot / inference_instruct2 内部会自己调 load_wav，
    如果预加载成 tensor 再传入会报错: TypeError: Invalid file: tensor(...)
    """
    if server_path:
        if os.path.isabs(server_path) and os.path.exists(server_path):
            return server_path
        cand = os.path.join(VOICES_DIR, server_path)
        if not os.path.exists(cand):
            raise FileNotFoundError(f"服务端参考音频不存在: {server_path}（在 {VOICES_DIR} 中未找到）")
        return cand
    if upload is not None and upload.filename:
        return _save_upload_wav(upload)
    raise ValueError("需提供 prompt_wav 文件上传或 prompt_wav_path 服务端路径")


def _resolve_prompt_text(prompt_text: str, server_path: str) -> str:
    """参考文本：客户端传了用客户端的，否则用 voices.json 中按文件名查到的。"""
    text = (prompt_text or "").strip()
    if text:
        return text
    if server_path:
        name = os.path.basename(server_path)
        mapped = _load_voices_map().get(name)
        if mapped:
            return mapped
    return ""


async def _queue_worker():
    """常驻单 worker 协程：串行消费队列，保证 GPU 推理同一时刻只有一个任务。"""
    assert _infer_queue is not None
    while True:
        item = await _infer_queue.get()
        try:
            gen = item["gen"]
            future = item["future"]
            label = item.get("label", "infer")
            _worker_state["state"] = "busy"
            _worker_state["started_at"] = time.time()
            _worker_state["current"] = label
            try:
                pcm = await asyncio.wait_for(
                    asyncio.to_thread(_synthesize, gen),
                    timeout=INFER_TIMEOUT,
                )
                if not future.cancelled():
                    future.set_result((pcm, 200, ""))
            except asyncio.TimeoutError:
                if not future.cancelled():
                    future.set_result(("", 504, "inference timeout"))
            except Exception as e:  # noqa: BLE001
                if not future.cancelled():
                    future.set_result(("", 500, f"inference error: {e}"))
            finally:
                _worker_state["state"] = "idle"
                _worker_state["started_at"] = None
                _worker_state["current"] = None
        finally:
            _infer_queue.task_done()


async def _enqueue(gen, label: str):
    """将推理任务入队，返回 (pcm, status_code, message)。

    队满或等待超时返回 429，让调用方退避重试，而不是无限堆积。
    """
    assert _infer_queue is not None
    loop = asyncio.get_event_loop()
    future = loop.create_future()
    item = {"gen": gen, "future": future, "label": label}
    try:
        # 若队列满，最多等 QUEUE_WAIT_TIMEOUT 秒，避免请求永久挂起
        await asyncio.wait_for(_infer_queue.put(item), timeout=QUEUE_WAIT_TIMEOUT)
    except asyncio.TimeoutError:
        return "", 429, "service busy, queue full"
    pcm, code, msg = await future
    return pcm, code, msg


@app.get("/")
def health():
    return {"status": "ok", "model_loaded": cosyvoice is not None, "sample_rate": SAMPLE_RATE}


@app.get("/queue")
def queue_status():
    """实时查看队列与 worker 状态，便于监控/调试。"""
    if _infer_queue is None:
        return {"queue_size": None, "waiting": None, "worker": "not_started"}
    state = dict(_worker_state)
    if state.get("started_at") is not None:
        state["running_seconds"] = round(time.time() - state["started_at"], 1)
    return {
        "queue_maxsize": QUEUE_SIZE,
        "in_queue": _infer_queue.qsize(),
        "worker": state,
    }


@app.get("/voices")
def list_voices():
    if not os.path.isdir(VOICES_DIR):
        return {"voices_dir": VOICES_DIR, "files": []}
    files = sorted(
        f
        for f in os.listdir(VOICES_DIR)
        if os.path.isfile(os.path.join(VOICES_DIR, f)) and f.lower().endswith((".wav", ".mp3", ".flac"))
    )
    mapping = _load_voices_map()
    return {"voices_dir": VOICES_DIR, "files": files, "texts": {f: mapping.get(f, "") for f in files}}


@app.post("/inference_zero_shot")
async def inference_zero_shot(
    tts_text: str = Form(...),
    prompt_text: str = Form(""),
    prompt_wav: Optional[UploadFile] = File(None),
    prompt_wav_path: str = Form(""),
):
    if cosyvoice is None:
        return Response("model not loaded", status_code=503)
    # 先在主线程解析路径（含上传文件落盘），再进入队列跑同步推理
    try:
        prompt_wav_path = _resolve_prompt_wav_path(prompt_wav, prompt_wav_path)
        prompt_text = _resolve_prompt_text(prompt_text, prompt_wav_path)
        if not prompt_text:
            logging.warning(
                f"[zero_shot] 缺少参考文本: prompt_text为空且voices.json未配置, "
                f"prompt_wav_path={prompt_wav_path}"
            )
            return Response("缺少参考文本：请在请求中传 prompt_text，或在 voices.json 配置该文件的文本", status_code=400)
    except (FileNotFoundError, ValueError) as e:
        logging.warning(f"[zero_shot] 音频解析失败: {e}")
        return Response(str(e), status_code=400)

    logging.info(f"[zero_shot] tts_text={tts_text[:40]}..., prompt_text={prompt_text[:40]}...")
    is_upload = prompt_wav is not None and prompt_wav.filename
    try:
        pcm, code, msg = await _enqueue(
            cosyvoice.inference_zero_shot(tts_text, prompt_text, prompt_wav_path, stream=False),
            label="zero_shot",
        )
    finally:
        # 若参考音频是本次上传的临时文件，用完后清理
        if is_upload:
            _safe_remove(prompt_wav_path)
    if code != 200:
        return Response(msg or "inference failed", status_code=code)
    if not pcm:
        return Response("empty audio", status_code=500)
    return Response(content=pcm, media_type="application/octet-stream")


@app.post("/inference_instruct2")
async def inference_instruct2(
    tts_text: str = Form(...),
    instruct_text: str = Form(...),
    prompt_wav: Optional[UploadFile] = File(None),
    prompt_wav_path: str = Form(""),
):
    """CosyVoice2/3 指令控制合成。prompt_wav 仅用于音色参考，不需要 prompt_text。"""
    if cosyvoice is None:
        return Response("model not loaded", status_code=503)
    if not hasattr(cosyvoice, "inference_instruct2"):
        return Response("model does not support instruct2", status_code=400)
    try:
        prompt_wav_path = _resolve_prompt_wav_path(prompt_wav, prompt_wav_path)
    except (FileNotFoundError, ValueError) as e:
        logging.warning(f"[instruct2] 音频解析失败: {e}")
        return Response(str(e), status_code=400)

    logging.info(f"[instruct2] tts_text={tts_text[:40]}..., instruct_text={instruct_text[:40]}...")
    is_upload = prompt_wav is not None and prompt_wav.filename
    try:
        pcm, code, msg = await _enqueue(
            cosyvoice.inference_instruct2(tts_text, instruct_text, prompt_wav_path, stream=False),
            label="instruct2",
        )
    finally:
        if is_upload:
            _safe_remove(prompt_wav_path)
    if code != 200:
        return Response(msg or "inference failed", status_code=code)
    if not pcm:
        return Response("empty audio", status_code=500)
    return Response(content=pcm, media_type="application/octet-stream")


def main():
    parser = argparse.ArgumentParser(description="CosyVoice TTS API（队列版，兼容 CosyVoice1/2/3）")
    parser.add_argument(
        "--model_dir",
        default=os.path.join(ROOT_DIR, "pretrained_models", "Fun-CosyVoice3-0.5B-2512"),
        help="模型目录或 ModelScope/HuggingFace 仓库名",
    )
    parser.add_argument(
        "--voices_dir",
        default=os.path.join(ROOT_DIR, "cosyvoice_voices"),
        help="参考音频目录；插件通过 prompt_wav_path 传文件名时在此查找",
    )
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=50002)
    args = parser.parse_args()

    global VOICES_DIR, VOICES_JSON
    VOICES_DIR = args.voices_dir
    VOICES_JSON = os.path.join(VOICES_DIR, "voices.json")
    os.makedirs(VOICES_DIR, exist_ok=True)

    # ── CUDA 环境自检 ──
    try:
        import torch as _torch_check
        if not _torch_check.cuda.is_available():
            print("=" * 55)
            print("❌ 未检测到可用的 NVIDIA GPU")
            print()
            print("请确认：")
            print("  1. 本机安装的是 NVIDIA 显卡（非 Intel/AMD 集显）")
            print("  2. 显卡驱动版本 >= 572（CUDA 12.8 要求）")
            print("     → 驱动下载: https://www.nvidia.com/drivers")
            print("  3. 驱动安装后已重启系统")
            print("=" * 55)
            sys.exit(1)
        print(f"✅ CUDA 可用: {_torch_check.cuda.get_device_name(0)}")
        print(f"   显存: {_torch_check.cuda.get_device_properties(0).total_memory // 1024 // 1024 // 1024} GB")
    except ImportError:
        print("=" * 55)
        print("❌ 无法导入 PyTorch，环境可能损坏")
        print("   请确认依赖包安装完整")
        print("=" * 55)
        sys.exit(1)
    print()

    _load_model(args.model_dir)
    logging.info(f"启动 API 服务(队列版): http://{args.host}:{args.port}, voices_dir={VOICES_DIR}")
    # timeout 参数确保 worker 在请求/关闭异常卡死时能被强制回收，
    # 避免进程假死。推理本身的超时由 INFER_TIMEOUT 控制。
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        timeout_keep_alive=30,
        timeout_graceful_shutdown=15,
    )


if __name__ == "__main__":
    main()
