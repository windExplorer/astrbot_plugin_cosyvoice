# CosyVoice3 语音合成插件 · 方案与实现说明

> 本文档记录插件的设计决策、接口契约与实现细节，供跨设备/跨大模型协作时直接阅读。

## 一、目标

为 AstrBot 开发一个插件，接入本地自建的 **CosyVoice3** 推理服务，让机器人在回复时以可配置音色朗读语音。

## 二、关键决策（已与用户拍板）

| 项 | 决策 |
| --- | --- |
| 部署形态 | 本地自建 HTTP 服务 |
| 接入方式 | 全局接管 `on_llm_response`/`on_decorating_result` 钩子 + 手动指令 + LLM 工具 |
| 音色策略 | `prompt_wav` 参考音频映射多音色（音色名 → {prompt_wav, prompt_text}），**不做 zero-shot 克隆**，仅预配 |
| 默认发送模式 | `both`（文本+语音都发，**文本绝不丢失**） |
| 语音范围 | `tts_scope`：默认 `llm_only`（仅大模型回复转语音），可设 `all_text` |
| 默认行为 | `auto_tts=false`（默认不发语音），按需触发 |
| 触发方式 | ① LLM 函数调用工具 `text_to_speech`；② 用户 `/tts` 指令；③ 关键词触发（语音/念出来/读出来） |

## 三、目标服务接口（CosyVoice3）

插件 `cosyvoice/client.py` 对准一个**极简 FastAPI 推理服务**，随仓库提供：`deploy/cosyvoice_api.py`。

> 注意：常见的 CosyVoice3 WebUI 是 **Gradio** 应用（路由形如 `/gradio_api/...`，命名接口如
> `/generate_audio`），与插件期望的 `/inference_zero_shot` 原始接口**不兼容**，插件不会去调它。
> 因此我们在 CosyVoice3 环境里另起一个最小 FastAPI 服务来对接。

### 服务契约（与 client 完全对齐）
- 路由：`/inference_zero_shot`（本项目用，需 `tts_text` + `prompt_text` + 参考音频）、`/inference_instruct2`（可选）
- 请求：`multipart` 表单，字段 `tts_text`、`prompt_text`，参考音频二选一：
  - `prompt_wav_path`：**服务端本地文件名/路径**（推荐，不占上传带宽，需 server 启动时 `--voices_dir` 指定目录）
  - `prompt_wav`：AstrBot 服务端本地文件，以表单上传（回退模式）
- 额外 `GET /voices` 列出服务端参考音频目录文件与对应文本（来自 voices.json），便于核对
- 参考文本来源：`voices_dir` 下的 `voices.json`（`{"文件名": "文本"}`）为服务端权威来源；插件也可在请求里传 `prompt_text` 覆盖。服务端目录模式下插件 `prompt_text` 可留空。
- 响应：**裸 int16 PCM 字节流（无 WAV 头）** → 客户端用 `wave` 补 WAV 头
- 采样率：24000（CosyVoice3 默认），单声道

### 运行方式（在 CosyVoice3 环境，确保 `import cosyvoice` 可用）
```bash
cd <CosyVoice3 目录>
python <本仓库>/deploy/cosyvoice_api.py \
    --model_dir pretrained_models\Fun-CosyVoice3-0.5B-2512 \
    --host 0.0.0.0 --port 50000
```
- 若端口 50000 已被 Gradio 版占用，先停掉它或换端口，并相应修改插件 `base_url`。
- 模型可填本地路径或 HF/ModelScope 仓库名（脚本会自动下载/加载）。

### 关键实现
- 用 `CosyVoice(model_dir)` 加载模型；`inference_zero_shot(tts_text, prompt_text, prompt_speech_16k, stream=False)`。
- 参考音频经 `load_wav(path, 16000)` 转为 16k 张量。
- 输出张量 `.squeeze().cpu().numpy()` 后 `*32767` 截断为 int16，拼接为裸 PCM 返回。

### 兼容性说明
`CosyVoice3-0.5B-2512` 支持 `inference_zero_shot` 与 `inference_instruct2`；老接口 `sft/cross_lingual/instruct`
在该模型上通常不存在，本服务未暴露（避免误用）。插件当前也只调用 `zero_shot`。

## 四、配置项（`_conf_schema.json`）

