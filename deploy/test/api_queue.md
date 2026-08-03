# CosyVoice TTS API（队列版）对接文档

- **版本**：队列版（基于 `cosyvoice_api_queue.py`，对应 `start_cosyvoice_api_queue.bat`）
- **适用模型**：CosyVoice / CosyVoice2 / CosyVoice3
- **与原版差异**：接口、请求参数、返回格式与原版（`docs/api.md`）**完全一致**，仅新增「服务端有界队列 + 单 worker 串行推理」以支撑多人并发，并多出 `GET /queue` 监控端点与 `429` 拒绝语义。**客户端只需改端口即可对接。**

---

## 一、为什么用队列版

原版（`cosyvoice_api.py`，端口 50000）使用全局串行锁，高并发时请求会**阻塞堆积**，导致服务越堆越慢甚至卡死。

队列版（端口 50002）专为解决「即时通信机器人 + 多人同时触发回复」这类场景：

- 所有推理请求进入一个**有界队列**（默认长度 8）；
- **单个 worker 协程串行消费**，保证同一时刻只跑一个 GPU 推理，避免 CUDA 上下文竞争 / event loop 卡死；
- **队满或入队等待超时 → 返回 `429`**，让客户端退避重试，而不是无限堆积拖垮服务；
- 单次推理超时（默认 120s）→ 返回 `504`；模型未加载 → 返回 `503`。

---

## 二、服务启动

双击 `start_cosyvoice_api_queue.bat`，或命令行启动：

```bash
uv run python cosyvoice_api_queue.py \
  --model_dir pretrained_models/Fun-CosyVoice3-0.5B-2512 \
  --voices_dir cosyvoice_voices \
  --port 50002
```

| 参数 | 说明 | 默认值 |
| --- | --- | --- |
| `--model_dir` | 模型目录或 ModelScope/HuggingFace 仓库名 | （必填） |
| `--voices_dir` | 参考音频目录，服务端在此查找 `wav` 文件 | `cosyvoice_voices` |
| `--host` | 监听地址 | `0.0.0.0` |
| `--port` | 监听端口 | `50002` |

### 队列行为配置（环境变量，可选）

| 环境变量 | 说明 | 默认值 |
| --- | --- | --- |
| `COSY_QUEUE_SIZE` | 队列长度上限，超过后新请求直接返回 `429` | `8` |
| `COSY_QUEUE_WAIT` | 入队最长等待秒数，队满时最多等这么久仍进不去就返回 `429` | `30` |
| `COSY_INFER_TIMEOUT` | 单次推理超时秒数，超过返回 `504` | `120` |

`start_cosyvoice_api_queue.bat` 已默认设置这三个变量（8 / 30 / 120），按需修改即可。

---

## 三、端点总览

| 端点 | 方法 | 说明 |
| --- | --- | --- |
| `/` | GET | 健康检查 + 返回采样率 |
| `/queue` | GET | **（新增）** 实时查看队列长度与 worker 状态 |
| `/voices` | GET | 列出服务端可用参考音频及参考文本 |
| `/inference_zero_shot` | POST | 零样本音色复刻 TTS |
| `/inference_instruct2` | POST | 自然语言指令合成（CosyVoice2/3） |

> 两个推理端点的请求字段、返回格式与原版完全一致（详见 `docs/api.md` 第三、四、五节），本文不再重复。

---

## 四、GET `/queue`（监控用）

返回当前队列与 worker 状态，便于监控/调试。

**响应** `200 application/json`

```json
{
  "queue_maxsize": 8,
  "in_queue": 2,
  "worker": {
    "state": "busy",
    "started_at": 1722720000.1,
    "current": "zero_shot",
    "running_seconds": 3.4
  }
}
```

- `in_queue`：当前排队中的请求数。
- `worker.state`：`idle`（空闲）/ `busy`（推理中）。
- `worker.current`：正在处理的任务标签（`zero_shot` / `instruct2`）。
- `worker.running_seconds`：当前任务已运行秒数（仅 `busy` 时存在）。

---

## 五、状态码（客户端必须处理）

