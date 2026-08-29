# CosyVoice3 合成文本控制标记使用教程（插件端参考）

本文档面向调用 `cosyvoice_api` 的插件（如 astrbot_plugin_cosyvoice）。

CosyVoice3 支持两类「在文本里穿插控制」的能力，本文档只覆盖**第一类（音效/副语言标记）**和**第二类（情绪/语气/方言指令前缀）**。语速等参数控制不在本文档范围内。

服务端接口见 `cosyvoice_api.py`（FastAPI）。可用端点：
- `POST /inference_zero_shot`：表单 `tts_text`、`prompt_text`、`prompt_wav_path`（或 `prompt_wav` 上传）
- `POST /inference_instruct2`：表单 `tts_text`、`instruct_text`、`prompt_wav_path`（或 `prompt_wav` 上传）

---

## 速查表：两类标记分别写进哪个字段

| 标记类型 | 例子 | 写进哪个字段 |
|---|---|---|
| 音效标记（第一类） | `[laughter]` `[breath]` `<strong>` | **`tts_text` 正文**，可穿插 ✅ |
| 情绪/方言指令（第二类） | `请非常开心地说一句话。<|endofprompt|>` | **`prompt_text`**（方式 A / zero_shot）或 **`instruct_text`**（方式 B / instruct2）❌ 禁止写进 `tts_text` |

> 本文档默认采用 **方式 A（zero_shot）**：第二类指令拼在 `prompt_text` 最前面，格式 `{指令}<|endofprompt|>{参考音频原文}`。

---

## 第一类：音效 / 副语言标记（直接写进 `tts_text`）

这些标记直接拼进你要合成的 **`tts_text`** 正文里，模型会把它当成「动作/情绪」而不是字面文字念出来。

> 原理：这些标记已在 tokenizer 的 `additional_special_tokens` 注册（`cosyvoice/tokenizer/tokenizer.py`），且模型配置 `allowed_special: 'all'`，会被整体识别为一个特殊 token，不会读成文字。

### 可用标记一览

| 标记 | 效果 | 示例 |
|---|---|---|
| `[laughter]` | 笑声 | 他突然`[laughter]`笑了起来 |
| `[breath]` | 换气（≈停顿效果） | 我那时候`[breath]`还在想 |
| `[quick_breath]` | 急促换气 | |
| `[sigh]` | 叹气 | |
| `[cough]` | 咳嗽 | |
| `[hissing]` | 嘘声/吸气 | |
| `[lipsmack]` | 咂嘴 | |
| `[clucking]` | 弹舌 | |
| `[accent]` | 口音变化 | |
| `[noise]` / `[vocalized-noise]` / `[mn]` | 噪声类 | |
| `<laughter>...</laughter>` | 包住一段笑声 | |
| `<strong>...</strong>` | 强调重音 | 他展现了非凡的`<strong>勇气</strong>` |

### 用法示例

```
在他讲述那个荒诞故事的过程中，他突然[laughter]停下来，因为他自己也被逗笑了[laughter]。
```

```
[breath]因为他们那一辈人[breath]在乡里面住的要习惯一点，[breath]邻居都很活络，[breath]嗯，都很熟悉。[breath]
```

### 注意事项

1. 标记用英文方括号 `[ ]` / 尖括号 `< >`，**不要**用中文【】（）——前端会删除中文括号内容。
2. 标记前后最好留标点或空格，让模型正确切分。
3. 不要过度密集堆叠标记，否则音色/节奏会失真。
4. 这些标记**不计入字数限制**，但会占用 token，超长正文 + 大量标记可能触顶。

---

## 第二类：情绪 / 语气 / 方言指令（靠 `<|endofprompt|>` 前缀）

这类**不放在正文里**，而是控制「用什么情绪/口音/语速来说这句话」。

### 两种调用方式

#### 方式 A：zero_shot 模式（推荐，配合参考音色）

把指令拼在 **`prompt_text` 的最前面**，格式：

```
{指令}<|endofprompt|>{参考音频对应的原文}
```

- `<|endofprompt|>` 是分隔符，左边是「说话方式指令」，右边是参考音频原本念的原文。
- 模型会按左边的指令去「演绎」右边的参考文本，再用参考音频的音色合成。

**示例**：
```
You are a helpful assistant. 请非常开心地说一句话。<|endofprompt|>希望你以后能够做的比我还好呦。
```

