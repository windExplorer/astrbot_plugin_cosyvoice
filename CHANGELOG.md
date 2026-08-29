# 更新日志

本文档记录插件各版本变更。版本号遵循语义化版本（MAJOR.MINOR.PATCH）。

## v2.1.26 (2026-08-29)

- fix: WebUI 音色编辑补上「副语言标记」开关——v2.1.25 只做了后端接口，前端没有对应控件，导致在 WebUI 里看不到也改不了。
  - `VoicesPanel.vue`：编辑/新增对话框新增该开关；音色卡片对关闭标记的音色显示「无标记」标签，便于一眼分辨哪些音色关了。
  - 列表加载、新建重置、编辑回填、保存提交四个环节全部带上 `markup`，避免回填时把开关丢失。
  - 重新构建前端产物（`npm run build` → `pages/cosyvoice/`）。
- 版本 v2.1.25 → v2.1.26。

## v2.1.25 (2026-08-29)

- feat: 新增**音色级**副语言标记开关 `voices.<音色>.markup`（默认 `true`）。
  - 置为 `false` 时该音色**完全不注入**任何副语言标记（换气与音效都不注入），只念纯文本；其余音色仍按全局配置正常注入。
  - 用于「某个音色/参考音频对标记敏感，插入 `[breath]`/`[laughter]` 后音色失真、读法怪异」的场景。
  - **优先级高于**全局 `auto_breath` / `auto_effect`——全局开关继续作用于其他音色，互不影响。
  - 实现：统一入口 `inject_markup()` 增加 `voice` 参数做拦截，5 处注入点（后台合成、翻译多段、不合并逐段、`/tts` 指令、LLM 工具 `text_to_speech`）全部接入；`TtsEngine._norm_voices` 解析该字段。
  - 三处入口均可配置：AstrBot 配置页（`metadata.yaml` 与 `_conf_schema.json` 的音色模板新增该项）、插件 WebUI 音色库（音色列表返回该字段，新增/编辑接口同步支持，避免编辑后被清掉）。
- 版本 v2.1.24 → v2.1.25。

## v2.1.24 (2026-08-29)

- fix: 配置/内容类失败不再触发熔断冷却，修复「每次重载插件后语音就发不出来」。
  - 根因：`TtsEngine.synthesize` 返回 `None` 涵盖四种原因（未配置音色、无有效可合成文本、分段返回空音频、合成异常），但调用方**一律当成服务端故障**并进入 30s 冷却。插件刚重载、配置尚未就绪时会被判成「未配置任何可用音色」→ 立刻进冷却 → 静默停发语音 30s，还会发出误导性的「语音服务器失联」提示。用户一重载就测试，正好撞在冷却窗口里。
  - 修复：`TtsEngine` 新增 `last_failure_kind`（`config` / `content` / `server`），全部 6 处冷却点改为**仅 `server` 才熔断**；配置/内容类失败只记录 WARNING 并回退文字，不再停发后续语音，也不会谎报服务器失联。
- 版本 v2.1.23 → v2.1.24。

## v2.1.23 (2026-08-29)

- fix: 修复「关掉 `/tts_on` 反而能出语音」——语音模式开启时两条发声通道被同时堵死。
  - 根因：`text_to_speech` 工具中，「已开启语音模式则拒绝工具」的判断排在**服务端冷却检查之前**（v2.1.19 引入）。冷却期间自动语音路径本就静默回退文字（`on_decorating_result` 直接 return，不发语音也不报错），此时工具再被一并拒绝，两条通道同时失效，用户彻底收不到语音；而 `/tts_off` 之后工具不再被拒绝，反而成了唯一可用通道。
  - 修复：冷却检查提到工具拒绝之前——**服务端健康时**才拒绝工具（避免多出孤立语音、把回复拆成多条），**冷却时**放行工具并统一给出失联提示。
  - 同步更新 LLM 工具 docstring 与 `skills/cosyvoice_voice_mode/SKILL.md`，写明该例外，避免模型在冷却时不敢调用工具。
- 版本 v2.1.22 → v2.1.23。

## v2.1.22 (2026-08-29)

