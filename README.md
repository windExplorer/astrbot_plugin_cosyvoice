# astrbot_plugin_cosyvoice

AstrBot 插件：接入本地自建 **CosyVoice3** 推理服务，让机器人以可配置音色朗读回复。

> 详细方案、接口契约与风险见 [PLAN.md](./PLAN.md)。

## 功能

- 接入官方 QwenAudio/CosyVoice 的 FastAPI 推理服务（默认端口 50000），模型 `FunAudioLLM/Fun-CosyVoice3-0.5B-2512`。
- 多音色：`prompt_wav` 参考音频映射，可按会话切换（`/tts_voice <音色名>`）。
- 多种触发方式：会话级持久开关（`/tts_on`、`/tts_off`）、自然语言开关、关键词触发、自动语音、LLM 工具 / 指令。详见下表。
- 文本绝不丢失：`both` 模式下原文与语音一同发送，且 LLM 文本由 AstrBot 单独存入会话历史，记忆插件与大模型下一轮都能拿到文字。
- 语音开关、音色均**按会话（群聊或私聊）独立持久记忆**，重启不丢，互不影响。

## 指令一览

| 指令 / 触发方式 | 作用 | 是否持久 | 说明 |
| --- | --- | --- | --- |
| `/tts_on` | 开启当前聊天的自动语音 | ✅ 持久 | 该聊天每句回复都念出来（重启不丢） |
| `/tts_off` | 关闭当前聊天的自动语音 | ✅ 持久 | 关闭后恢复文字回复 |
| `/tts_status` | 查看当前聊天状态 | — | 显示开关状态、当前会话音色、全局默认音色、记录文件 |
| `/tts_export` | 导出当前音色配置为 JSON | — | 把已配置的所有音色导出成 JSON（代码块或文件），便于备份 / 迁移 |
| `/tts <文本>` | 临时朗读某段文本 | ❌ 一次性 | 念一次，不开启语音模式。例：`/tts 你好呀` |
| `/tts_voice <音色名>` | 切换当前聊天的音色 | ✅ 持久 | 仅本聊天生效，其他聊天不受影响；不带参数可查看可用音色列表 |
| 自然语言「以后用语音跟我交流」 | 模型自动开启语音 | ✅ 持久 | 由内置 Skill 引导模型调用 `set_voice_mode`（不靠写死关键词） |
| 自然语言「别用语音了 / 用文字回复」 | 模型自动关闭语音 | ✅ 持久 | 同上，关闭当前会话语音 |
| 关键词「念出来 / 读出来」 | 触发语音朗读 | ❌ 一次性 | 命中 `trigger_keywords` 时对当前回复合成语音 |
| 大模型调用 `text_to_speech` | 念指定文本 | ❌ 一次性 | LLM 工具，模型自行决定朗读哪些内容 |
| 大模型调用 `list_voices` | 列出可用音色 | — | LLM 工具，回答「你有哪些声音」类问题 |
| 大模型调用 `set_voice <音色名>` | 切换当前聊天音色 | ✅ 持久 | LLM 工具，模型按自然语言（如「换成小明的声音」）切换音色 |

## WebUI 管理面板（插件 Pages）

本插件提供一个 **Dashboard 内嵌管理页**（AstrBot 插件 Pages），在 WebUI「插件 → 本插件详情 → CosyVoice 语音管理」打开。

三个 Tab：

- **概览**：服务端健康（各节点在线/熔断冷却剩余秒）、全局配置（自动语音/发送方式/范围/默认音色/采样率）、统计。
- **音色管理**：音色表格（参考音频/文本/音频可达性/隐藏），**试听**（优先服务端直链 `/synthesize_save`，浏览器直连播放并本地缓存；服务端未升级时回退本地合成下载），设默认、隐藏切换。
- **会话管理**：按会话（origin）查看/切换语音开关、音色、发送方式，搜索 + 批量关闭。

**与配置弹窗的关系（解耦）**：

- 会话开关/会话音色/发送方式、WebUI 设置的默认音色，均存插件自有 `data/tts_*.json`（与聊天指令 `/tts_on`、`/tts_voice` 同源），**重启不丢、热生效**，与 AstrBot 主配置解耦。
- 音色本身的 `prompt_wav / prompt_text` 由 AstrBot 配置弹窗（`_conf_schema.json` 的 voices template_list）维护，WebUI 只读展示 + 快捷操作。

**服务端要求**：试听直链需要 CosyVoice 服务端升级到新版 `deploy/cosyvoice_api.py`（含 `POST /synthesize_save` 与 `GET /audio/{name}`）；未升级也能用（WebUI 回退本地合成下载播放）。详见 [deploy/README.md](./deploy/README.md)。

## 配置（`_conf_schema.json`）

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `base_url` | `http://127.0.0.1:50002` | CosyVoice 服务地址（单机模式；配置了 `servers` 后仅作全部节点不可用时的回退） |
| `servers` | `[]` | **多机分流**：可配置多台 CosyVoice 服务地址（`url` / `enabled` / `default` / `weight`），同时启用、按权重分流；「设为默认」的节点优先使用；某台故障自动临时隔离，其余节点继续服务 |
| `server_voices_dir` | `""` | CosyVoice 服务端参考音频目录（填后 `prompt_wav` 只传文件名，服务端本地读，避免大文件重复上传） |
| `sample_rate` | `24000` | 采样率（Hz）。插件首次合成时会向服务端 `/` 接口查询真实采样率并自动覆盖此值 |
| `timeout` | `60` | 请求超时（秒） |
| `default_voice` | `""` | 默认音色名 |
| `voices` | `[]` | 音色列表（**template_list 逐条编辑**）：每条含「音色名 / 参考音频 / 参考文本」，可增删 |
| `send_mode` | `both` | `both` / `voice_only` |
| `auto_tts` | `false` | 是否自动语音 |
| `tts_scope` | `llm_only` | `llm_only`（仅大模型回复）/ `all_text`（所有文本） |

