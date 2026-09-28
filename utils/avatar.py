"""QQ 头像抓取与本地缓存（WebUI 会话列表用）。

为什么放后端抓（而不是让浏览器直接请求腾讯 CDN）：

- 控制台运行在 AstrBot 面板的 sandbox iframe 里，直连外网 CDN 在部分部署环境
  （内网/离线/企业代理/浏览器拦截第三方图片）下会整片挂掉；
- 群头像本来就没有现成 URL 字段，需要按群号自己拼；
- 抓到本地后可以「更新头像」「离线可用」，并且列表渲染不再依赖外网。

数据来源（公开 CDN，无需鉴权）：

- 好友：``https://q1.qlogo.cn/g?b=qq&nk=<QQ>&s=100``
- 群聊：``https://p.qlogo.cn/gh/<群号>/<群号>/100``

缓存策略：

- 落盘在 ``<插件数据目录>/avatars/<kind>_<id>.img``（存原始字节，不猜扩展名，
  返回时按文件头判 MIME）；
- 文件 mtime 超过 ``max_age_days`` 视为过期，下次请求时重取；``force=True`` 强制重取；
- 并发用信号量限制，整体有超时保护；
- **任何失败都不抛异常**：取不到就当作「没有头像」，前端退化为首字母色块。

（实现参考同作者的 astrbot_plugin_user_gateway/avatar.py，保持一致的行为与缓存布局。）
"""

from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from typing import Any, Iterable, Optional

from astrbot.api import logger

try:  # aiohttp 是 AstrBot 的运行期依赖；缺失时降级为「不抓头像」
    import aiohttp
except Exception:  # pragma: no cover
    aiohttp = None  # type: ignore

# 只允许数字 id 参与文件名拼接（防目录穿越）
_SAFE_ID = re.compile(r"[^0-9]")

# 用户头像：与 astrbot_plugin_box / astrbot_plugin_moe_star_whisper 同款官方接口
# （``headimg_dl`` 支持指定尺寸；spec 合法值只有 40 / 100 / 140 / 640，其它会返回 400）。
# 控制台列表头像按 46px 显示，取 140 已足够清晰、体积仅 ~6KB（640 会到 ~77KB）。
FRIEND_URL = "https://q4.qlogo.cn/headimg_dl?dst_uin={id}&spec=140"
# 群头像：OneBot 的 get_group_info 不返回群头像，只能按群号拼（与 user_gateway 一致）
GROUP_URL = "https://p.qlogo.cn/gh/{id}/{id}/140"
# 备用接口（q4 不通时回退，同为腾讯官方、内容一致）
FRIEND_URL_FALLBACK = "https://q1.qlogo.cn/g?b=qq&nk={id}&s=100"

MAX_BYTES = 512 * 1024  # 头像不该超过 512KB，超过视为异常响应


def safe_id(raw: Any) -> str:
    """把 id 规整成纯数字串（用于文件名与 URL 拼接）。非法 id 返回空串。"""
    return _SAFE_ID.sub("", str(raw or "").strip())[:20]


def avatar_url(kind: str, target_id: str) -> str:
    """头像的原始 CDN 地址（``kind``: ``user`` / ``group``）。"""
    tid = safe_id(target_id)
    if not tid:
        return ""
    return (GROUP_URL if kind == "group" else FRIEND_URL).format(id=tid)


def guess_mime(data: bytes) -> str:
    """按文件头判断图片类型（腾讯那边会返回 jpg 或 png）。"""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:5] == b"<svg " or data[:5] == b"<?xml":
        return "image/svg+xml"
    return "application/octet-stream"


def to_data_uri(data: bytes) -> str:
    """转成前端可直接塞进 ``<img src>`` 的 data URI。"""
    import base64

    return f"data:{guess_mime(data)};base64,{base64.b64encode(data).decode('ascii')}"