#### 方式 B：instruct2 模式

直接把指令写进 **`instruct_text`** 字段（不需要 `prompt_text`，音色由 `prompt_wav` 决定）：

```
You are a helpful assistant. 请用广东话表达。<|endofprompt|>
```

> 注：`<|endofprompt|>` 在 instruct2 里可省略，指令直接写即可；零样本方式 A 建议保留分隔符以区分「指令」和「参考原文」。

### 内置预设指令表（可直接复用）

以下指令前缀均以 `You are a helpful assistant.` 开头，是服务端 webui 同款预设：

| 意图 | 指令（拼在 `prompt_text` 前 / 写进 `instruct_text`） |
|---|---|
| 默认 | `You are a helpful assistant.` |
| 开心 | `You are a helpful assistant. 请非常开心地说一句话。` |
| 伤心 | `You are a helpful assistant. 请非常伤心地说一句话。` |
| 生气 | `You are a helpful assistant. 请非常生气地说一句话。` |
| 大声 | `You are a helpful assistant. Please say a sentence as loudly as possible.` |
| 轻声 | `You are a helpful assistant. Please say a sentence in a very soft voice.` |
| 慢速 | `You are a helpful assistant. 请用尽可能慢地语速说一句话。` |
| 快速 | `You are a helpful assistant. 请用尽可能快地语速说一句话。` |
| 小猪佩奇风格 | `You are a helpful assistant. 我想体验一下小猪佩奇风格，可以吗？` |
| 机器人风格 | `You are a helpful assistant. 你可以尝试用机器人的方式解答吗？` |
| 广东话 | `You are a helpful assistant. 请用广东话表达。` |
| 东北话 | `You are a helpful assistant. 请用东北话表达。` |
| 四川话 | `You are a helpful assistant. 请用四川话表达。` |
| 上海话 | `You are a helpful assistant. 请用上海话表达。` |
| 河南话 | `You are a helpful assistant. 请用河南话表达。` |
| 山东话 | `You are a helpful assistant. 请用山东话表达。` |
| 陕西话 | `You are a helpful assistant. 请用陕西话表达。` |
| 天津话 | `You are a helpful assistant. 请用天津话表达。` |

> 方言/情绪指令可自由改写措辞，模型对自然语言指令有一定泛化能力。

### 注意事项（重要）

1. **`prompt_text` 必须保留「参考音频原文」部分**。
   格式是 `{指令}<|endofprompt|>{原文}`，不要只传指令而丢掉原文。
   若只传指令、没有 `<|endofprompt|>` 和原文，服务端会自动补默认前缀 `You are a helpful assistant.<|endofprompt|>`，但参考文本为空 → 触发 `too short than prompt text` 警告，音色参考变差。

2. **`tts_text`（正文）里不要写 `<|...|>`**。
   前端检测到文本含 `<|` 和 `|>` 时会**跳过整段文本归一化**（数字不转写、标点不规整），导致读音异常。

3. **指令前缀会让 `prompt_text` 变长**，比 `tts_text` 长很多时服务端会打印：
   `WARNING synthesis text ... too short than prompt text ... this may lead to bad performance`
   这是已知现象，不影响合成成功（官方 zero_shot 示例同样带前缀）。如要消除警告，可让 `tts_text` 长度接近或大于带前缀的 `prompt_text`。

4. **两类可组合使用**：正文用第一类标记（如 `[laughter]`），同时 `prompt_text` 用第二类前缀（如「开心」）。两者作用在不同字段，互不冲突。

---

## 插件端接入建议

- 第一类：在组装 `tts_text` 时，按用户/场景需求把对应标记直接插入文本即可，无需改动接口。
- 第二类（zero_shot）：把「选中的情绪/方言指令」+ `<|endofprompt|>` + **`voices.json` 里该音色绑定的固定原文** 拼成 `prompt_text` 传入。
  - **关键**：`prompt_text` 里**只放参考音频对应的纯朗读文本**，不要把 LLM 的 system prompt、角色扮演设定塞进去。否则会污染参考文本、触发上述 WARNING，并让音色参考失真。
- `voices.json` 建议只存「文件名 → 参考音频原文」，情绪/方言由插件在调用时动态拼前缀。

---

## 完整请求示例（zero_shot + 开心 + 笑声）