- fix: 补齐「语音突然不转了」的可观测性（纯排查辅助，无发送行为变更）。
  - 冷却期原本**静默**回退文字、不打印任何日志，导致「为什么后面一直是文字」完全无法定位；现补充 WARNING，带剩余冷却秒数与触发来源说明。
  - 分段语音合成失败时，日志附带 `TtsEngine.last_failure` 真实原因（此前只有一句「跳过该段」，看不出到底为什么失败）。
- 版本 v2.1.21 → v2.1.22。

## v2.1.21 (2026-08-29)

- fix: 逐段模式下「有括号的分段导致语音重复」——括号后面的段落会重复念前面那段的语音。
  - 根因：文字段 `segs` 按**含括号**的原文切分，语音段 `vsegs` 按**剥离括号**的文本切分，两者段数不一致；而取值写的是 `vsegs[min(i, len(vsegs) - 1)]`，越界的段会一直复用最后一段语音（括号里有句末标点时必然触发）。
  - 根治：分段改在「剥离括号后的文本」`base_text` 上进行，括号内容由新增的 `bracket_text` 单独补发一条文字消息；文字段不再出现「括号没闭合的半截句」（如 `你好（哈哈哈。`）。
  - 保留段数保护：译文分段与文字分段数量一致时才逐段对齐取译文，否则退回用当前文字段，杜绝任何形式的跨段复用。
  - 合并模式行为不变（文字留在结果链、含括号），与 `skip_bracket_tts` 配置说明一致。
- 版本 v2.1.20 → v2.1.21。

## v2.1.20 (2026-08-29)

- fix: 修复 v2.1.19 引入的回归——日志显示「主动推送成功(event.send)」，但语音/文字用户实际收不到。
  - 根因：v2.1.19 把发送内容由裸 `list` 改为 `MessageChain` 后，`event.send` 不再抛 `'list' object has no attribute 'chain'`，于是走进 `event.send` 分支；而 `event.send` 只是把消息并入事件结果链，后台补发语音时事件早已响应完成，该链不会再被发送，且它**不抛异常**，于是日志一片「成功」、消息全部静默丢弃。
  - 反直觉点：v2.1.17 传裸 `list` 时 `event.send` 报错，反而正确降级到 `context.send_message`（真正调用平台投递），语音才能送达；v2.1.19 补上 `MessageChain` 等于把这个「保护性报错」修掉，反而堵死了可靠通道。
  - `_realtime_send` 通道优先级调整为：**`context.send_message`（首选，真正投递）→ `event.send`（兜底，失败如实告警）**，并把该坑写入 docstring 注意③，避免再次被调换回去。
- 版本 v2.1.19 → v2.1.20。

## v2.1.19 (2026-08-28)

- fix: 语音/文字发送报 `module 'astrbot.api.message_components' has no attribute 'MessageChain'`。
  - 根因：`MessageChain` 并不在 `astrbot.api.message_components` 中（该模块仅重导出组件类），此前打包进生产的用法在运行时取不到。
  - 改为兼容性导入：依次尝试 `astrbot.api.all` → `astrbot.core.message.message_event_result` → `message_components`，取到可用的 `MessageChain` 再构造消息链，语音与文字均可正常推送。
- 版本 v2.1.18 → v2.1.19。

## v2.1.18 (2026-08-28)

- fix: WebUI 试听失败不再只显示误导性的「无有效音频（可能是纯符号/无音色）」兜底文案，改为透传真实失败原因（`TtsEngine.last_failure`），根因一眼可见。
- fix: 「无有效可合成文本」日志从 DEBUG 提升为 WARNING 并附上原始文本与音色/语种，默认日志级别即可看到，方便排查试听与聊天语音失败。
- 版本 v2.1.17 → v2.1.18。

## v2.1.17 (2026-08-28)

- fix: 修复 LLM 工具 `text_to_speech` 合成成功但用户收不到语音的问题（翻译与合成本身正常，日志可见「合成 1/1 OK」）。
  - 根因：主动推送前调用了 `event.chain_result(records)`，该调用会**改写事件自身的结果链**；在 tool_loop 执行期间调用会与 agent runner 的结果处理冲突，导致语音被静默丢弃，且 `event.send()` 不抛异常，工具仍谎报「已发送给用户」。
  - `_realtime_send` 不再调用 `event.chain_result()`，直接传组件列表，事件自身结果保持不动；失联提示、失败兜底补发文字两处同样改掉。
  - 工具路径现在检查 `_realtime_send` 返回值：推送失败会如实返回「合成成功但推送失败」并发文字告知，不再谎报成功。
  - 推送成功日志从 DEBUG 提到 INFO，便于在日志里确认走了 `event.send` 还是 `context.send_message`。
