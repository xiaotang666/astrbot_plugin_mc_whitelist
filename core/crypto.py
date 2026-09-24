"""AES-128-ECB 加解密 —— 与模组端 Java 实现逐字节等价。

契约（docs/接口契约冻结_v0.3.md §1.2 规则 5）：
    key  = aes_key 的 UTF-8 字节取前 16、不足补 0x00
    算法 = AES-128-ECB + PKCS7（Java 侧 PKCS5Padding 对 AES 等价）+ Base64

Java 侧等价实现：
    byte[] padded = new byte[16];
    System.arraycopy(keyBytes, 0, padded, 0, Math.min(keyBytes.length, 16));
    Cipher.getInstance("AES/ECB/PKCS5Padding")
"""

from __future__ import annotations

import base64

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

BLOCK_SIZE = 16


class CryptoError(Exception):
    """加解密失败（密钥错误、密文损坏、填充非法）。"""


class AESCrypto:
    """AES-128-ECB + PKCS7 + Base64。"""

    def __init__(self, key: str) -> None:
        if not isinstance(key, str) or not key:
            raise CryptoError("AES 密钥不能为空")
        raw = key.encode("utf-8")
        if len(raw) < BLOCK_SIZE:
            # 与 Java 一致：不足 16 字节补 0x00
            raw = raw.ljust(BLOCK_SIZE, b"\x00")
        self.key_bytes: bytes = raw[:BLOCK_SIZE]
        self._encryptor_factory = lambda: Cipher(  # noqa: E731
            algorithms.AES(self.key_bytes), modes.ECB()
        )

    # ---- 便于与其它实现比对 ----
    @property
    def key_hex(self) -> str:
        """密钥的十六进制形式（供 openssl / 模组侧比对，非机密泄漏点在调试日志中屏蔽）。"""
        return self.key_bytes.hex()

    # ---- PKCS7 ----
    @staticmethod
    def _pad(data: bytes) -> bytes:
        n = BLOCK_SIZE - (len(data) % BLOCK_SIZE)
        return data + bytes([n]) * n

    @staticmethod
    def _unpad(data: bytes) -> bytes:
        if not data or len(data) % BLOCK_SIZE:
            raise CryptoError("密文长度不是 16 的整数倍")
        n = data[-1]
        if not 1 <= n <= BLOCK_SIZE or data[-n:] != bytes([n]) * n:
            raise CryptoError("PKCS7 填充非法（通常是密钥不匹配）")
        return data[:-n]

    # ---- 对外 ----
    def encrypt(self, plaintext: str) -> str:
        data = self._pad(plaintext.encode("utf-8"))
        enc = self._encryptor_factory().encryptor()
        return base64.b64encode(enc.update(data) + enc.finalize()).decode("ascii")

    def decrypt(self, ciphertext: str) -> str:
        try:
            data = base64.b64decode(ciphertext, validate=True)
        except Exception as exc:  # noqa: BLE001
            raise CryptoError(f"Base64 解码失败: {exc}") from exc
        dec = self._encryptor_factory().decryptor()
        try:
            plain = self._unpad(dec.update(data) + dec.finalize())
        except CryptoError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise CryptoError(f"解密失败: {exc}") from exc
        try:
            return plain.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CryptoError(f"UTF-8 解码失败: {exc}") from exc
