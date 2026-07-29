"""CosyVoice3 HTTP 客户端。

对准 deploy/cosyvoice_api.py（最小 FastAPI 推理服务）：
  - 路由：/inference_zero_shot | /inference_instruct2
  - 请求：multipart 表单（tts_text, prompt_text, prompt_wav 文件 / 或 prompt_wav_path 服务端路径）
  - 响应：裸 int16 PCM 字节流（无 WAV 头），需要补 WAV 头
"""

import os

import httpx

from astrbot.api import logger

from ..utils import audio


class CosyVoiceServerError(Exception):
    """语音服务器不可达（连接失败 / 超时）。

    与「服务器在线但推理报错」区分：用于触发专门的失联提示，
    而非笼统的「合成失败」。
    """


class CosyVoiceClient:
    def __init__(
        self,
        base_url: str,
        sample_rate: int = 24000,
        timeout: int = 60,
        cache_dir: str = "",
    ):
        self.base_url = base_url.rstrip("/")
        self.sample_rate = sample_rate
        self.timeout = timeout
        self.cache_dir = cache_dir
        self._sr_fetched = False

    async def fetch_sample_rate(self) -> int:
        """从服务端 / 健康检查获取真实采样率（模型实际值），覆盖配置值。失败则沿用配置。"""
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as c:
                r = await c.get(f"{self.base_url}/")
            if r.status_code == 200:
                sr = r.json().get("sample_rate")
                if sr:
                    self.sample_rate = int(sr)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[cosyvoice] 获取服务端采样率失败，沿用配置值 {self.sample_rate}: {e}")
        return self.sample_rate

    async def synthesize(
        self,
        text: str,
        prompt_wav: str = "",
        prompt_text: str = "",
        prompt_wav_path: str = "",
        mode: str = "zero_shot",
    ) -> bytes:
        """请求合成，返回裸 int16 PCM 字节（未封装 WAV 头）。

        mode=zero_shot 需要 prompt_text + 参考音频。参考音频二选一：
          - prompt_wav_path：服务端本地文件名（无需上传，推荐大文件用），
          - prompt_wav：AstrBot 服务端本地路径，会以文件形式上传。
        """
        # 首次合成时向服务端查询真实采样率，确保 WAV 编码与实际 PCM 一致
        if not self._sr_fetched:
            self._sr_fetched = True
            await self.fetch_sample_rate()

        url = f"{self.base_url}/inference_{mode}"
        data = {"tts_text": text}
        files = None

        if mode == "zero_shot":
            data["prompt_text"] = prompt_text
            if prompt_wav_path:
                data["prompt_wav_path"] = prompt_wav_path
            elif prompt_wav:
                if not os.path.exists(prompt_wav):
                    raise FileNotFoundError(f"参考音频不存在: {prompt_wav}")
                data["prompt_text"] = prompt_text
                files = {
                    "prompt_wav": (
                        os.path.basename(prompt_wav),
                        open(prompt_wav, "rb"),
                        "audio/wav",
                    )
                }
            else:
                raise ValueError("未提供参考音频（prompt_wav 或 prompt_wav_path 至少其一）")

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(url, data=data, files=files)
            resp.raise_for_status()
            pcm = resp.content
        except httpx.HTTPStatusError as e:
            logger.error(f"[cosyvoice] 合成失败 HTTP {e.response.status_code}: {e.response.text[:500]}")
            raise
        except httpx.RequestError as e:
            logger.error(f"[cosyvoice] 无法连接 CosyVoice 服务 {url}: {e}")
            raise CosyVoiceServerError(f"无法连接语音服务器 {url}: {e}") from e
        finally:
            if files:
                files["prompt_wav"][1].close()

        if not pcm:
            raise RuntimeError("[cosyvoice] 服务返回空音频")

        return pcm

    async def synthesize_to_file(
        self,
        text: str,
        prompt_wav: str = "",
        prompt_text: str = "",
        prompt_wav_path: str = "",
        mode: str = "zero_shot",
    ) -> str:
        """合成并返回临时 wav 文件路径（Record 可直接引用）。"""
        pcm = await self.synthesize(text, prompt_wav, prompt_text, prompt_wav_path, mode)
        return audio.pcm_to_wav_file(pcm, self.sample_rate, self.cache_dir)