- 版本 v2.1.16 → v2.1.17。

## v2.1.16 (2026-08-28)

- fix: 修复翻译场景「文字被拆成两条、语音重复发送」。翻译分支此前也有 `len(segs) > 1` 门槛：单段中文原文翻译后只设了 audio_text/display_text、没设 seg_items，非合并发送会落到非翻译分支，把 display_text（译文+换行+中文：原文）按换行二次切分。
  - 现在单段译文同样构建 seg_items，严格按流程发送：中文原文分段 → 逐段翻译 → 译文加副语言标签送 TTS → 发语音 → 发文本「译文 + 换行 + 中文：原文」（一条发出）→ 下一段。
- fix: 提高 `_is_bilingual` 双语判定门槛，避免中文回复被误判为双语而跳过翻译。
  - 含假名/谚文/西里尔/泰文等非拉丁外文字 → 仍直接判为双语（日/韩/俄等原文）；
  - 仅含拉丁字母/数字时，需 ≥15 个拉丁字母才算双语。此前中文回复里夹带 OK / WiFi / 数字（如「7 点」）就会被误判，导致跳过翻译、剥离中文后只把那几个字母念出来。
- 版本 v2.1.15 → v2.1.16。

## v2.1.15 (2026-08-28)

- fix: 修复配置面板「看不到配置项」的根因。`metadata.yaml` 的 `config` 段此前被截断为仅剩 3 个键（auto_breath/auto_effect/effect_intensity），导致 AstrBot 加载后只生成残缺的 `_conf_schema.json`，面板丢失 send_mode/voices/auto_tts 等约 30 项；用户也因此找不到情绪/换气开关。
  - 按完整的 `_conf_schema.json` 手工重建 `metadata.yaml` 的 `config` 段（约 33 项 + 3 项新增全部补全）。
  - 同步把 3 个新增键补回 `_conf_schema.json`，双保险：即使 AstrBot 不重新生成 schema，面板也能看到。
  - 注意：本仓库此前发布的 v2.1.14 压缩包内 metadata.yaml 即残缺版，请改用 v2.1.15 压缩包重新安装/重载。
- 版本 v2.1.14 → v2.1.15。

## v2.1.14 (2026-08-28)

- fix: 双语回复「中文被念出来」根因修复。原 `on_decorating` 仅在 `len(segs) > 1` 时才构建 `seg_items`，导致双语回复被 `split_text` 视为单段时落到非翻译分支，直接把含中文的 `display_text` 整段念出。
  - 双语场景**无条件构建 `seg_items`**（去掉单段门槛），稳定走「去中文外文」分支。
  - 新增 `_has_foreign`：语段去中文后仍含外文（假名/字母/数字/谚文）才发声；纯中文段（去中文后只剩标点/emoji）只发文字、不合成语音（避免把 `：，~ 🎵` 这类残留当噪音念出）。覆盖 both 多段、voice_only 多段。
  - 非翻译分支语音文本优先用 `audio_text`（译文/去中文外文），双保险不再念中文。
- 版本 v2.1.13 → v2.1.14。

## v2.1.13 (2026-08-28)

- feat: 语音标记注入（规范见 docs/cosyvoice_tts_markup_guide.md）。新增 `core/markup.py`：
  - `auto_breath`（默认开）：按语种标点集自动插换气标记，句末 `[breath]`、句中 `[quick_breath]`，连续标点只插一次、[quick_breath] 间隔 ≥8 字。
  - `auto_effect`（默认关）：关键词音效，按语种词典在句末追加 `[laughter]`/`[sigh]`/`[cough]`，密度受 `effect_intensity`（light/medium/strong）控制，并排除「可笑的」等形容误触发。
  - 标记白名单：`_strip_brackets` 不再误删 `[laughter]` 等朗读标记；所有标记只进合成文本，绝不进显示文字 / prompt_text。
  - 接入三处合成路径：后台补发（合并/多段/单行 both）、`text_to_speech` 工具、`/tts` 指令。

## v2.1.12 (2026-08-28)

