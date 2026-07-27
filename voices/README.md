# 参考音频目录（AstrBot 端，上传模式用）

> 推荐：把参考音频 **放在 CosyVoice 服务端** 的 `--voices_dir` 目录（见下），避免每次
> 请求都把大文件从 AstrBot 端上传一遍。本目录仅用于「上传模式」——即未配置
> `server_voices_dir`、且 `prompt_wav` 是 AstrBot 服务端本地路径时的回退方案。

把每个音色的 **参考音频 wav** 放在本目录下即可，配置 `voices.<音色名>.prompt_wav` 时
只需写文件名（如 `xiaoyu.wav`），插件会自动在本目录查找并上传。

## 要求
- 格式：wav（单声道或双声道均可，服务端会重采样为 16k 用于 zero-shot 建模）
- 时长：3~10 秒最合适，过短或过长会影响克隆质量
- 清晰度：环境安静、无明显噪声；文本需与音频内容逐字对应

## 推荐：服务端目录（server_voices_dir 模式）
1. 在 CosyVoice 机器上建目录（如 `./cosyvoice_voices`），把 wav 放进去。
2. 启动 server 时加 `--voices_dir /path/to/cosyvoice_voices`。
3. 插件配置 `server_voices_dir: /path/to/cosyvoice_voices`，`voices.<音色名>.prompt_wav`
   只写文件名（如 `xiaoyu.wav`）。插件会只传文件名，服务端读本地文件，**不占用带宽上传**。

## 文本放哪
- **服务端目录模式（推荐）**：文本写在 CosyVoice 服务端 `voices_dir/voices.json`（`{"文件名":"文本"}`），
  插件 `voices.<音色>.prompt_text` 可留空，服务端自动取。
- **上传模式（回退）**：文本写在插件配置的 `voices.<音色名>.prompt_text` 里（字符串）。
