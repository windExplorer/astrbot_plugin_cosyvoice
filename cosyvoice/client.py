"""CosyVoice3 HTTP 客户端。

对准 deploy/cosyvoice_api.py（最小 FastAPI 推理服务）：
  - 路由：/inference_zero_shot | /inference_instruct2
  - 请求：multipart 表单（tts_text, prompt_text, prompt_wav 文件 / 或 prompt_wav_path 服务端路径）
  - 响应：裸 int16 PCM 字节流（无 WAV 头），需要补 WAV 头
"""

import os
import asyncio

import httpx

from astrbot.api import logger

from ..utils import audio


def _looks_polluted(text: str) -> bool:
    """判断参考文本是否疑似被 LLM / 系统提示污染。

    参考文本（prompt_text）应为「参考音频对应的人声朗读文字」，应当是普通的
    一句话/一段中文或英文，不含控制标记、不应过长。若混入 system prompt、
    角色设定、对话历史或 <|endofprompt|> 之类标记，则视为污染。

    命中条件（任一即判污染）：
      - 含 <|...|> / </...> / <...> 形式的标记（含 endofprompt）；
      - 含典型 LLM 提示词片段（如 "You are a helpful assistant"、
        "system" / "assistant" / "user" 角色标记、对话历史拼接痕迹）；
      - 长度异常（> 300 字，参考文本远不该这么长，极可能是整段上下文被误填）。
    """
    t = text.strip()
    if not t:
        return False
    if "<|" in t or "|>" in t:
        return True
    if "<" in t and ">" in t:
        return True
    low = t.lower()
    markers = (
        "you are a helpful assistant",
        "you are an",
        "assistant.<|endofprompt|>",
        "system prompt",
        "<|endofprompt|>",
    )
    if any(m in low for m in markers):
        return True
    # 参考文本长度异常：正常参考音频文本极短（10–50 字），>150 字几乎一定是被污染
    if len(t) > 150:
        return True
    return False


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
        timeout: int = 150,
        cache_dir: str = "",
        max_retry: int = 0,
        retry_backoff: float = 0.5,
    ):
        self.base_url = base_url.rstrip("/")
        self.sample_rate = sample_rate
        # timeout 仅约束「读取」（推理耗时），连接保持较短以便真正连不上时快速失败。
        # 注意：队列版服务端（cosyvoice_api_queue.py）单次推理可能 >30s，且入队等待
        # 最长 30s，故客户端读超时不能压成 30s 上限（否则服务端还在排队/推理，客户端
        # 已 ReadTimeout 断连，自己制造雪崩）。直接用配置的 timeout（默认 150s，
        # 略大于服务端 INFER_TIMEOUT+入队等待）。
        self.timeout = timeout
        self._http_timeout = httpx.Timeout(timeout, connect=10.0)
        self._max_retry = max_retry  # 退避重试次数；0=不重试（一次失败直接抛，由插件进冷却+回退文字）
        self._retry_backoff = retry_backoff  # 退避基数（秒）：实际等待 = base * 2**attempt
        self.cache_dir = cache_dir
        self._sr_fetched = False
        # 复用单一 httpx 客户端（含连接池）：避免每次请求创建/销毁 AsyncClient。
        # 多段合成时共用同一个连接池，消除 TIME_WAIT 积累和间歇性连接重建带来的
        # ReadError / RemoteProtocolError 等低概率网络层故障。
        self._client = httpx.AsyncClient(timeout=self._http_timeout)

    async def fetch_sample_rate(self) -> int:
        """从服务端 / 健康检查获取真实采样率（模型实际值），覆盖配置值。失败则沿用配置。"""
        try:
            r = await self._client.get(f"{self.base_url}/")
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
        # tts_text 与 prompt_text 严格分离：
        # - tts_text 仅放目标合成文本；
        # - prompt_text 仅放参考音频对应的纯人声朗读文字（来自 voices.<音色>.prompt_text），
        #   绝不拼接 LLM system prompt / 角色设定 / 对话历史 / <|endofprompt|> 等任何标记。
        data = {"tts_text": text}
        # 参考音频以 bytes 缓存（而非文件句柄）：重试时必须能重新构造请求体。
        # 用 open() 句柄会在第一次请求后停在 EOF，重试将上传 0 字节，服务端解析必然失败。
        wav_bytes: bytes | None = None
        wav_name = ""

        if mode == "zero_shot":
            # 仅在参考文本非空时传该字段；为空则完全不带，交给服务端从 voices.json 按文件名取。
            # 防御性净化：若参考文本混入 <|endofprompt|> / system prompt 类标记或明显过长，
            # 视为污染，丢弃该字段改由服务端从 voices.json 取干净文本，避免污染参考音色。
            pt = (prompt_text or "").strip()
            if pt and not _looks_polluted(pt):
                data["prompt_text"] = pt
            if prompt_wav_path:
                data["prompt_wav_path"] = prompt_wav_path
            elif prompt_wav:
                if not os.path.exists(prompt_wav):
                    raise FileNotFoundError(f"参考音频不存在: {prompt_wav}")
                with open(prompt_wav, "rb") as f:
                    wav_bytes = f.read()
                wav_name = os.path.basename(prompt_wav)
            else:
                raise ValueError("未提供参考音频（prompt_wav 或 prompt_wav_path 至少其一）")

        last_err: Exception | None = None
        pcm: bytes = b""
        for attempt in range(self._max_retry + 1):
            # 每次尝试都用缓存 bytes 重新构造 multipart，保证重试请求体完整
            files = (
                {"prompt_wav": (wav_name, wav_bytes, "audio/wav")}
                if wav_bytes is not None
                else None
            )
            try:
                resp = await self._client.post(url, data=data, files=files)
            except httpx.ConnectError as e:
                logger.error(f"[cosyvoice] 无法连接 CosyVoice 服务 {url}（服务未启动或地址不可达）: {e}")
                raise CosyVoiceServerError(f"无法连接语音服务器 {url}: {e}") from e
            except httpx.ConnectTimeout as e:
                logger.error(f"[cosyvoice] 连接 CosyVoice 服务超时 {url}（服务未启动或地址不可达）: {e}")
                raise CosyVoiceServerError(f"无法连接语音服务器 {url}: {e}") from e
            except httpx.ReadTimeout as e:
                # 读取超时：服务在线但本次推理/排队过慢。按偶发错误处理（不熔断），退避重试。
                last_err = e
                logger.warning(
                    f"[cosyvoice] 合成读取超时（第 {attempt + 1}/{self._max_retry + 1} 次，"
                    f"服务在线但耗时超过 {self.timeout}s） {url}: {e}"
                )
                if attempt < self._max_retry:
                    await asyncio.sleep(self._backoff(attempt))
                    continue
                raise
            except httpx.RequestError as e:
                # 连接中途被服务端断开（RemoteProtocolError / ReadError 等）：
                # 服务其实是「可达」的，不应误报「服务器失联」，按偶发错误退避重试后抛出。
                last_err = e
                msg = str(e).strip()
                logger.warning(
                    f"[cosyvoice] 请求 CosyVoice 服务中断（第 {attempt + 1}/{self._max_retry + 1} 次，"
                    f"服务可达但本次失败） {url}: {msg or type(e).__name__}"
                )
                if attempt < self._max_retry:
                    await asyncio.sleep(self._backoff(attempt))
                    continue
                raise
            # HTTP 状态码：区分「可重试」与「致命」。
            code = resp.status_code
            if 200 <= code < 300:
                pcm = resp.content
                break
            # 队列版新增语义：429 队满/入队超时、503 模型未加载、504 推理超时
            # —— 这些都应退避重试，而不是丢弃（否则高峰期直接丢语音）。
            if code in (429, 503, 504):
                last_err = httpx.HTTPStatusError(
                    f"服务端返回 {code}", request=resp.request, response=resp
                )
                logger.warning(
                    f"[cosyvoice] 合成被服务端拒绝（第 {attempt + 1}/{self._max_retry + 1} 次）"
                    f" HTTP {code}: {resp.text[:200]} -> 退避重试"
                )
                if attempt < self._max_retry:
                    await asyncio.sleep(self._backoff(attempt))
                    continue
                raise httpx.HTTPStatusError(
                    f"合成失败 HTTP {code}", request=resp.request, response=resp
                )
            # 其它状态码（400 参数错误 / 500 服务端生成失败等）：不可重试，直接抛。
            logger.error(f"[cosyvoice] 合成失败 HTTP {code}: {resp.text[:500]}")
            raise httpx.HTTPStatusError(
                f"合成失败 HTTP {code}", request=resp.request, response=resp
            )

        if not pcm:
            # 重试耗尽（上面 break 未命中）：抛出最后一次错误
            if last_err is not None:
                raise last_err
            raise RuntimeError("[cosyvoice] 合成失败（未知错误）")

        return pcm

    def _backoff(self, attempt: int) -> float:
        """指数退避秒数：base, base*2, base*4 ...（base 由 tts_retry_backoff 配置）。

        await asyncio.sleep 不阻塞事件循环，配合后台合成任务安全。
        """
        return self._retry_backoff * (2 ** max(attempt, 0))

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

    async def close(self):
        """关闭底层 httpx 客户端及连接池。"""
        await self._client.aclose()
