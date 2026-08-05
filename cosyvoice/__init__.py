"""CosyVoice 语音合成客户端包：单机客户端与多机负载均衡路由。"""

from .client import CosyVoiceClient, CosyVoiceServerError
from .router import CosyVoiceRouter

__all__ = ["CosyVoiceClient", "CosyVoiceServerError", "CosyVoiceRouter"]
