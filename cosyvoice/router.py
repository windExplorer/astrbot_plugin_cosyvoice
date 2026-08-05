"""CosyVoice 多服务端负载均衡客户端。

把多个 CosyVoice 推理服务（每台可能部署在不同机器/端口）抽象成一个
「路由客户端」：
  - 按权重分流：weight 越大，被选中的概率越高（每台独立计算概率）；
  - 故障隔离：某台连续失败后进入临时冷却（不再被选中），其他节点照常服务，
    冷却期到后自动恢复探测；
  - 配置兼容：可同时使用旧的 base_url（单台）或新的 servers 列表（多台）。

对外暴露与 CosyVoiceClient 一致的接口：
  - synthesize(text, ...) -> bytes
  - synthesize_to_file(text, ...) -> str
  - close()
  - sample_rate / cache_dir 属性（取当前节点，供 TtsEngine 封装 WAV 头用）
"""

import asyncio
import random
import time

from astrbot.api import logger

from .client import CosyVoiceClient, CosyVoiceServerError


class CosyVoiceRouter:
    """多服务端负载均衡 + 故障隔离客户端。"""

    # 节点单次失败的临时冷却（秒）：失败后该节点此期间内不再参与分流
    NODE_COOLDOWN_SEC = 30.0
    # 连续失败多少次判定该节点「疑似宕机」，进入更长的冷却
    FAIL_THRESHOLD = 3

    def __init__(
        self,
        servers: list[dict] | None = None,
        sample_rate: int = 24000,
        timeout: int = 150,
        cache_dir: str = "",
        max_retry: int = 0,
        retry_backoff: float = 0.5,
        fallback_url: str = "",
    ):
        """初始化路由客户端。

        :param servers: 服务端列表，每项 {url, enabled, weight}；
            缺省/全空时回退到 fallback_url（兼容旧 base_url 配置）。
        :param fallback_url: 旧配置的单一服务地址，servers 为空时使用。
        """
        self.timeout = timeout
        self.cache_dir = cache_dir
        self._max_retry = max_retry
        self._retry_backoff = retry_backoff
        self._fallback_url = (fallback_url or "").strip().rstrip("/")
        # 解析并构建节点
        self._nodes: list[dict] = []
        self._rebuild(servers, sample_rate)
        # 当前生效节点下标（供 sample_rate / cache_dir 属性取值）
        self._current_index = 0

    # ---------- 节点管理 ----------
    def _normalize(self, servers: list[dict] | None) -> list[dict]:
        """归一化 servers 配置：{url, enabled, default, weight} -> 过滤后的可用节点原始配置。"""
        if not servers:
            return []
        out: list[dict] = []
        for item in servers:
            if not isinstance(item, dict):
                continue
            url = (str(item.get("url") or "").strip()).rstrip("/")
            if not url:
                continue
            enabled = item.get("enabled", True)
            if isinstance(enabled, str):
                enabled = enabled.strip().lower() not in ("0", "false", "no", "off")
            if not enabled:
                continue
            default = item.get("default", False)
            if isinstance(default, str):
                default = default.strip().lower() in ("1", "true", "yes", "on")
            weight = 0
            try:
                weight = max(1, int(float(item.get("weight", 1) or 1)))
            except (TypeError, ValueError):
                weight = 1
            out.append({"url": url, "enabled": True, "default": bool(default), "weight": weight})
        return out

    def _rebuild(self, servers: list[dict] | None, sample_rate: int):
        """按配置重建节点列表（配置热更新时调用）。"""
        norm = self._normalize(servers)
        if not norm and self._fallback_url:
            # 兼容旧 base_url：单台服务
            norm = [{"url": self._fallback_url, "enabled": True, "weight": 1}]
        if not norm:
            norm = [{"url": "http://127.0.0.1:50002", "enabled": True, "weight": 1}]
        nodes: list[dict] = []
        for item in norm:
            client = CosyVoiceClient(
                base_url=item["url"],
                sample_rate=sample_rate,
                timeout=self.timeout,
                cache_dir=self.cache_dir,
                max_retry=self._max_retry,
                retry_backoff=self._retry_backoff,
            )
            nodes.append(
                {
                    "url": item["url"],
                    "weight": item["weight"],
                    "default": bool(item.get("default", False)),
                    "client": client,
                    "failed": 0,
                    "cooldown_until": 0.0,
                }
            )
        # 关闭旧的未复用 client（配置变更时避免连接泄漏）
        for old in self._nodes:
            try:
                old["client"].close()
            except Exception:  # noqa: BLE001
                pass
        self._nodes = nodes
        defaults = [n["url"] for n in nodes if n["default"]]
        logger.info(
            f"[cosyvoice] 负载均衡已启用，节点: "
            + ", ".join(f"{n['url']}(w={n['weight']})" for n in nodes)
            + (f" | 默认节点: {defaults}" if defaults else "")
        )

    def update_servers(self, servers: list[dict] | None, sample_rate: int = 24000):
        """配置热更新：重建节点（保持原有分流逻辑）。"""
        self._rebuild(servers, sample_rate)

    # ---------- 分流选择 ----------
    def _pick_index(self) -> int:
        """选一个【可用】（非冷却中）节点：
        优先默认节点（default=true 且可用），有多个默认则随机取一；
        否则按权重随机分流；全冷却则退回第一个可用。"""
        now = time.time()
        candidates = [i for i, n in enumerate(self._nodes) if n["cooldown_until"] <= now]
        if not candidates:
            candidates = list(range(len(self._nodes)))
        # 默认节点优先（若配置了 default 且当前可用）
        defaults = [i for i in candidates if self._nodes[i].get("default")]
        if defaults:
            return random.choice(defaults)
        weights = [self._nodes[i]["weight"] for i in candidates]
        total = sum(weights) or 1
        r = random.uniform(0, total)
        acc = 0.0
        for idx, w in zip(candidates, weights):
            acc += w
            if r <= acc:
                return idx
        return candidates[0]

    def _mark_success(self, index: int):
        self._nodes[index]["failed"] = 0

    def _mark_failure(self, index: int):
        n = self._nodes[index]
        n["failed"] += 1
        if n["failed"] >= self.FAIL_THRESHOLD:
            n["cooldown_until"] = time.time() + self.NODE_COOLDOWN_SEC
            logger.warning(
                f"[cosyvoice] 节点 {n['url']} 连续失败 {n['failed']} 次，"
                f"临时隔离 {self.NODE_COOLDOWN_SEC}s（其余节点继续服务）"
            )
            n["failed"] = 0

    # ---------- 对外接口（对齐 CosyVoiceClient） ----------
    @property
    def sample_rate(self) -> int:
        """当前节点采样率（供 TtsEngine 封装 WAV 头）。"""
        cur = self._nodes[self._current_index]["client"]
        return getattr(cur, "sample_rate", 24000)

    @property
    def base_url(self) -> str:
        cur = self._nodes[self._current_index]["client"]
        return getattr(cur, "base_url", "")

    async def synthesize(self, text: str, **kwargs) -> bytes:
        """请求合成（按权重分流；单节点失败自动尝试下一节点，最多遍历全部）。"""
        last_err: Exception | None = None
        tried = set()
        for _ in range(len(self._nodes)):
            idx = self._pick_index()
            if idx in tried:
                # 全部试过一轮仍未成功
                break
            tried.add(idx)
            node = self._nodes[idx]
            self._current_index = idx
            try:
                pcm = await node["client"].synthesize(text, **kwargs)
                self._mark_success(idx)
                return pcm
            except CosyVoiceServerError as e:
                last_err = e
                logger.warning(
                    f"[cosyvoice] 节点 {node['url']} 失联: {e}，尝试下一节点"
                )
                self._mark_failure(idx)
            except Exception as e:  # noqa: BLE001
                last_err = e
                logger.warning(
                    f"[cosyvoice] 节点 {node['url']} 合成失败: {e}，尝试下一节点"
                )
                self._mark_failure(idx)
        if last_err is not None:
            raise last_err
        raise RuntimeError("[cosyvoice] 无可用节点")

    async def synthesize_to_file(self, text: str, **kwargs) -> str:
        pcm = await self.synthesize(text, **kwargs)
        from ..utils import audio

        return audio.pcm_to_wav_file(pcm, self.sample_rate, self.cache_dir)

    async def fetch_sample_rate(self) -> int:
        """从当前节点获取采样率（失败沿用配置）。"""
        try:
            cur = self._nodes[self._current_index]["client"]
            return await cur.fetch_sample_rate()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[cosyvoice] 获取服务端采样率失败: {e}")
            return self.sample_rate

    async def close(self):
        for n in self._nodes:
            try:
                await n["client"].close()
            except Exception:  # noqa: BLE001
                pass
        self._nodes = []
