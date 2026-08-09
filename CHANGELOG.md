# 更新日志

本文档记录插件各版本变更。版本号遵循语义化版本（MAJOR.MINOR.PATCH）。

## v1.16.0 (2026-08-09)

- 新增音色隐藏功能：音色配置可勾选「隐藏」，勾选后该音色**不出现在音色列表中**：
  - `/tts_voice`（不带参数查看可选音色）与 LLM 的 `list_voices` 均不展示隐藏音色；
  - 但知道名字仍可手动使用：`/tts_voice 名字`、LLM `set_voice 名字`、作为默认/会话音色均不受限；
  - `/tts_export` 导出配置仍包含隐藏音色（管理用途，备份不丢）。
  - 实现：`TtsEngine.list_voices(include_hidden=False)` 默认过滤隐藏；`_norm_voices` 解析 `hidden` 字段（旧配置无该字段视为不隐藏，兼容）。

## v1.15.0 (2026-08-09)

- 新增会话级发送方式 `/tts_type`：每个聊天可单独设置「语音发送方式」，不再受全局配置限制。
  - `/tts_type -1` 跟随全局（默认，恢复全局 `send_mode`）；`/tts_type 0` 仅语音；`/tts_type 1` 语音+文字。
  - 按会话持久记忆（`tts_sendmodes.json`），重启不丢；只影响当前聊天，其他会话不受影响。
  - 生效点：后台自动语音、`/tts` 指令、`text_to_speech` 工具三条路径统一走 `_effective_send_mode`（会话优先、否则全局）。
- 新增 `/tts_help` 指令：展示常用指令用法（`/tts`、`/tts_on|off`、`/tts_type`、`/tts_voice`、`/tts_status`、关键词触发），不展示 `/tts_export` 等管理员操作。
- `/tts_status` 增加「发送方式」行：显示本聊天是「跟随全局 / 仅语音 / 语音+文字」以及全局当前值。
- 顺手修复 `on_decorating_result` 冷却期分支在 `send_mode` 赋值前引用导致的潜在 `NameError`（该分支此前仅在服务端冷却期内可能触发）。

## v1.14.2 (2026-08-07)

- 修复「语音服务器失联/繁忙」提示消息开头出现空白行：提示均为独立发送的消息（`_enter_cooldown` 主动推送、`/tts` 指令结果、`text_to_speech` 工具补发），前面并无正文，原有 `\n\n` 前导换行只剩副作用。去掉 `SERVER_DOWN_TIP` / `SERVER_BUSY_TIP` 的前导换行，消息从 🎙️ 直接开始。

## v1.14.1 (2026-08-07)

- 优化「分段 + 队列」的交互：同一轮回复（无论合并/不合并模式）**只对第一段做「排队过长」判定**（探路）：
  - 首段未超阈值 → 后续段跳过 `X-Queue-Position` 判定、照常合成，杜绝「前半段语音已发出、后半段因排队被弃」的半截现象；
  - 首段即超阈值 → 整轮回退文字 + 繁忙提示，干净利落。
  - 同时消除长回复误杀：此前逐段独立判定时，后续段入队位置天然包含自己前面段占的位（如第 5 段入队时队列里已有自己前 4 段 + 别人的任务），固定阈值会把长回复误判为「繁忙」而整轮回退。
  - 实现：`cosyvoice/client.py` 的 `synthesize` 新增 `check_queue` 参数（默认 True，仅影响多段回复的后续段）；`core/tts_engine.py` 合并/逐段两条合成路径均传 `check_queue=(i == 1)`，首段 True、后续段 False。

## v1.14.0 (2026-08-07)

- 适配中转站/队列版服务端：排队过长时「提前放弃」并回退文字，不再傻等超时。
  - 新增配置项 `tts_queue_max_position`（默认 **8**，`0`=不限制）：服务端返回 `X-Queue-Position` 响应头且排队位置 ≥ 阈值时，判定「语音服务器繁忙」，立即放弃本次合成（抛 `QueueFullError`）、回退文字并进入冷却，而不是一直等到 ReadTimeout/重试验崩。
  - **零侵入兼容**：仅当服务端带 `X-Queue-Position` 头时该逻辑生效；直连 CosyVoice（无该头）完全走原有逻辑，行为不变。
  - `cosyvoice/client.py`：新增 `QueueFullError`；`synthesize` 在读响应头时校验排队位置。
  - `cosyvoice/router.py`：排队阈值下发到各节点；繁忙节点**不算故障、不隔离**，仅切换下一节点（全部繁忙才回退）。
  - `core/tts_engine.py`：`QueueFullError` 与 `CosyVoiceServerError` 一样向上抛，不被「单段失败跳过/返回 None」吞掉。
  - `main.py`：三条合成路径（后台自动语音 `/tts` 指令、`text_to_speech` 工具）均捕获 `QueueFullError`，新增繁忙提示 `SERVER_BUSY_TIP`（「语音服务器正忙…稍后再试」），与失联提示区分文案。

## v1.13.0 (2026-08-05)

- 优化多服务器配置：标题缩短（`base_url`→「单机服务地址」，`servers`→「服务端列表」），避免面板截断。
- 新增「设为默认」节点：在「服务端列表」里对某台勾选 `default` 即可优先使用，**无需再手输一遍 url**。
  - `CosyVoiceRouter._pick_index` 优先选择可用默认节点（有多个默认则随机取一），否则按权重分流；默认节点冷却/失败时自动切回权重分流。
  - 兼容旧配置：`servers` 项没有 `default` 字段时按权重分流（行为不变）。

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