> `tts_scope=llm_only` 时只对「大模型回复」转语音，其他插件返回的固定文案、指令输出等不会被转成语音。
| `enable_llm_tool` | `true` | 注册 `text_to_speech` 工具 |
| `enable_user_trigger` | `true` | 启用关键词触发 |
| `trigger_keywords` | `["念出来","读出来"]` | 触发关键词（已去除过于宽泛的「语音」，改由内置 SKILL.md 语义触发接管「用语音」场景） |
| `text_keywords` | `["用文字","发文字","文字回复","别用语音","这段别念","不用语音念"]` | 纯文字请求关键词：即使已开语音，用户说这些词时**本条只发文字、不合成语音**（不改变 tts_on 开关）。需 `enable_user_trigger=true` |
| `blocklist` / `allowlist` | `[]` | 会话/用户白黑名单 |
| `max_text_len` | `200` | 长文本切分长度 |

## 部署注意事项（拓扑）

本插件运行在 **AstrBot 服务端**，CosyVoice 推理服务通常在另一台机器（如本机）。需注意：

- `base_url` 必须是**从 AstrBot 服务端网络可达**的 CosyVoice 地址（本地机需做端口映射/内网穿透/同网段）。
- **多机分流**：在「服务端列表」`servers` 中添加多台地址即可同时启用并按权重分流（`weight` 越大概率越高）。某台连续失败 3 次会被临时隔离 30 秒，期间请求自动走其他节点，隔离到期后自动恢复探测；全部节点都不可用时自动回退到 `base_url` 单机模式。
- **参考音频放哪（推荐放 CosyVoice 服务端）**：把 wav 放到 CosyVoice 机器的 `--voices_dir` 目录，并同目录放 `voices.json`（`{"文件名": "参考文本"}`）。插件配置 `server_voices_dir` 填同样路径、`voices.<音色>.prompt_wav` 只写文件名、`prompt_text` 可留空（服务端从 `voices.json` 自动取）。这样插件**只传文件名**，服务端读本地文件与文本，大文件不重复上传。
- **回退（上传模式）**：不填 `server_voices_dir` 时，`prompt_wav` 视为 AstrBot 服务端本地路径，插件读取后以表单上传（此时 wav 需放在 AstrBot 服务端，文本仍需在 `prompt_text` 填）。
- 当前跑着的 CosyVoice WebUI 多为 **Gradio**（`/gradio_api/...`），与插件接口不兼容。请改用仓库内 `deploy/cosyvoice_api.py` 起一个最小 FastAPI 服务，端口若冲突先停掉 Gradio 版或换端口。
  **服务端怎么部署、怎么放音频与文本、接口与排错**，见 [deploy/README.md](./deploy/README.md)。

## 使用前提

1. 部署 CosyVoice3 推理服务并监听 `base_url`（默认 50000）。
2. 在「音色列表」里逐条添加音色（音色名 / 参考音频 / 参考文本），并设置 `default_voice`。可用 `/tts_export` 备份当前配置。
3. 常用操作见上方「指令一览」表，例如：
   - 在目标聊天发送 `/tts_on` 开启自动语音，`/tts 你好呀` 临时朗读，`/tts_voice 小明` 切换该聊天音色。
   - 对话中说「以后用语音跟我交流」让模型自动开启；说「别用语音了」自动关闭。

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

- 输出位于 `dist/astrbot_plugin_cosyvoice_v<版本号>.zip`（同名不覆盖，重打会追加时间戳），zip 顶层目录即为插件名 `astrbot_plugin_cosyvoice/`，AstrBot 解压后可直接识别。
- 自动排除无需上线的内容：`.git`、`__pycache__`、`*.pyc`、`.venv`、`node_modules`、`dist`、打包脚本自身，以及前端源码 `frontend/`（构建产物 `pages/cosyvoice/` 会打包进去，供 WebUI 使用）。
- `deploy/`（CosyVoice 服务端）也会一并打包进插件仓库 zip，但 AstrBot 运行时不会加载它；服务端需单独按 [deploy/README.md](./deploy/README.md) 在 CosyVoice 机器上部署。
- 上传后记得在插件配置里填好 `base_url`、`server_voices_dir`、各音色的 `prompt_wav`（文件名）与 `prompt_text`，并重启插件生效。

## 目录结构

```
astrbot_plugin_cosyvoice/
├── main.py            # Star 入口：钩子 + 指令 + 工具 + WebUI 后端 API 注册
├── skills/cosyvoice_voice_mode/SKILL.md  # 内置 Skill：引导 LLM 语义触发语音工具（大写文件名+子目录，符合 AstrBot 规范）
├── metadata.yaml
├── _conf_schema.json
├── PLAN.md
├── .astrbot-plugin/i18n/   # 插件 Pages 国际化资源（zh/en/ja/ko）
├── pages/cosyvoice/        # WebUI 前端构建产物（AstrBot 自动发现为插件 Page）
├── frontend/               # WebUI 前端源码（Vue3+Vite，构建产物输出到 pages/cosyvoice/，不打进 zip）
├── core/
│   ├── webapi.py           # WebUI 后端 API（register_web_apis）
│   └── tts_engine.py
├── deploy/                 # CosyVoice 服务端：cosyvoice_api.py + 启动脚本 + 部署文档
├── cosyvoice/client.py
└── utils/audio.py
```