- fix: 双语回复换行拆成多段时，纯中文段（无外文可读）不再被单独念成中文语音——该段只发文字、不发声，文字展示仍按 `translate_display_mode` 保留（bilingual 时显示双语原文）。去掉 v2.1.11 中纯中文段 `or seg` 的退化逻辑。
- 无需额外配置：回复同时含外文与中文即自动「看中文、听外文」。

## v2.1.11 (2026-08-28)

- fix: 双语（外文原文 + 中文翻译）回复用中文音色时，语音不再把中文翻译念出来。检测到双语（同时含汉字与外语文字）时，语音合成文本统一取「去中文后的外文」朗读，文字展示仍按 `translate_display_mode` 保留双语原文（看中文、听外文）。纯中文 / 纯外文回复行为不变。

## v2.1.10 (2026-08-28)

- fix: `text_to_speech` 工具内使用 `self._effective_send_mode(event, cfg)` 但 `cfg` 未定义，触发 `NameError`，被兜底 except 误判为「语音合成失败」。已在函数开头 `self._refresh_cfg()` 后补 `cfg = self.config`。修复后艾特「来段语音」、`/tts_on` 等走该工具的语音可正常发出。

## v2.1.9 (2026-08-28)

- fix: 单行（无换行/句末标点分段）语音路径也遵循 `text_after_voice`——true 时先发语音、再补发整条文字（先听后读）；false 恢复先文字后语音。v2.1.7 漏改此分支，导致短文本仍先文字后语音。

## v2.1.8 (2026-08-28)

- fix: LLM 工具 `text_to_speech` 与 `/tts` 指令现在遵循 `tts_type`/`send_mode`——both（语音+文字）时在语音之外补发文字，voice_only 仍只发语音。此前两条路径被硬编码为只发语音，导致配置 tts_type=1 也不发文字（与关键词「语音」无关）。文字保留原始内容（含括号），与自动语音路径一致。

## v2.1.7 (2026-08-28)

- feat: 新增「先发语音再发文字」配置项 `text_after_voice`（bool，默认 true）。不合并 both（逐段发送）模式下，每段先发语音、再发对应文字（先听后读）；设为 false 恢复先文字后语音（原行为）。生效于翻译多段与未走翻译多段两个分支；合并 both / voice_only 不受影响。

## v2.1.6 (2026-08-28)

- feat: WebUI 会话列表显示 QQ号/群号+昵称，并支持编辑配置。后端在 on_decorating_result 中 best-effort 记录会话昵称（事件 sender_name，持久化 data/tts_nicknames.json）；_list_sessions 新增 platform/group_id/user_id/label/nickname 字段（由 unified_msg_origin 解析群号与 QQ号）。前端会话列优先显示昵称、副行显示群号/QQ号；新增「编辑」弹窗，可配置语音开关、发送方式（默认/both/voice_only）、音色（下拉取自 voices 接口）。修复此前仅「删除」按钮、无法配置的问题。

## v2.1.5 (2026-08-28)

- 修复：不合并 both 模式且未走「翻译多段」分支时（没开翻译 / original 模式 / 单行翻译），有换行的文字此前整块一次性发出、仅语音分段；现文字也按「换行+句末标点」逐段发送，与语音逐段对齐。

## v2.1.4 (2026-08-28)

- 修复：`skip_bracket_tts` 仅「语音不朗读括号内容」，文字照常显示完整原文（含括号）；移除原先在 voice_only / 不合并 both 模式下单独补发括号文字的逻辑，避免括号内容既在完整消息里出现、又单独发一条造成重复。
- `both` 模式原文标注「原文：」改为「中文：」，更直接表明外语译文对应的中文文本（表示其他语言已译为中文），阅读更自然。

## v2.1.3 (2026-08-28)

- 修复：多段翻译分支里未真正翻译的段（原文语种 = 音色语种）也曾被强制挂「原文：xxx」前缀，导致「不需要翻译的文本也出现原文」。现仅在确有段真正被翻译时才走译文排版；未翻译的段保持纯原文展示、不加「原文：」前缀。逐段发送分支同步修正。

## v2.1.2 (2026-08-28)

