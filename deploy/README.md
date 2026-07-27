# CosyVoice3 推理服务部署指南（服务端）

本目录包含让 `astrbot_plugin_cosyvoice` 插件能用的 **CosyVoice3 极简推理服务**。插件本身只负责调用，真正的语音合成由这个服务完成。

> 为什么不用官方 WebUI？官方 CosyVoice3 多为 Gradio 应用（路由形如 `/gradio_api/...`），与插件期望的 `/inference_zero_shot` 接口不一致，所以这里提供一个最小 FastAPI 服务。

---

## 1. 环境准备

在**运行 CosyVoice 的机器**上，把本目录的 `cosyvoice_api.py` 放到 **CosyVoice 仓库根目录**（与 `pretrained_models/`、`third_party/` 同级）。推荐用 [uv](https://github.com/astral-sh/uv) 启动，它会直接使用 CosyVoice 项目自带的环境与依赖：

```bash
# 在 CosyVoice 仓库根目录执行（脚本已在此处）
uv run python cosyvoice_api.py --model_dir pretrained_models/Fun-CosyVoice3-0.5B-2512 --port 50000
```

- 脚本会自动把 `third_party/Matcha-TTS` 加入 `sys.path`，并兼容新版 `AutoModel` 与旧版 `CosyVoice` 入口，无需手动处理。
- 确保 `import cosyvoice` 可用、模型已下载到本地（默认目录 `pretrained_models/Fun-CosyVoice3-0.5B-2512`，相对脚本目录）。
- 若不想用 uv，也可在已激活的 cosyvoice 环境里直接 `python cosyvoice_api.py ...`。

---

## 2. 把文件放到 CosyVoice 机器

把本目录的 `cosyvoice_api.py` 放到 CosyVoice 环境下（建议放在 CosyVoice 仓库根目录，这样相对路径 `pretrained_models/...` 才正确）。`start_cosyvoice_api.bat` 仅 Windows 用，Linux/macOS 直接用下面的命令行。

---

## 3. 启动服务

### 方式 A：Windows 双击（推荐）

直接双击 `start_cosyvoice_api.bat`，按需要修改文件顶部的变量：

> 该 bat 已内置 `chcp 65001` + `PYTHONUTF8=1`，且以 UTF-8 BOM 保存；脚本内也强制 stdout/stderr 用 UTF-8，控制台中文不会乱码。若你用自己的方式启动（如命令行），请保证控制台代码页为 UTF-8 或设置 `PYTHONUTF8=1`。

| 变量 | 说明 |
| --- | --- |
| `VENV_DIR` | uv 虚拟环境目录，默认 bat 同目录下的 `.venv`（不存在则直接用当前 python） |
| `MODEL_DIR` | 模型目录，默认 `pretrained_models\Fun-CosyVoice3-0.5B-2512` |
| `VOICES_DIR` | 参考音频目录，默认 bat 同目录下的 `cosyvoice_voices` |
| `PORT` | 监听端口，默认 `50000`（被 Gradio 占用就换一个或先停掉它） |
| `SCRIPT` | `cosyvoice_api.py` 路径（若你移到别处需改这里） |

### 方式 B：命令行（任意系统，推荐在 CosyVoice 仓库根目录执行）

```bash
# uv 自动使用 CosyVoice 项目环境
uv run python cosyvoice_api.py \
    --model_dir pretrained_models/Fun-CosyVoice3-0.5B-2512 \
    --voices_dir /path/to/cosyvoice_voices \
    --host 0.0.0.0 \
    --port 50000
```

### 参数说明

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--model_dir` | `pretrained_models\Fun-CosyVoice3-0.5B-2512`（相对脚本目录） | 模型目录或 HuggingFace/ModelScope 仓库名 |
| `--voices_dir` | `./cosyvoice_voices` | 参考音频目录，不存在会自动创建 |
| `--host` | `0.0.0.0` | 监听地址 |
| `--port` | `50000` | 监听端口 |

---

## 4. 准备参考音频与文本

参考音频和文本**放 CosyVoice 服务端**最省事（避免大文件每次从 AstrBot 上传）。

1. 把参考 `wav` 放进 `--voices_dir` 目录（如 `cosyvoice_voices/xiaoyu.wav`）。
   - 建议 3~10 秒、环境安静、无明显噪声。
2. 同目录放 `voices.json`，绑定 `文件名 → 参考文本`（详见 `cosyvoice_voices/README.md`）：

```json
{
  "xiaoyu.wav": "你好，我是小宇，很高兴为你服务。",
  "boss.wav": "这件事交给我来处理。"
}
```

3. 改完 `voices.json` **无需重启**，下次请求即生效。

---

## 5. 接口一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | 健康检查：`{"status":"ok","model_loaded":true,"sample_rate":24000}` |
| GET | `/voices` | 列出 `--voices_dir` 中的文件及文本映射，便于核对 |
| POST | `/inference_zero_shot` | zero-shot 克隆合成 |
| POST | `/inference_instruct2` | 带指令的合成（按模型支持） |

`/inference_zero_shot` 表单字段：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `tts_text` | 是 | 要朗读的文本 |
| `prompt_text` | 否* | 参考文本；不传则自动取 `voices.json` 中对应文件的值 |
| `prompt_wav` | 否** | 上传参考音频文件（与 `prompt_wav_path` 二选一） |
| `prompt_wav_path` | 否** | 服务端本地参考音频路径/文件名（推荐），服务端本地读，不重复上传 |

\* 若既没传 `prompt_text`、也没在 `voices.json` 配对应文本，返回 400。
\** `prompt_wav` 与 `prompt_wav_path` 至少给一个；两者都缺返回 400。

响应为**裸 int16 PCM 字节流**（24kHz 单声道，无 WAV 头），由插件端补 WAV 头。

---

## 6. 验证服务可用

服务起来后，浏览器或命令行访问：

```bash
# 健康检查
curl http://127.0.0.1:50000/

# 查看已配置的参考音频与文本
curl http://127.0.0.1:50000/voices
```

若 `model_loaded` 为 `true`、`/voices` 能列出文件，说明服务正常，可在 AstrBot 插件里把 `base_url` 指向本机地址（跨机器时确保网络可达）。

---

## 7. 排错

- **`model not loaded` (503)**：模型还在加载，稍等重试；或检查 `--model_dir` 路径是否正确。
- **`服务端参考音频不存在` (400)**：`prompt_wav_path` 指向的文件不在 `--voices_dir` 内，核对文件名与目录。
- **`缺少参考文本` (400)**：该 wav 既没传 `prompt_text`，`voices.json` 也没配文本。
- **端口被占用**：Gradio 版 WebUI 可能占了 50000，先停掉它或换 `--port`。
- **跨机器连不上**：确认 `host=0.0.0.0` 且防火墙/端口映射放行；AstrBot 端 `base_url` 用 CosyVoice 机器的可达 IP。