队列版相比原版**新增 `429` 与 `504`**，客户端务必区分处理：

| 状态码 | 内容类型 | 说明 | 客户端建议 |
| --- | --- | --- | --- |
| `200` | `application/octet-stream` | 成功，int16 PCM 原始字节流 | 正常解析播放 |
| `400` | `text/plain` | 参数错误（缺参考音频/文本、文件不存在、模型不支持 instruct2） | 终止本次请求，提示配置错误 |
| `429` | `text/plain` | 服务繁忙：队列已满或入队等待超时 | **退避重试**（见第六节） |
| `503` | `text/plain` | 模型尚未加载完成 | 稍后重试 |
| `504` | `text/plain` | 单次推理超时（> `COSY_INFER_TIMEOUT`） | 可重试，或缩短文本 |
| `500` | `text/plain` | 服务端生成失败 / 空音频 | 记录日志，谨慎重试 |

非 `200` 时响应体为纯文本错误信息。

---

## 六、客户端接入（重点：端口 + 429 重试）

### 改动点

1. **端口**：从原版 `50000` 改为队列版 `50002`。
2. **必须处理 `429`**：收到 `429` 时按退避策略重试，否则高峰期会丢消息。

请求构造、表单字段、`prompt_wav_path` 用法、PCM 解码/播放逻辑、采样率（24000）全部不变。

### Python 客户端示例（含 429 退避重试）

```python
import time
import requests
import numpy as np
import soundfile as sf

BASE = "http://localhost:50002"   # 注意：队列版端口 50002

def synthesize(text: str, voice: str = "xiaoyu.wav", max_retry: int = 3) -> bytes | None:
    """带 429 退避重试的合成封装。返回 int16 PCM 字节，失败返回 None。"""
    for attempt in range(max_retry):
        r = requests.post(f"{BASE}/inference_zero_shot", data={
            "tts_text": text,
            "prompt_wav_path": voice,
        }, timeout=130)   # 略大于服务端推理超时，避免客户端先超时
        if r.status_code == 200:
            return r.content
        if r.status_code == 429:
            wait = 0.5 * (2 ** attempt)   # 0.5s, 1s, 2s 指数退避
            print(f"[重试] 服务繁忙(429)，{wait:.1f}s 后重试 ({attempt+1}/{max_retry})")
            time.sleep(wait)
            continue
        # 其它错误直接暴露
        print(f"[错误] HTTP {r.status_code}: {r.text}")
        return None
    print("[失败] 多次重试后仍被拒绝")
    return None

# 使用
pcm = synthesize("你好，今天天气真不错。", voice="xiaoyu.wav")
if pcm:
    sr = requests.get(f"{BASE}/").json()["sample_rate"]
    audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32767.0
    sf.write("output.wav", audio, sr)
```

### curl 示例

```bash
# 健康检查
curl http://localhost:50002/

# 队列状态监控
curl http://localhost:50002/queue

# 通过文件名引用（推荐）
curl -X POST http://localhost:50002/inference_zero_shot \
  -F "tts_text=你好，今天天气不错。" \
  -F "prompt_wav_path=xiaoyu.wav"

# 指令合成
curl -X POST http://localhost:50002/inference_instruct2 \
  -F "tts_text=今天太开心了！" \
  -F "instruct_text=请用开心的语气说" \
  -F "prompt_wav_path=xiaoyu.wav"
```

---

## 七、与原版的共存

- 原版（`50000`）与队列版（`50002`）可**同时运行、互不干扰**，目录与模型共用。
- 切换时客户端仅改端口；建议最终统一使用队列版（多人并发场景更稳）。
- 队列版新增的 `GET /queue` 仅用于监控，对接时非必需。

---

## 八、音频格式

返回值为 int16 PCM 原始字节流：

- **格式**：int16 PCM（小端序，little-endian）
- **采样率**：通过 `GET /` 获取（通常 24000 Hz）
- **声道**：单声道（mono）

> 音色配置（`voices.json`）、参考音频质量建议等，详见 `docs/api.md` 第五、八节，队列版与原版一致。