```
POST /inference_zero_shot
表单字段：
  tts_text   = 今天天气真好[laughter]我们一起去散步吧
  prompt_text= You are a helpful assistant. 请非常开心地说一句话。<|endofprompt|>希望你以后能够做的比我还好呦。
  prompt_wav_path = xiaoyu.wav
```

服务端预期日志：`[zero_shot] tts_text=今天天气真好...` 并返回 int16 PCM 音频。

---

## 程序化自动插入规则（无需大模型）

这些标记本质是字面字符串（模型注册的 special token），**插件端用纯字符串规则就能自动拼入 `tts_text`，不需要 LLM 参与**。下面是推荐规则及评价。

### 规则 1：标点驱动换气（最基础，建议默认开启）
- 句末标点 `。！？…` 后插入 `[breath]`
- 句中标点 `，、；：` 后插入 `[quick_breath]`
- 波浪号 `~/～`（拖音/短停顿）后插入 `[quick_breath]`，无条件、不依赖间隔
- 连续标点（如 `……`）只插一次，避免堆叠

**评价：✅ 好，风险低。** 给语音加自然呼吸节奏，几乎所有文本受益。
**风险与防护**：长句 `，` 太多会导致换气过密、显得喘。建议加「两个 `[quick_breath]` 之间至少间隔 N 个字符（如 8 字）才插」。

### 规则 2：关键词触发音效（场景化，建议默认关闭）
- 维护映射：`笑/哈哈/噗嗤 → [laughter]`、`叹气/唉 → [sigh]`、`咳 → [cough]`
- 命中时在**整句末尾**追加对应标记（不要在字中间插）

**评价：⚠️ 可用但易翻车，建议默认关、用户手动开。**
**坑**：
1. 误触发——「可笑的是」里的「笑」不是真笑，会乱插 `[laughter]`。建议用短语/词边界匹配，而非单字。
2. 中文「笑」是语义不是拟声，插在字中间很怪，应在句末追加表示「说完带着笑」。
3. 与规则 1 叠加时 `[laughter]` 后可能又跟 `[breath]`，需去重。

### 规则 3：配置开关控制（强烈建议）
- 开关 A：`auto_breath`（默认开）→ 规则 1
- 开关 B：`auto_effect`（默认关）→ 规则 2
- 开关 C：`effect_intensity`：轻/中/强，控制插标密度

**评价：✅ 好。** 默认 `auto_breath=on`、`auto_effect=off`，避免用户觉得「语音里老有奇怪喘气/笑声」。

### 规则 4：长度 / 密度上限（防护栏，必需）
- 单段文本最多插 N 个标记（如每 50 字 ≤ 3 个）
- 相邻标记最小间隔 M 个字符
- 超长文本（>100 字）分段处理，每段独立插

**评价：✅ 必需。** 没有密度上限，规则叠起来会「喘得像哮喘 + 笑得像疯子」。

### 规则 5：标记隔离（必须）
- 自动插入的标记**只拼在 `tts_text`**，绝不污染 `prompt_text` 的参考原文和 `<|endofprompt|>` 指令前缀。

**评价：✅ 必须。** 否则破坏「`prompt_text` 只放参考原文」约束，触发音色污染。

### 规则总评

| 规则 | 好不好 | 建议默认 |
|---|---|---|
| 1 标点换气 | ✅ 好，低风险 | 开 |
| 2 关键词音效 | ⚠️ 能用但易翻车 | 关（用户自选开） |
| 3 配置开关 | ✅ 好 | 必做 |
| 4 密度上限 | ✅ 必需 | 必做 |
| 5 隔离 tts_text | ✅ 必须 | 必做 |

**核心建议**：换气（规则 1）放心自动化；音效（规则 2）默认关、交给用户/LLM 触发。

---

## 跨语种注意事项

标记本身是模型层 special token，**与语种无关**——`[laughter]` `[breath]` 等插进中文/英文/日文文本都生效。但「自动插标的规则」必须按语种分别配置。

### 1. 音效标记本身：跨语种通用 ✅
```
I was so surprised[laughter] that I dropped my phone.          # 英文
レキシテキセカイニオイテワ[breath]カコワタンニ...              # 日文（片假名）
```
规则 1（标点换气）、规则 2（关键词音效）对标记本身跨语种有效。

