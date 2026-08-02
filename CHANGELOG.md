# 更新日志

本文档记录插件各版本变更。版本号遵循语义化版本（MAJOR.MINOR.PATCH）。

## v1.4.3 (2026-08-02)

- 修复「无法连接 CosyVoice 服务」误报：原 `timeout=60` 为 httpx 标量超时，同时约束连接与读取，导致服务端在线但推理耗时 > 60s 时被 `ReadTimeout` 误判为连接失败。改为细粒度超时（`httpx.Timeout(timeout, connect=10.0)`），连接 10s 快速失败、读取用配置超时；并区分 `ConnectError`/`ConnectTimeout`（真连不上）与 `ReadTimeout`（服务在线但推理慢），日志不再误导。
- 修复 `prompt_text` 被污染：组装请求时 `prompt_text` 仅在非空时才作为表单字段发送，为空则完全不传（交由服务端从 `voices.json` 按文件名取），杜绝空串导致的 CosyVoice 默认 prepend `You are a helpful assistant.<|endofprompt|>`。新增 `_looks_polluted` 防御：参考文本含 `<|...|>` 标记、LLM 提示词片段或长度 > 300 字时视为污染并丢弃该字段，回退服务端 `voices.json`。`tts_text` 与 `prompt_text` 字段严格分离，绝不拼接 LLM system prompt / 角色设定 / 对话历史。

## v1.0.0 (2026-07-27)

- 初始版本。
- 接入本地自建 CosyVoice3 推理服务（官方 QwenAudio/CosyVoice 的 FastAPI 服务，默认端口 50000，模型 FunAudioLLM/Fun-CosyVoice3-0.5B-2512）。
- 多音色：以 `prompt_wav` 参考音频映射音色（音色名 → {prompt_wav, prompt_text}），支持 `/tts_voice` 切换默认音色。
- 三种触发方式：
  - 自动（`auto_tts`）。
  - 关键词触发（`trigger_keywords`，默认「语音 / 念出来 / 读出来」）。
  - LLM 函数调用工具 `text_to_speech` 与 `/tts` 指令。
- 发送模式 `send_mode`：`both`（文本+语音，默认）/ `voice_only`（仅语音）。
- 语音范围 `tts_scope`：`llm_only`（默认）/ `all_text`。
- 会话/用户白黑名单 `blocklist` / `allowlist`。
- 长文本按 `max_text_len` 切分合成后拼接。
- 硬约束：文本绝不因发语音而丢失上下文（原文 Plain 保留于结果链，LLM completion_text 单独存历史）。
- 提供 `PLAN.md` 记录完整方案与接口契约。
