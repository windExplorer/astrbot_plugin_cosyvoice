# 更新日志

本文档记录插件各版本变更。版本号遵循语义化版本（MAJOR.MINOR.PATCH）。

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
