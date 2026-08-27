"""通用翻译适配器：零依赖语种检测 + 高度可配置的翻译 API 接入。

设计目标：把「任意翻译服务」抽象成一个完全可配置的适配器，配置项不缺胳膊少腿：
  - 请求方法 GET / POST
  - URL
  - 认证：apikey + 认证头名（默认 Authorization）+ 认证 scheme（默认 Bearer）
  - 额外请求头（key/value 列表）
  - 请求参数模板（GET 走 query；POST 走 body，支持 json / form），值支持占位符
    {text}（待翻译文本）/ {source}（源语言）/ {target}（目标语言）
  - 响应解析路径（点号 + 数组下标，如 data.trans_result[0].dst）从响应 JSON 取译文
  - 译文缓存，避免同一段文本重复打 API
  - 任何失败都兜底返回原文，绝不影响语音合成主流程

语种判定走本地 Unicode 脚本检测（detect_lang），零依赖、不消耗 API；
仅覆盖主流脚本（中/日/韩/俄/泰/阿/梵/英…），拉丁系内部不细分（统一归 en）。
"""

import re

import httpx
from astrbot.api import logger

_LANG_ZH = "zh"
_LANG_EN = "en"


def detect_lang(text: str) -> str:
    """本地 Unicode 脚本语种检测（零依赖、不消耗 API）。

    只覆盖日常常见的几个脚本；无法细分拉丁系内部（en/fr/es… 统一归 en）。
    """
    if not text or not text.strip():
        return _LANG_EN
    has_cjk = has_kana = has_hangul = False
    has_cyrillic = has_thai = has_arabic = has_deva = False
    for ch in text:
        cp = ord(ch)
        if 0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF or 0xF900 <= cp <= 0xFAFF:
            has_cjk = True
        elif 0x3040 <= cp <= 0x309F or 0x30A0 <= cp <= 0x30FF:
            has_kana = True
        elif 0xAC00 <= cp <= 0xD7A3 or 0x1100 <= cp <= 0x11FF:
            has_hangul = True
        elif 0x0400 <= cp <= 0x04FF:
            has_cyrillic = True
        elif 0x0E00 <= cp <= 0x0E7F:
            has_thai = True
        elif 0x0600 <= cp <= 0x06FF or 0x0750 <= cp <= 0x077F:
            has_arabic = True
        elif 0x0900 <= cp <= 0x097F:
            has_deva = True
    # 优先级：含汉字又含假名 → 日文；含汉字又含谚文 → 韩文
    if has_cjk and has_kana:
        return "ja"
    if has_cjk and has_hangul:
        return "ko"
    if has_kana:
        return "ja"
    if has_hangul:
        return "ko"
    if has_cjk:
        return _LANG_ZH
    if has_cyrillic:
        return "ru"
    if has_thai:
        return "th"
    if has_arabic:
        return "ar"
    if has_deva:
        return "hi"
    return _LANG_EN