- 自动翻译「both」模式排版调整：译文与原文之间改为直接换行（不再空一行），即「译文\n中文：xxx」。分段策略改为在原文侧按「换行 + 句末标点（。！？.!?）」切分，每段原文单独调用翻译接口（每段是完整语义单元，翻译更准）；不合并发送模式下，文字随语音逐段发送、且原文/译文一一对应（分段在原文侧，跨语言句对齐可靠）。单行无多段时退回整块翻译，行为不变。

## v2.1.1 (2026-08-28)

- 调整「翻译后文字展示方式」`both` 模式的聊天气泡排版：改为「译文在上、空一行、原文：xxx」——优先展示语音实际朗读的译文，不再用括号括注（避免和文本自带括号混淆）。`original`（只原文）、`translated`（只译文）模式不变，语音始终念译文。

## v2.1.0 (2026-08-28)

- 新增「翻译后文字展示方式」配置项 `translate_display_mode`：自动翻译（外语音色）场景下，聊天气泡文字可在三种模式间切换——`both`（默认，`原文（译文）`，如「你好（こんにちは）」）、`original`（只显示原文）、`translated`（只显示译文）。无论哪种模式，语音始终念译文（原文用外语音色念不通顺）。未配置翻译适配器时不生效。

## v2.0.7 (2026-08-28)

- 修复 v2.0.6 引入的 `NameError: name '_BRACKET_RE' is not defined`：`_BRACKET_RE` 是类属性，但 `_strip_brackets`/`_extract_brackets` 方法体内裸引用（Python 名字查找不会进类命名空间），`skip_bracket_tts` 开启且回复含括号时仍在结果装饰阶段报错、消息发不出。本次改为 `self._BRACKET_RE` 引用，括号剥离与单独补发文字恢复正常。
- 翻译合成发送文本改为「原文(译文)」格式：自动翻译场景下，发送给用户看的文字从原文改为 `原文（译文）`（原文在前、译文括注），语音仍照常念译文；翻译命中时把已翻译文本直接传给合成（`pre_translated`），避免引擎对译文二次翻译造成多余 API 调用与回译失真。翻译未开启或译文未命中时行为不变。覆盖合并 both / 不合并 both / voice_only 三种发送模式。

## v2.0.6 (2026-08-28)

- 修复 v2.0.5 修复不彻底导致的运行时报错：`on_decorating_result` 中 `self._extract_brackets(...)` 仍抛 `TypeError: _extract_brackets() takes 1 positional argument but 2 were given`（`skip_bracket_tts` 开启且回复含括号内容时，LLM 回复在结果装饰阶段崩掉、消息发不出）。根因是 `_extract_brackets`/`_strip_brackets` 方法定义漏了 `self` 参数：本次统一修正为实例方法（定义补 `self`，类内调用补 `self.`），括号内容单独补发文字恢复正常。

## v2.0.5 (2026-08-28)

- 修复运行时崩溃 `NameError: name '_extract_brackets' is not defined`：`on_decorating_result` 在 `skip_bracket_tts` 开启时会把括号内容单独发送为文字，但误将实例方法当作全局函数调用，导致 LLM 回复在「结果装饰/发送」阶段整段抛异常、消息发不出来。改为 `self._extract_brackets(...)` 调用修复。

## v2.0.4 (2026-08-28)

- 修复 WebUI 会话页表格字段全部空白：前后端会话字段契约对齐（后端 /sessions 改为返回 id/user/on/mode/voice/prob，前端表格列相应调整）；并新增 sessions/delete、sessions/clear 端点，修复「删除 / 清空全部」按钮因端点不匹配而 404 的问题。

## v2.0.3 (2026-08-28)

- 翻译合成 WebUI 文案修正：面板总说明、目标语种字段说明、测试提示统一改为「按音色语种翻译」逻辑（目标语种取所选音色的 language，音色未配置语种时回落全局目标语种），消除与 v2.0.2 实际行为不一致的旧描述。

## v2.0.2 (2026-08-28)