## v1.9.1 (2026-08-04)

- 修复配置 schema 类型错误：AstrBot 不支持 `number` 类型（报错「不受支持的配置类型 number」），将 `tts_retry_backoff`、`tts_cooldown_sec` 改为 `float`。（功能同 v1.9.0）

## v1.9.2 (2026-08-04)

- 修复 `CosyVoiceClient.__init__() got an unexpected keyword argument 'retry_backoff'`：
  之前一次替换只改了函数体赋值、漏把 `retry_backoff` 加进 `__init__` 参数列表，导致调用方传参即报错。现补全参数列表，并把 `max_retry` 默认值归正为 0（匹配"默认不重试"设计）。（功能同 v1.9.0/1.9.1）

## v1.10.0 (2026-08-05)

- 新增「优先按换行分段」配置 `split_by_newline`（默认 true，QQ 多行消息用）：
  - 一个或连续多个换行符都归一为硬分段边界，`re.split(r"\n+")` 切开、逐行 strip、去空。
  - 每行独立走标点窗口分段 + 行内短段合并；**不跨行合并**（多行通常语义独立，避免两行不同意思糊成一段）。
  - 避免把换行符念成噪音/静音块，也避免超长行一次性合成。
  - 无实际换行时退化为整段窗口分段（与原行为一致）；配置关闭则完全走旧逻辑。
  - 已用临时脚本验证：连续换行、短首行、空行夹杂、单行无换行均符合预期。

## v1.11.0 (2026-08-05)

- 修复 LLM 工具 `text_to_speech` 丢段：AstrBot 的 `llm_tool` runner 期望工具返回**字符串**作为 tool result 喂回给 LLM，而该工具原来用 `yield event.chain_result(...)` 逐段发消息组件，导致中间段被吞、只有最后一段发出，且日志提示「text_to_speech 没有返回值」。
  - 改为与其它工具一致的写法：逐段合成后用 `_realtime_send`（event.send → context.send_message）**主动发送**每一段语音（与 on_decorating_result 后台补发同一套可靠机制），不再 yield 消息组件；最终 `return` 一个简短字符串给 LLM 作为 tool result。
  - 失败/冷却分支同样改为主动发提示文字 + 返回字符串，LLM 能感知结果。
  - `/tts` 手动指令（command）的 yield 逐段发送机制不受影响，未改动。

## v1.11.1 (2026-08-05)

- 修复「开了 tts_on 仍有文字没转语音直接发出」：`_should_tts` 在默认 `llm_only` 下强依赖 `is_llm` 标志（由 `on_llm_response` 钩子设置），若该钩子因 AstrBot 版本/工具循环 final response 路径差异未触发，`is_llm` 缺失 → `tts_on` 开着也直接发文字。现改为：用户显式 `/tts_on` 开启会话后（`session_on`）不再依赖 `is_llm`，视为已授权「本聊天都念」。
- 加固逐段语音发送，避免「只念了一段、中间几段没发」：
  - `_realtime_send` 全量捕获异常（含 `event.chain_result` 构造、`event.send`、`context.send_message`），任何失败只记日志不外抛——不中断整个逐段循环。
  - `_background_speak` 逐段循环内再包一层 try：单段发送失败只跳过该段，后续段继续发出。
- `/tts` 手动指令与 LLM 工具不受影响。

## v1.12.0 (2026-08-05)

- 修复 `tts_scope=llm_only` 下其他插件返回的固定文案也被转语音的问题：根因是同会话内上一轮大模型原文残留在 `_last_llm`，下一轮非大模型消息被误判为 LLM 回复。修复方式：`on_llm_response` 新增 `llm_this_round` 本轮标记（随本轮结束自动清理），`_should_tts` 以本轮标记为判定主依据；`on_decorating_result` 各路径（合成/早退/不触发）结束后清理 `_last_llm`/`_last_user_msg` 残留，杜绝跨轮污染。
- 支持多台 CosyVoice 推理服务同时启用并按权重分流（多机负载均衡）。
  - 新增配置项 `servers`（服务端列表）：每项 `url`（地址）/ `enabled`（是否启用）/ `weight`（分流权重），`template_list` 形式在插件配置面板逐条添加。
  - 新增 `cosyvoice/router.py`：`CosyVoiceRouter` 内部管理多个 `CosyVoiceClient`，按权重随机分流；单节点连续失败 3 次自动临时隔离 30 秒（其余节点照常服务），到期自动恢复探测；全部节点不可用时回退到 `base_url` 单机模式。
  - 兼容旧配置：`base_url` 保留为单机模式，配置了 `servers` 后以 `servers` 为准；配置热更新（`_refresh_servers`）仅当 `servers`/`base_url` 变化时才重建节点，避免每条消息反复重建连接池。
  - `TtsEngine` 无需改动（`router` 暴露与单客户端一致的 `synthesize`/`sample_rate`/`cache_dir` 接口）。

## v1.11.2 (2026-08-05)

- 修复 v1.11.1 引入的语义回归：`tts_scope=llm_only`（只转大模型语音）被误放宽成「`tts_on` 会话开启就全转」，导致指令输出等非大模型文本也被转语音。
  - `llm_only` 分支恢复严格语义：只转大模型回复（`is_llm` 标志）。
  - `is_llm` 缺失（`on_llm_response` 钩子未触发，如工具循环 final response 路径）时，用 `_last_llm`（该钩子每次都会写入本轮模型原文）兜底判定，既保住 `tts_on` 下大模型回复能转语音，又不误转非大模型消息。

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