class Translator:
    """可配置翻译适配器。配置来自插件自有 data/translate_config.json（热更新）。"""

    def __init__(self, config: dict | None = None):
        self.reload(config or {})

    def reload(self, config: dict):
        """重新加载配置。调用方在保存翻译配置 / 刷新配置时触发。"""
        self.enabled = bool(config.get("enabled", False))
        self.target = (config.get("target") or "zh").strip().lower() or "zh"
        self.source = [str(x).strip().lower() for x in (config.get("source") or []) if str(x).strip()]
        api = config.get("api") or {}
        self.api_url = (api.get("url") or "").strip()
        self.api_method = (api.get("method") or "POST").strip().upper()
        self.apikey = api.get("apikey") or ""
        self.auth_header = (api.get("auth_header") or "Authorization").strip()
        self.auth_scheme = (api.get("auth_scheme") or "Bearer").strip()
        self.content_type = (api.get("content_type") or "json").strip().lower()
        self.extra_headers = [
            {"key": h.get("key", ""), "value": h.get("value", "")}
            for h in (api.get("extra_headers") or [])
            if h.get("key")
        ]
        self.params = [
            {"key": p.get("key", ""), "value": p.get("value", "")}
            for p in (api.get("params") or [])
            if p.get("key")
        ]
        self.response_path = (api.get("response_path") or "").strip()
        self.timeout = float(api.get("timeout") or 15)
        # 语种代码映射：简码 → 接口要求的代码（如 zh → zh-CN）；同时作用于 {source}/{target}
        self.lang_map = {
            str(k).strip().lower(): str(v).strip()
            for k, v in (config.get("lang_map") or [])
            if str(k).strip() and str(v).strip()
        }
        # 译文缓存：origin -> translated（配置热更新时清空，避免旧译文滞留）
        self._cache: dict = {}

    # ------------------------------------------------------------------ #
    # 对外入口
    # ------------------------------------------------------------------ #
    async def maybe_translate(self, text: str, target: str | None = None) -> str:
        """需要翻译才翻译，否则原样返回；任何异常都回退原文。

        target：目标语种，覆盖配置里的全局 target。合成时由调用方传入所选
        音色的语种，实现「中文 → 音色语种」；为 None 时回落到全局 target。
        """
        if not self.enabled or not self.api_url:
            return text
        dst = (target or self.target).strip().lower() or self.target
        src = detect_lang(text)
        if not self._should_translate(src, dst):
            return text
        if text in self._cache:
            return self._cache[text]
        try:
            translated = await self._call_api(text, src, dst)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[cosyvoice-translate] 翻译失败，回退原文: {e}")
            return text
        if not translated or not translated.strip():
            return text
        self._cache[text] = translated
        return translated

    def _should_translate(self, src: str, dst: str) -> bool:
        """是否需要对 src 语种翻译（dst 为目标语种）：

        - src 就是目标语言 → 不翻
        - 未配置 source 白名单 → 除目标语言外全部翻
        - 配置了 source 白名单 → 仅白名单内的语种翻
        """
        if src == dst:
            return False
        if not self.source:
            return True
        return src in self.source

    # ------------------------------------------------------------------ #
    # 请求构建 + 调用
    # ------------------------------------------------------------------ #
    async def _call_api(self, text: str, src: str, dst: str) -> str:
        # 语种代码映射：把简码换成接口要求的代码（zh → zh-CN 等）
        src = self.lang_map.get(src, src)
        dst = self.lang_map.get(dst, dst)
        def fill(v):
            return (
                str(v)
                .replace("{text}", text)
                .replace("{source}", src)
                .replace("{target}", dst)
            )

        headers = {}
        if self.apikey:
            headers[self.auth_header] = (
                f"{self.auth_scheme} {self.apikey}" if self.auth_scheme else self.apikey
            )
        for h in self.extra_headers:
            headers[h["key"]] = fill(h["value"])

        params = {p["key"]: fill(p["value"]) for p in self.params}
        query = params if self.api_method == "GET" else {}
        body = params if self.api_method != "GET" else None

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            if self.api_method == "GET":
                resp = await client.get(self.api_url, params=query, headers=headers)
            elif self.content_type == "form":
                resp = await client.post(self.api_url, data=body, headers=headers)
            else:
                resp = await client.post(self.api_url, json=body, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        return self._extract(data)

    def _extract(self, data) -> str:
        if not self.response_path:
            if isinstance(data, dict):
                for k in ("result", "translated", "translation", "text", "dst"):
                    if k in data and isinstance(data[k], str):
                        return data[k]
            raise ValueError("未配置 response_path 且无法自动识别译文")
        val = self._extract_path(data, self.response_path)
        if not isinstance(val, str):
            raise ValueError(f"response_path 取值非字符串: {val!r}")
        return val

    @staticmethod
    def _extract_path(data, path: str) -> object:
        """解析 data.trans_result[0].dst 这类路径。"""
        cur = data
        for name, idx in re.findall(r"([^.\[\]]+)|\[(\d+)\]", path):
            if name:
                cur = cur[name]
            elif idx != "":
                cur = cur[int(idx)]
        return cur

    # ------------------------------------------------------------------ #
    # 测试辅助（WebUI 翻译配置面板「测试」按钮用）
    # ------------------------------------------------------------------ #
    async def test(self, sample: str) -> dict:
        """用当前配置真实调用一次翻译，返回诊断信息（不写缓存）。"""
        if not self.api_url:
            return {"ok": False, "error": "未配置翻译 API 的 URL"}
        if not self.enabled:
            return {"ok": False, "error": "翻译开关未开启"}
        src = detect_lang(sample)
        dst = self.target
        info = {
            "detected_lang": src,
            "target_lang": dst,
            "should_translate": self._should_translate(src, dst),
        }
        if not info["should_translate"]:
            return {"ok": True, "skipped": True, **info, "result": sample}
        try:
            translated = await self._call_api(sample, src, dst)
            info["result"] = translated
            info["ok"] = True
        except Exception as e:  # noqa: BLE001
            info["ok"] = False
            info["error"] = str(e)
        return info
