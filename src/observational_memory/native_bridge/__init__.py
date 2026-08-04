"""Secure, LLM-free native-memory bridge."""

from .bridge import BridgePolicy, BridgeResult, NativeMemoryBridge
from .profiles import STRICT_DEFAULT_PROFILE

__all__ = [
    "BridgePolicy",
    "BridgeResult",
    "NativeMemoryBridge",
    "STRICT_DEFAULT_PROFILE",
]