- 修复 WebUI 首页概览「连接状态 / 基础信息 / 自动 TTS 配置 / 最近事件」全部为空且误报「未连接」：前后端概览字段契约对齐（`client_ready`/`base_url`/`auto_tts_reply`/`recent_events`/`voices_count`/`servers_count` 等），并新增进程内「最近事件」环形缓冲，让首页展示真实连接状态与近期事件。
- 翻译合成支持「按音色语种翻译」：目标语种从全局固定值改为取所选音色的 `language` 字段，实现「中文文本 + 外语音色 → 翻成该语种」；合并模式与逐段（不合并）模式两条路径统一生效（此前逐段发送漏翻已修复）。
- 新增「语种代码映射」：翻译配置支持简码 → 接口码映射（如 `zh→zh-CN`、`ja→ja-JP`），作用于 `{source}`/`{target}` 占位符，解决插件输出语种码与翻译接口要求不一致的问题；WebUI「翻译合成」面板新增「语种代码映射」编辑区。

## v2.0.1 (2026-08-28)

- 修复插件初始化顺序：`translator` 与 `data_dir` 在 `TtsEngine` 创建前未就绪导致的运行时报错。
- 修复 WebUI 注入：补全 `provide('bridge', bridge)`，修复各面板 `inject('bridge')` 取到 undefined（`apiGet` 调用报错）。
- 音色管理面板增强：
  - 顶部「音色总数 / 可见 / 已隐藏」可点击筛选（高亮当前项）；
  - 未设置语种的音色默认归为「中文」；
  - 编辑音色时语种下拉展示中文名称（不再显示代码）；
  - 新增「设为默认」按钮，调用 `voices/default` 即时生效；
  - 试听区新增「试听（默认音色）」确认按钮，并兼容 bridge 返回的字符串 URL / Blob 两种形态。

## v2.0.0 (2026-08-28)

- **翻译合成**：合成前自动将非目标语种的文本翻译为目标语种（默认汉语），自动 TTS / 指令 / WebUI 试听**全局生效**，可整体开关。语种判定走本地 Unicode 脚本检测（零 API 消耗，不细分拉丁系内部）。
  - 通用翻译适配器，配置项不缺胳膊少腿：URL、请求方法(GET/POST)、API Key、认证头名(默认 `Authorization`)、认证 scheme(默认 `Bearer`)、额外请求头(key/value 列表)、请求参数模板(支持 `{text}`/`{source}`/`{target}` 占位符)、响应解析路径(点号+数组下标，如 `data.trans_result[0].dst`)。
  - 可设置「需翻译语种」白名单（留空 = 除目标语种外全部）；译文带缓存；任意异常兜底回退原文，绝不影响合成主流程。
  - 翻译配置保存在插件自有 `data/translate_config.json`，WebUI「翻译合成」面板即可编辑并热生效；面板含「测试」显示检测语种 / 是否翻译 / 译文。README 已补充完整配置说明。
- **括号内容不朗读**（新增开关 `skip_bracket_tts`，默认开）：正文中被括号包裹的文字（支持中文（）/英文()/方括号[]）不进入语音合成。
  - 语音+文字（both）合并模式：括号内容随文字正常显示在聊天记录里，仅语音不念。
  - 仅语音（voice_only）或不合并 both 模式：文字不在聊天记录里，括号内容会单独作为一条文字消息发给用户，保证看得见。
  - `/tts` 指令同样遵循「括号不念」（仅剥离、不额外补发文字）。
- **音色多语言分类**：新增 `language` 字段（zh/en/ja/ko/ru/th/ar/hi…），WebUI 音色管理按语种分组展示与筛选；AstrBot 配置面板音色模板也新增「语种」字段。
- **WebUI 全面重做**：统一亮/暗设计 token 与卡片化风格、胶囊化标签导航；音色管理改为分组卡片 + 内联试听 + 批量显示/隐藏/删除；新增「翻译合成」面板；概览/会话/配置面板同步美化。
- **修复打包脚本 `pack.py`**：归档路径改用正斜杠，解决 Windows 打包后到 Linux 解压变成「带斜杠文件」的问题。

## v1.17.2 (2026-08-10)