class AvatarCache:
    """头像磁盘缓存 + 抓取。生命周期与插件一致，无后台任务。"""

    def __init__(
        self,
        root: str | Path,
        *,
        timeout: float = 8.0,
        concurrency: int = 6,
        max_age_days: int = 30,
    ) -> None:
        self.root = Path(root)
        self.timeout = float(timeout)
        self.concurrency = max(1, int(concurrency))
        self.max_age_days = int(max_age_days)
        self._sem: Optional[asyncio.Semaphore] = None
        self._session: Any = None
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[cosyvoice] 头像缓存目录创建失败（头像将不可用）: {e}")

    # ------------------------------------------------------------------ #
    # 磁盘
    # ------------------------------------------------------------------ #
    def path(self, kind: str, target_id: str) -> Optional[Path]:
        tid = safe_id(target_id)
        if not tid:
            return None
        return self.root / f"{'group' if kind == 'group' else 'user'}_{tid}.img"

    def read(
        self, kind: str, target_id: str, *, max_age_days: Optional[int] = None
    ) -> Optional[bytes]:
        """读缓存；不存在或已过期返回 None。"""
        p = self.path(kind, target_id)
        if p is None or not p.exists():
            return None
        days = self.max_age_days if max_age_days is None else int(max_age_days)
        try:
            if days > 0:
                age = time.time() - p.stat().st_mtime
                if age > days * 86400:
                    return None
            return p.read_bytes() or None
        except Exception:  # noqa: BLE001
            return None

    def read_any(self, kind: str, target_id: str) -> Optional[bytes]:
        """读缓存，**不看过期**（离线时宁可给旧头像也不给空）。"""
        p = self.path(kind, target_id)
        try:
            return (p.read_bytes() or None) if p is not None and p.exists() else None
        except Exception:  # noqa: BLE001
            return None

    def write(self, kind: str, target_id: str, data: bytes) -> bool:
        p = self.path(kind, target_id)
        if p is None or not data:
            return False
        try:
            tmp = p.with_suffix(".tmp")
            tmp.write_bytes(data)
            tmp.replace(p)  # 原子替换：避免并发读到半个文件
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[cosyvoice] 头像写入失败（{kind}:{target_id}）: {e}")
            return False

    def drop(self, kind: str, target_id: str) -> bool:
        """删除某个头像缓存（「更新头像」先删再取）。"""
        p = self.path(kind, target_id)
        try:
            if p is not None and p.exists():
                p.unlink()
                return True
        except Exception:  # noqa: BLE001
            pass
        return False

    def stats(self) -> dict[str, Any]:
        """缓存概况：文件数、占用字节、最旧/最新时间。"""
        try:
            files = list(self.root.glob("*.img"))
        except Exception:  # noqa: BLE001
            files = []
        total = 0
        oldest = 0
        newest = 0
        for f in files:
            try:
                st = f.stat()
            except Exception:  # noqa: BLE001
                continue
            total += st.st_size
            oldest = st.st_mtime if not oldest else min(oldest, st.st_mtime)
            newest = max(newest, st.st_mtime)
        return {
            "dir": str(self.root),
            "count": len(files),
            "bytes": total,
            "oldest_at": int(oldest),
            "newest_at": int(newest),
            "max_age_days": self.max_age_days,
        }

    def cached_ids(self, kind: str) -> list[str]:
        """已缓存的该类 id 列表（「更新头像」时用它把本类型全部重取）。"""
        prefix = "group_" if kind == "group" else "user_"
        try:
            return [f.stem[len(prefix):] for f in self.root.glob(f"{prefix}*.img")]
        except Exception:  # noqa: BLE001
            return []

    # ------------------------------------------------------------------ #
    # 抓取
    # ------------------------------------------------------------------ #
    async def _get_session(self) -> Any:
        if aiohttp is None:
            return None
        if self._session is None or getattr(self._session, "closed", False):
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout),
                headers={"User-Agent": "AstrBot-CosyVoice/avatar"},
            )
        return self._session

    async def close(self) -> None:
        """关闭 HTTP 会话（插件 terminate 时调用）。"""
        s, self._session = self._session, None
        if s is not None:
            try:
                await s.close()
            except Exception:  # noqa: BLE001
                pass

    def _candidate_urls(self, kind: str, target_id: str) -> list[str]:
        """该头像的候选地址（按顺序尝试）：主接口 -> 备用接口。"""
        tid = safe_id(target_id)
        if not tid:
            return []
        urls = [avatar_url(kind, tid)]
        if kind != "group" and FRIEND_URL_FALLBACK:
            urls.append(FRIEND_URL_FALLBACK.format(id=tid))
        return [u for u in urls if u]

    async def fetch_one(self, kind: str, target_id: str) -> Optional[bytes]:
        """从腾讯 CDN 抓一个头像（不落盘）；主接口失败会依次试备用接口。失败返回 None。"""
        urls = self._candidate_urls(kind, target_id)
        if not urls:
            return None
        session = await self._get_session()
        if session is None:
            return None
        if self._sem is None:
            self._sem = asyncio.Semaphore(self.concurrency)
        for url in urls:
            try:
                async with self._sem:
                    async with session.get(url) as resp:
                        if resp.status != 200:
                            continue
                        data = await resp.content.read(MAX_BYTES + 1)
            except Exception as e:  # noqa: BLE001  网络问题一律静默（列表不能因为头像挂掉）
                logger.debug(f"[cosyvoice] 抓取头像失败 {kind}:{target_id} {url}: {e}")
                continue
            if not data or len(data) > MAX_BYTES:
                continue
            if guess_mime(data) == "application/octet-stream":
                continue  # 不是图片（多半是错误页）
            return data
        return None

    async def ensure(
        self,
        kind: str,
        ids: Iterable[Any],
        *,
        force: bool = False,
        max_age_days: Optional[int] = None,
    ) -> dict[str, bytes]:
        """保证给定 id 的头像可用，返回 ``{id: bytes}``（取不到的 id 不出现）。

        Args:
            force: 为 True 时忽略已有缓存全部重取（「更新头像」按钮）。
            max_age_days: 覆盖默认过期天数；``0`` 表示永不过期。
        """
        result: dict[str, bytes] = {}
        todo: list[str] = []
        for raw in ids:
            tid = safe_id(raw)
            if not tid or tid in result:
                continue
            if force:
                todo.append(tid)
                continue
            cached = self.read(kind, tid, max_age_days=max_age_days)
            if cached:
                result[tid] = cached
            else:
                todo.append(tid)
        if not todo:
            return result

        async def one(tid: str) -> None:
            data = await self.fetch_one(kind, tid)
            if data:
                self.write(kind, tid, data)
                result[tid] = data
            elif not force:
                # 抓不到但本地有过期缓存 → 用旧的兜底（离线可用）
                stale = self.read_any(kind, tid)
                if stale:
                    result[tid] = stale

        await asyncio.gather(*(one(t) for t in todo), return_exceptions=True)
        return result

    async def ensure_map(
        self,
        kind: str,
        ids: Iterable[Any],
        *,
        force: bool = False,
        max_age_days: Optional[int] = None,
    ) -> dict[str, str]:
        """同 :meth:`ensure`，但返回 ``{id: data URI}``，可直接给前端。"""
        raw = await self.ensure(kind, ids, force=force, max_age_days=max_age_days)
        return {tid: to_data_uri(data) for tid, data in raw.items()}
