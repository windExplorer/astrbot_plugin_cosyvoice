#!/usr/bin/env python3
"""CosyVoice TTS 推理 API（兼容 CosyVoice1/2/3）。

与 astrbot_plugin_cosyvoice 的 client.py 完全对齐，返回裸 int16 PCM 字节流。

运行：
    # 本机开发
    uv run python cosyvoice_api.py --model_dir pretrained_models/Fun-CosyVoice3-0.5B-2512

    # 或双击
    start_cosyvoice_api.bat

接口：
    GET  /                             健康检查
    GET  /voices                       列出参考音频目录中的文件
    POST /inference_zero_shot          表单: tts_text, prompt_text, prompt_wav(文件)/prompt_wav_path(路径)
                                        -> 裸 int16 PCM
    POST /inference_instruct2          表单: tts_text, instruct_text, prompt_wav(文件)/prompt_wav_path(路径)
                                        -> 裸 int16 PCM（CosyVoice2/3）

参考音频推荐放到 --voices_dir 目录，并同目录放 voices.json：
    { "xiaoyu.wav": "你好，我是小宇，很高兴为你服务。" }
插件传 prompt_wav_path=文件名 即可，不用每次上传大文件。
"""

import argparse
import json
import os
import sys
import tempfile
from typing import Optional

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT_DIR, "third_party", "Matcha-TTS"))

import numpy as np
import uvicorn
from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import Response

from cosyvoice.cli.cosyvoice import AutoModel
from cosyvoice.utils.file_utils import load_wav, logging

app = FastAPI(title="CosyVoice TTS API")

cosyvoice = None
SAMPLE_RATE = 24000
VOICES_DIR = os.path.join(ROOT_DIR, "cosyvoice_voices")
VOICES_JSON = os.path.join(VOICES_DIR, "voices.json")


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


def _load_prompt_wav(upload: UploadFile) -> "object":
    suffix = os.path.splitext(upload.filename or "prompt.wav")[1] or ".wav"
    fd, path = tempfile.mkstemp(suffix=suffix)
    with os.fdopen(fd, "wb") as f:
        f.write(upload.file.read())
    try:
        return load_wav(path, 16000)
    finally:
        os.remove(path)


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


def _resolve_prompt_speech(upload: Optional[UploadFile], server_path: str):
    """解析参考音频：优先用服务端本地路径（无需上传），否则用上传文件。"""
    if server_path:
        if os.path.isabs(server_path) and os.path.exists(server_path):
            path = server_path
        else:
            cand = os.path.join(VOICES_DIR, server_path)
            if not os.path.exists(cand):
                raise FileNotFoundError(f"服务端参考音频不存在: {server_path}（在 {VOICES_DIR} 中未找到）")
            path = cand
        return load_wav(path, 16000)
    if upload is not None and upload.filename:
        return _load_prompt_wav(upload)
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


@app.get("/")
def health():
    return {"status": "ok", "model_loaded": cosyvoice is not None, "sample_rate": SAMPLE_RATE}


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
    try:
        prompt_speech = _resolve_prompt_speech(prompt_wav, prompt_wav_path)
        prompt_text = _resolve_prompt_text(prompt_text, prompt_wav_path)
        if not prompt_text:
            logging.warning(
                f"[zero_shot] 缺少参考文本: prompt_text为空且voices.json未配置, "
                f"prompt_wav_path={prompt_wav_path or '(上传)'}"
            )
            return Response("缺少参考文本：请在请求中传 prompt_text，或在 voices.json 配置该文件的文本", status_code=400)
    except (FileNotFoundError, ValueError) as e:
        logging.warning(f"[zero_shot] 音频解析失败: {e}")
        return Response(str(e), status_code=400)
    logging.info(f"[zero_shot] tts_text={tts_text[:40]}..., prompt_text={prompt_text[:40]}...")
    gen = cosyvoice.inference_zero_shot(tts_text, prompt_text, prompt_speech, stream=False)
    pcm = _synthesize(gen)
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
        prompt_speech = _resolve_prompt_speech(prompt_wav, prompt_wav_path)
    except (FileNotFoundError, ValueError) as e:
        logging.warning(f"[instruct2] 音频解析失败: {e}")
        return Response(str(e), status_code=400)
    logging.info(f"[instruct2] tts_text={tts_text[:40]}..., instruct_text={instruct_text[:40]}...")
    gen = cosyvoice.inference_instruct2(tts_text, instruct_text, prompt_speech, stream=False)
    pcm = _synthesize(gen)
    if not pcm:
        return Response("empty audio", status_code=500)
    return Response(content=pcm, media_type="application/octet-stream")


def main():
    parser = argparse.ArgumentParser(description="CosyVoice TTS API（兼容 CosyVoice1/2/3）")
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
    parser.add_argument("--port", type=int, default=50000)
    args = parser.parse_args()

    global VOICES_DIR, VOICES_JSON
    VOICES_DIR = args.voices_dir
    VOICES_JSON = os.path.join(VOICES_DIR, "voices.json")
    os.makedirs(VOICES_DIR, exist_ok=True)

    _load_model(args.model_dir)
    logging.info(f"启动 API 服务: http://{args.host}:{args.port}, voices_dir={VOICES_DIR}")
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
