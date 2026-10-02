"""确定性 UID 映射。

同一 (策略规范化 JSON, 原始 UID, UID 类别) 永远派生相同的新 UID，
从而保证“同输入与策略字节一致”，同时无法直接从新 UID 反推原值
（HMAC-SHA256 截断 128 bit）。

输出形如 ``2.25.<128bit 十进制整数>``，总长不超过 44 字符，满足
DICOM UI 64 字符上限。策略可指定自定义 UID 根。
"""

from __future__ import annotations

import hashlib
import hmac

from pydicom.uid import UID

MAX_UI_LENGTH = 64
_HASH_BYTES = 16  # 128 bit；十进制最多 39 位，2.25. 前缀下总长 <= 44


class UidRootError(ValueError):
    pass


def build_key(policy_canonical: bytes) -> bytes:
    """策略派生密钥：规范化策略 JSON 的 SHA-256。"""
    return hashlib.sha256(policy_canonical).digest()


def validate_uid_root(root: str) -> None:
    if not root or not root.replace(".", "").isdecimal():
        raise UidRootError(f"uid_root 必须是点分十进制: {root!r}")
    if any(p.startswith("0") and p != "0" for p in root.split(".")):
        raise UidRootError(f"uid_root 含非法前导零的弧: {root!r}")
    # 留出 '.' + 最多 39 位十进制数字的空间
    if len(root) + 1 + 39 > MAX_UI_LENGTH:
        raise UidRootError(f"uid_root 过长，无法保证 UID <= 64 字符: {root!r}")
    if not UID(root).is_valid:
        raise UidRootError(f"uid_root 不是合法 DICOM UID: {root!r}")


def _derive_integer(key: bytes, kind: str, original: str) -> int:
    msg = kind.encode("ascii") + b"\x00" + original.encode("utf-8")
    digest = hmac.new(key, msg, hashlib.sha256).digest()[:_HASH_BYTES]
    n = int.from_bytes(digest, "big")
    # 避免 0（虽然概率可忽略），保证非空、无前导零的十进制弧
    return n if n else 1


class UidMapper:
    """按类别记忆派生结果，相同原始 UID 在同类别下始终得到同一新 UID。"""

    def __init__(self, key: bytes, uid_root: str):
        validate_uid_root(uid_root)
        self._key = key
        self._root = uid_root
        self._maps: dict[str, dict[str, str]] = {
            "study": {},
            "series": {},
            "instance": {},
            "frame": {},
            "ui": {},
        }

    def map_uid(self, kind: str, original: str) -> str:
        if kind not in self._maps:
            raise KeyError(f"未知 UID 映射类别: {kind}")
        table = self._maps[kind]
        if original not in table:
            n = _derive_integer(self._key, kind, original)
            table[original] = f"{self._root}.{n}"
        return table[original]

    def audit_tables(self) -> dict[str, dict[str, str]]:
        return {k: dict(v) for k, v in self._maps.items()}
