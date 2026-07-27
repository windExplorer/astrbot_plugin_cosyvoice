# astrbot_plugin_cosyvoice

AstrBot 插件：接入本地自建 **CosyVoice3** 推理服务，让机器人以可配置音色朗读回复。

> 详细方案、接口契约与风险见 [PLAN.md](./PLAN.md)。

## 功能

- 接入官方 QwenAudio/CosyVoice 的 FastAPI 推理服务（默认端口 50000），模型 `FunAudioLLM/Fun-CosyVoice3-0.5B-2512`。
- 多音色：`prompt_wav` 参考音频映射，按需切换（`/tts_voice <音色名>`）。
- 多种触发方式：
  1. **会话级持久开关**（推荐按群使用）：`/tts_on` 在当前群开启「一直语音」，`/tts_off` 关闭；状态按群持久保存（重启不丢），其他群不受影响。
  2. **自然语言开关**：对话中说「以后用语音交流」「别用语音了」，模型自动调用 `set_voice_mode` 工具开关当前群语音（由 `skill.md` 引导语义，不靠写死关键词）。
  3. **自动**：`auto_tts=true` 时对符合条件的回复自动合成语音（全局生效）。
  4. **关键词**：用户消息含触发词（默认「念出来 / 读出来」）。
  5. **LLM 工具 / 指令**：大模型可调用 `text_to_speech` 念指定文本；用户可发 `/tts <文本>`。
- 文本绝不丢失：`both` 模式下原文与语音一同发送，且 LLM 文本由 AstrBot 单独存入会话历史，记忆插件与大模型下一轮都能拿到文字。

## 配置（`_conf_schema.json`）

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `base_url` | `http://127.0.0.1:50000` | CosyVoice 服务地址 |
| `server_voices_dir` | `""` | CosyVoice 服务端参考音频目录（填后 `prompt_wav` 只传文件名，服务端本地读，避免大文件重复上传） |
| `sample_rate` | `24000` | 采样率（Hz）。插件首次合成时会向服务端 `/` 接口查询真实采样率并自动覆盖此值 |
| `timeout` | `60` | 请求超时（秒） |
| `default_voice` | `""` | 默认音色名 |
| `voices` | `{}` | 音色名 → `{prompt_wav, prompt_text}` |
| `send_mode` | `both` | `both` / `voice_only` |
| `auto_tts` | `false` | 是否自动语音 |
| `tts_scope` | `llm_only` | `llm_only` / `all_text` |
| `enable_llm_tool` | `true` | 注册 `text_to_speech` 工具 |
| `enable_user_trigger` | `true` | 启用关键词触发 |
| `trigger_keywords` | `["念出来","读出来"]` | 触发关键词（已去除过于宽泛的「语音」，改由 skill.md 语义触发接管「用语音」场景） |
| `blocklist` / `allowlist` | `[]` | 会话/用户白黑名单 |
| `max_text_len` | `200` | 长文本切分长度 |

## 部署注意事项（拓扑）

本插件运行在 **AstrBot 服务端**，CosyVoice 推理服务通常在另一台机器（如本机）。需注意：

- `base_url` 必须是**从 AstrBot 服务端网络可达**的 CosyVoice 地址（本地机需做端口映射/内网穿透/同网段）。
- **参考音频放哪（推荐放 CosyVoice 服务端）**：把 wav 放到 CosyVoice 机器的 `--voices_dir` 目录，并同目录放 `voices.json`（`{"文件名": "参考文本"}`）。插件配置 `server_voices_dir` 填同样路径、`voices.<音色>.prompt_wav` 只写文件名、`prompt_text` 可留空（服务端从 `voices.json` 自动取）。这样插件**只传文件名**，服务端读本地文件与文本，大文件不重复上传。
- **回退（上传模式）**：不填 `server_voices_dir` 时，`prompt_wav` 视为 AstrBot 服务端本地路径，插件读取后以表单上传（此时 wav 需放在 AstrBot 服务端，文本仍需在 `prompt_text` 填）。
- 当前跑着的 CosyVoice WebUI 多为 **Gradio**（`/gradio_api/...`），与插件接口不兼容。请改用仓库内 `deploy/cosyvoice_api.py` 起一个最小 FastAPI 服务，端口若冲突先停掉 Gradio 版或换端口。
  **服务端怎么部署、怎么放音频与文本、接口与排错**，见 [deploy/README.md](./deploy/README.md)。

## 使用前提

1. 部署 CosyVoice3 推理服务并监听 `base_url`（默认 50000）。
2. 准备参考音频 `wav` 与对应文本，填入 `voices`，并设置 `default_voice`。
3. 触发示例：
   - 发送 `/tts_on` → 当前群开启「一直语音」（持久，重启不丢）；`/tts_off` 关闭。
   - 对话中说「以后都用语音跟我交流」→ 模型自动调用 `set_voice_mode` 开启当前群语音；说「别用语音了」则关闭。
   - 对话中说「把这段话**念出来**」→ 关键词触发语音。
   - 发送 `/tts 你好呀` → 直接朗读某段文本。
   - 发送 `/tts_voice 小明` → 切换默认音色。
   - 让大模型自己决定：「把这段念出来」→ 大模型调用 `text_to_speech`。

## 打包与上传到 AstrBot

把本插件打包成 zip 后，在 AstrBot 后台「插件市场 → 本地插件 → 从文件安装」上传即可。

```bash
# Windows：双击 pack.bat，或
python pack.py

# Linux / macOS：
bash pack.sh
# 或
python3 pack.py
```

- 输出位于 `dist/astrbot_plugin_cosyvoice.zip`，zip 顶层目录即为插件名 `astrbot_plugin_cosyvoice/`，AstrBot 解压后可直接识别。
- 自动排除无需上线的内容：`.git`、`__pycache__`、`*.pyc`、`.venv`、`node_modules`、`dist`、打包脚本自身。
- `deploy/`（CosyVoice 服务端）也会一并打包进插件仓库 zip，但 AstrBot 运行时不会加载它；服务端需单独按 [deploy/README.md](./deploy/README.md) 在 CosyVoice 机器上部署。
- 上传后记得在插件配置里填好 `base_url`、`server_voices_dir`、各音色的 `prompt_wav`（文件名）与 `prompt_text`，并重启插件生效。

## 目录结构

```
astrbot_plugin_cosyvoice/
├── main.py            # Star 入口：钩子 + 指令 + 工具
├── skill.md           # 注入 LLM 系统提示，引导语义触发语音工具
├── metadata.yaml
├── _conf_schema.json
├── PLAN.md
├── deploy/                 # CosyVoice 服务端：cosyvoice_api.py + 启动脚本 + 部署文档
├── cosyvoice/client.py
├── core/tts_engine.py
└── utils/audio.py
```