| 配置 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `base_url` | string | `http://127.0.0.1:50000` | CosyVoice 服务地址 |
| `sample_rate` | int | `24000` | 采样率，需与模型一致 |
| `timeout` | int | `60` | 请求超时（秒） |
| `default_voice` | string | `""` | 默认音色名（需存在于 voices） |
| `voices` | object | `{}` | 音色名 → `{prompt_wav, prompt_text}` |
| `server_voices_dir` | string | `""` | CosyVoice 服务端参考音频目录；填后 `prompt_wav` 只传文件名，服务端本地读，避免大文件重复上传 |
| `send_mode` | enum | `both` | `both`=文本+语音；`voice_only`=只发语音 |
| `auto_tts` | bool | `false` | 是否自动为每条符合条件回复合成语音 |
| `tts_scope` | enum | `llm_only` | `llm_only`/`all_text` |
| `enable_llm_tool` | bool | `true` | 是否注册 `text_to_speech` 工具 |
| `enable_user_trigger` | bool | `true` | 是否启用关键词触发 |
| `trigger_keywords` | array | `["语音","念出来","读出来"]` | 触发关键词 |
| `blocklist` | array | `[]` | 命中则跳过（origin/sender_id） |
| `allowlist` | array | `[]` | 非空时仅列表内生效 |
| `max_text_len` | int | `200` | 长文本按此长度切分合成后拼接；0=不限制 |

## 五、触发判定逻辑

- `on_llm_response(event, resp)`：标记 `is_llm`；若消息含关键词则标记 `want`；若 `auto_tts` 则标记 `want`。
- `on_decorating_result(event)`：
  - 本插件的 `/tts` 或 LLM 工具已发语音 → `suppress` → 跳过（防重复）。
  - `tts_scope=llm_only`：`is_llm && (auto_tts || want)` 才合成。
  - `tts_scope=all_text`：`auto_tts || want` 即合成（含指令输出）。
  - 命中 `blocklist` 或不在 `allowlist` → 跳过。
  - 抽取结果链中的 `Plain` 文本 → 引擎合成 → 追加 `Record` 到链。

## 六、硬约束：文本绝不丢失上下文

- `both` 模式：原文 `Plain` 始终保留在结果链，语音作为 `Record` 追加，**天然满足**。
- `voice_only` 模式：从发送链移除 `Plain` 仅发 `Record`；但 LLM 的 `completion_text` 由 AstrBot **单独存入会话历史**，
  文字不会丢失。因此记忆插件与大模型下一轮仍能拿到文字。
- 绝不修改 `resp.completion_text`，避免清空历史。

## 七、文件结构

```
astrbot_plugin_cosyvoice/
├── main.py              # Star 入口：钩子 + /tts、/tts_voice 指令 + text_to_speech 工具
├── metadata.yaml
├── _conf_schema.json     # 配置 schema
├── PLAN.md               # 本文件
├── cosyvoice/
│   ├── __init__.py
│   └── client.py         # HTTP 客户端，对准官方服务，PCM→WAV
├── core/
│   ├── __init__.py
│   └── tts_engine.py     # 音色解析、长文本分片、多段拼接
└── utils/
    ├── __init__.py
    └── audio.py          # 裸 PCM 补 WAV 头
```

## 八、实现步骤

1. `_conf_schema.json` 配置表 ✅
2. `metadata.yaml` 元信息 ✅
3. `utils/audio.py`：PCM→WAV（内存 + 临时文件）✅
4. `cosyvoice/client.py`：HTTP 调用官方服务，返回 PCM / wav 路径 ✅
5. `core/tts_engine.py`：音色解析、按句分片、拼接合成 ✅
6. `main.py`：两个钩子 + 两条指令 + 一个 LLM 工具 ✅

## 九、保护点（防递归/重复/回声）

- LLM 工具或 `/tts` 已发语音 → `suppress` 标记，装饰钩子跳过。
- 结果链已含 `Record` → 不再合成（防递归）。
- 排除本插件指令自身输出（`/tts_voice` 等仅在 `all_text+auto` 下可能发声，影响极小）。
- 长文本按 `max_text_len` 切分，逐段合成后拼接。

## 十、使用前提（用户侧）

1. 部署 CosyVoice3 推理服务并监听 `base_url`（默认 50000）。
2. 准备若干参考音频 `wav` 与对应文本，填入 `voices`。
3. 在插件配置中设置 `default_voice` 与 `voices`。
4. 按需调整 `auto_tts` / `tts_scope` / `send_mode` / 触发关键词。
5. 触发：直接对话（配合关键词或 `auto_tts`）、发送 `/tts 你好`、或让大模型调用 `text_to_speech`。

## 十一、已知风险与待验证

- 官方 server 对 CosyVoice3-0.5B-2512 的加载兼容性（见第三节回退方案）。
- `Record` 仅部分平台支持（QQ 个人号/企业微信等），Telegram 等可能不支持语音组件。
- AstrBot 各版本 `on_llm_response` / `on_decorating_result` / `llm_tool` 的具体签名以运行版本为准（本文基于 v4.5.x 文档）。
- `voice_only` 依赖 AstrBot 单独保存 `completion_text` 的历史机制；若运行版本改为以结果链存历史，需补会话上下文写入兜底。
