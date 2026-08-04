# 更新日志

本文档记录插件各版本变更。版本号遵循语义化版本（MAJOR.MINOR.PATCH）。

## v1.5.0 (2026-08-04)

- 解除语音合成对消息管线的阻塞：原 `on_decorating_result` 钩子内同步 `await` 合成，会占用事件循环、卡住同会话/全局的其他事件（表现为「语音卡半天、后续消息传不出、日志卡住」）。
  - `on_decorating_result` 不再 `await` 合成：文字立即交给 AstrBot 发出，语音合成放入 `asyncio` 后台任务，合成完成后通过 `event.send` 主动补发。
  - 新增 `_background_speak`：后台逐段/合并合成并补发语音，失败仅记日志、不影响已发出的文字；成功后才登记幂等锁与解除服务端冷却。
  - `voice_only` 模式在钩子内同步（轻量内存操作）把原文从结果链移除，仅后台发语音；文字仍由 LLM completion_text 存入会话历史，不丢上下文。
  - 「等一会儿才有语音」可接受，但其他事件不再被卡住。

## v1.6.0 (2026-08-04)

- 新增 TTS 服务端并发限流，保护性能较弱的推理服务：多用户同时触发语音时不再把 CosyVoice（GPU 推理）服务端打爆。
  - `TtsEngine` 增加全局 `asyncio.Semaphore`，每段合成真正请求服务端时占用、完成后释放；并发上限由新增配置项 `tts_concurrency` 控制（默认 1=完全串行最稳，服务端够强可调 2~3）。
  - 等待信号量期间是协程挂起，不占用 AstrBot 事件循环，因此**不会卡住其他消息**；只是该用户的语音会按队列顺延到达（"等一会儿"可接受）。
  - 配合服务端自身全局锁，可彻底避免「多请求排队 + 读超时 + 重试」的雪崩。

## v1.7.0 (2026-08-04)

- 对接队列版服务端（`cosyvoice_api_queue.py`，端口 50002）：多人并发不再丢语音、不雪崩。
  - 客户端读超时 `timeout` 默认 60→**150s**，且不再压成 30s 上限：队列版服务端单次推理可能 >30s + 入队等待 30s，压太小会自己先超时断连、制造重试验崩。
  - 新增对服务端 **429 / 503 / 504** 的指数退避重试（0.5s→1s→2s，最多 3 次）：队满/模型未加载/推理超时按文档语义退避重发，而非当失败丢弃（否则高峰期直接丢语音）。其余状态码（400/500）仍立即抛。
  - 默认 `base_url` 改为 `http://127.0.0.1:50002`，直接对接队列版；仍用原版非队列服务则改回 50000（接口完全一致）。
  - 说明：服务端已用有界队列 + 单 worker 限流，插件侧的 `tts_concurrency`（默认 1）保持串行即可；多 AstrBot 实例共享同一服务端时，服务端 429 退避重试作为跨进程兜底仍然生效。
  - 接口字段、PCM 格式、采样率自动获取等逻辑不变，无需改动对接代码。

## v1.8.0 (2026-08-04)

- 新增 `voice_only` 模式失败兜底：语音彻底失败时退化为补发文字，避免前端静默。
  - 背景：`voice_only` 会在钩子里把原文从结果链移除、只后台发语音；若语音合成失败（服务端失联/冷却/重试耗尽），前端会收不到任何东西。
  - 新增 `_fallback_text`：后台语音失败时，仅当 `send_mode == "voice_only"` 才用 `context.send_message` 把文字主动补发（both 模式文字已在结果链正常发出，无需补）。覆盖合并失败、逐段 0 段、CosyVoiceServerError 冷却、通用异常所有失败分支。
  - both 模式行为不变（文字照常发，语音失败仅记日志）。

## v1.8.1 (2026-08-04)

- 优化「语音服务器失联」提示样式：前面加换行空格与正文分隔、整句用括号包住，避免与其他文本混在一起（常量 `SERVER_DOWN_TIP`，结果链/指令/工具 6 处统一生效）。

## v1.9.0 (2026-08-04)

- 重试与冷却改为可配置，避免退避太久、卡着连文字也不发：
  - 新增 `tts_max_retry`（默认 **0**=不重试）：一次失败直接回退文字 + 进冷却，不再「下条重试」。需要自动重试可设 1~3。
  - 新增 `tts_retry_backoff`（默认 0.5s）：重试退避基数，实际等待 = base × 2^次数。
  - 新增 `tts_cooldown_sec`（默认 30s，替代硬编码 30）：服务端失联后多久内**完全不打 TTS 服务端**、直接回退文字。冷却期内后续所有请求（含队列中积压的）都走回退，不再卡着。
  - `timeout`（v1.7.0 已加，默认 150s）即为单次请求超时，可在配置里调小。
  - 失败即进冷却（任意异常，不只 CosyVoiceServerError）：新增 `_enter_cooldown` 统一「进冷却 + 发一次性失联提示 + voice_only 回退文字」；both 模式文字已在结果链正常发。冷却期内 on_decorating_result 对 voice_only 也补发文字，避免前端静默。
  - 顺手修了旧 bug：原 `_trip_breaker` 在后台任务里往空 list 追加提示导致失联提示永不显示，现改由 `_enter_cooldown` 用 `context.send_message` 主动发送。

## v1.4.4 (2026-08-02)

- 修复「卡半天报语音服务器失联、但语音最终却送达」的问题：根因是 AstrBot 框架对同一条消息重复触发 `on_decorating_result`，导致重复合成、给 CosyVoice 服务端加压，第二次请求在传输中途被服务端断开（httpx 通用 RequestError，错误信息为空），被误报为「服务器失联」。
  - 新增 per-message 合成幂等锁：同一会话同一条文本只合成一次，第二次触发直接跳过，不再重复打服务端。
  - `cosyvoice/client.py`：连接中途断开（服务实际可达）不再转 `CosyVoiceServerError`、不再误提示「服务器失联」，仅记 WARNING；真正的连接失败（ConnectError/ConnectTimeout）仍正常提示。
  - `update_voices`：音色配置内容未变时跳过重建与「已加载 N 个音色」日志，消除重复触发的日志噪音。

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
