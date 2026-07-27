# 参考音频目录（CosyVoice 服务端）

把参考音频 **wav** 放在这个目录（或你用 `--voices_dir` 指定的任意目录）。

## voices.json（文件名 -> 参考文本）

同目录放一个 `voices.json`，记录每个 wav 对应的原文文本，例如：

```json
{
  "xiaoyu.wav": "你好，我是小宇，很高兴为你服务。",
  "boss.wav": "这件事交给我来处理。"
}
```

- 插件调用时只传 `prompt_wav_path=文件名`，服务端会自动从 `voices.json` 取对应文本，
  无需在 AstrBot 插件配置里重复写 `prompt_text`。
- `prompt_text` 也可由插件请求时传（会覆盖 voices.json 的值）。
- 修改 `voices.json` 后**无需重启**服务，下次请求即生效（服务端每次读取）。

## 要求
- wav 格式，3~10 秒最佳，环境安静、无明显噪声。
- 文本需与音频内容逐字对应（zero-shot 建模用）。

> 本目录下的 `voices.example.json` 只是模板，重命名为 `voices.json` 并改成你自己的内容即可；
> 删除示例文件不影响服务运行。