- 修复「语音把 LLM 函数调用念出来了」：AstrBot 的 `tool_loop_agent_runner` 在工具循环中，最终响应的 `completion_text` 会混入工具调用序列化（如 `comfyui_draw{"name":"comfyui_draw","args":{...}}` + 完整 prompt JSON）。当结果链文本为空时，插件回退用 `_last_llm` 合成，就把这些工具调用 JSON 当正文念了（表现为 373 字里有 300 多字是工具调用内容）。
  - 新增 `clean_tool_calls`：识别「标识符 + `{` + `"name"` 键」的工具调用形态，用大括号配对**完整剔除**整个 JSON 块（避免被分段器按标点切碎残留）。实测不误伤 `json.dumps({"name":...})`、普通 `{"name":...}` 讨论文本。
  - 新增 `clean_tts_text`：媒体占位符 + 工具调用综合净化；`is_speakable` 对净化后为空的纯工具调用文本判为不可朗读。
  - `on_llm_response` 检测 `LLMResponse.tool_calls/tools`：纯工具调用轮（无正文）**不覆盖 `_last_llm`**，保留上一轮干净文本，避免回退念出垃圾。
  - 净化应用于：`_last_llm` 写入、结果链文本抽取与回退、`text_to_speech` 工具参数。

## v1.17.1 (2026-08-10)

- 修复「带图片的消息出现 `<pc_history_media images="1" />` 占位文本」：该标签是平台/框架对**历史消息中图片媒体**的序列化占位符，此前会被插件当作普通正文——被朗读、或在 both 模式下显示为乱码。
  - 新增 `clean_media_placeholders`：剔除自闭合标签 `<... />` 及含 `media/image/img/record` 关键词的标签。
  - 全链路净化：结果链文本抽取、LLM 原文回退、LLM 工具 `text_to_speech` 参数、关键词/抑制词判定（`on_llm_response` 的 `message_str`）。
  - `is_speakable` 对「净化后为空」的文本（纯图片占位）判定为不可朗读，直接跳过语音。
  - 说明：插件只保证不把占位符当文字/语音输出；图片能否正常渲染取决于平台适配器对 Image 组件的支持。

## v1.17.0 (2026-08-09)

- 手动指令 / LLM 工具触发的语音**只发语音**：`/tts`、`text_to_speech` 工具不再随 `send_mode`/`tts_type` 附带文字（文字仍由 LLM 结果链与会话历史保留，不丢上下文）。
- 新增 `/tts0` 指令：整段一次合成、一口气念完，只发一条语音（内部仍切段拼接防超长，输出不分条）。
- 新增 `/tts1` 指令：按换行符分段，每段一条语音逐条发送，同样只发语音。
- `/tts_help` 补充 `/tts0`、`/tts1` 用法。
- 说明：音色列表在配置面板的标题显示依赖 `display_item`（已配置指向 `name`），该能力 **AstrBot v4.10.4+** 才支持；旧版本会显示模板名「添加音色」，升级 AstrBot 即可。

## v1.16.2 (2026-08-09)

- 修复「语音+文字」模式只收到语音、收不到文字：v1.16.1 用「文字+语音组合消息」逐段推送，但部分平台对**主动推送的组合消息**可能只展示语音、静默丢弃文字，且原 `_realtime_send` 吞掉所有异常、无日志可查。
  - 改为**每段先发一条文字、再发一条语音**（两条单组件消息，平台兼容性最好），文字必然显示，仍满足「文字按语音分段走」。
  - `_realtime_send` 现在返回发送结果，并记录实际发送的组件构成（DEBUG 级）；文字/语音任一发送失败都会打出 WARNING 定位。
  - 关键决策点新增日志：后台合成开始（send_mode/merge/text_in_chain）、进入冷却、补发完整文字，便于确认实际生效的发送方式。

## v1.16.1 (2026-08-09)

- 优化「语音+文字」模式下文本的发送时机：文字**跟随语音分段发送**，不再一次性把全文刷给用户。
  - 不合并模式（`segment_merge=false`）+ both：原文从结果链移除，后台逐段合成后每段发「该段文字 + 该段语音」，边说边出文字；单段合成失败则该段文字照发（补发），保证文字不缺段；全部失败则回退补发完整文字。
  - 合并模式（`segment_merge=true`）+ both：保持「一次性全文 + 整条语音」（语音本身只有一条，天然对应）。
  - voice_only / 合并模式行为不变。
  - 实现：`TtsEngine` 新增 `iter_segment_items`（yield 段文字+wav，失败段 wav 为 None 仍给文字），`iter_segment_wavs` 改为基于它（兼容旧调用）；`main.py` 引入 `text_in_chain` 标志统一「文字是否已从结果链移除」的判定与失败补发逻辑。
  - 说明：文字均仍由 LLM `completion_text` 存入会话历史，AI 下一轮不丢上下文。

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