### 2. 标点换气规则（规则 1）：按语种用不同标点集 ⚠️
- 中文句末：`。！？…`；句中点：`，、；：`
- 英文句末：`. ! ?` —— **英文句点 `.` 大量出现在缩写/数字（Mr. / 3.5），不能见 `.` 就插 `[breath]`**，需用正则排除
- 日文句末：`。！？`，读点 `、`（相当于逗号）

→ 标点规则不能一套写死，要按目标语种切换标点集合。

### 3. 关键词音效（规则 2）：每语种维护独立词典 ⚠️
- 中文：`笑→[laughter]`、`叹气→[sigh]`、`咳→[cough]`
- 英文：`laugh→[laughter]`、`sigh→[sigh]`、`cough→[cough]`
- 日文：`笑う→[laughter]`、`ため息→[sigh]`

→ 纯词典工作，不同语种不同词。英文/日文里「笑」可能是叙述不是拟声，误触发风险比中文更高。

### 4. 第二类情绪/方言指令（方式 A）：语种耦合最强 ⚠️
- **情绪指令跨语种通用**：`You are a helpful assistant. Please say it angrily.` 对英文文本同样生效。
- **方言指令仅中文有效**：「请用四川话表达」对英文文本无意义，非中文时不拼方言前缀。

### 5. 日文预处理硬约束
CosyVoice3 要求日文必须转成**片假名**才能正常合成（`example.py` 注释明确）。
```
# NOTE for Japanese usage, you must translate it to katakana.
```
这与标记无关，但是跨语种接入时必须做的预处理。

### 跨语种规则总表

| 维度 | 跨语种？ | 插件端要做的事 |
|---|---|---|
| 音效标记本身 | ✅ 通用 | 直接插，不用改 |
| 标点换气规则 | ❌ 按语种 | 不同语种用不同标点集；英文 `.` 排除缩写/数字 |
| 关键词音效 | ❌ 按语种 | 每语种维护独立词典 |
| 情绪指令（方式A） | ✅ 通用 | 可直接用 |
| 方言指令（方式A） | ❌ 仅中文 | 非中文时不拼方言前缀 |
| 日文预处理 | ❌ 特殊 | 转片假名 |

**结论**：标记本身跨语种免费生效；但「自动插标规则」必须按语种分别配置（标点集 + 关键词词典 + 是否启用方言）。建议插件端把**语种作为一个维度**，规则参数跟着语种走，而不是全局一套。

---

## 插件配置项（astrbot_plugin_cosyvoice）

插件已实现上述规则 1（标点换气）与规则 2（关键词音效），按语种字典 + `voices.<音色>.language` 自动切换。配置项（在插件配置页或 `metadata.yaml`）：

| 配置项 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `auto_breath` | bool | `true` | 标点换气：句末 `。！？…` 插 `[breath]`、句中 `，、；：` 插 `[quick_breath]`、波浪号 `~/～` 后插 `[quick_breath]`（拖音短停顿）。低风险，默认开。 |
| `auto_effect` | bool | `false` | 关键词音效：按语种词典在句末追加 `[laughter]`/`[sigh]`/`[cough]`。易翻车，默认关。 |
| `effect_intensity` | string | `medium` | 音效密度：`light`（每句≤1）/ `medium`（≤3）/ `strong`（≤5）。仅 `auto_effect=true` 生效。 |
| `voices.<音色>.markup` | bool | `true` | **音色级**副语言标记总开关：`false` 时该音色**完全不注入**任何标记（换气与音效都不注入），只念纯文本。**优先级高于**全局 `auto_breath` / `auto_effect`。 |

**行为要点**：
- 音色级开关：`voices.<音色>.markup=false` 可让单个音色跳过全部副语言标记注入，用于「该音色/参考音频对标记敏感，插入 `[breath]`/`[laughter]` 后音色失真、读法怪异」的场景；其余音色仍按全局配置正常注入。可在 AstrBot 配置页的音色项里设置，也可用 WebUI 音色库编辑。
- 语种判定：优先取 `voices.<音色>.language`（`zh`/`ja`/`en`），缺失则按字符特征推断（含片假名→ja，纯字母→en，否则 zh）。
- 标记隔离：自动插入的标记**只进合成文本**（`tts_text`），绝不污染展示文字与 `prompt_text`。
- 白名单：`[laughter]` `[breath]` `[quick_breath]` `[sigh]` `[cough]` 等 special token 不会被「剥离括号」逻辑误删。
- 阶段 C（情绪/方言 `<|endofprompt|>` 指令前缀）本期未实现。
