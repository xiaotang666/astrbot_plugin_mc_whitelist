"""协议层自测：python tests/test_protocol.py

覆盖：
1. 密钥派生与模组 Java 实现一致（UTF-8 取前 16 补 0）
2. 与 openssl 的 AES-128-ECB 输出逐字节一致（跨实现验证，等价于 JDK AES/ECB/PKCS5Padding）
3. 加解密往返、错误密钥、非法密文
4. 信封编码/解码（加密模式 + 明文模式）
5. 前缀必填、proto_version 校验、msg_id 去重
"""

from __future__ import annotations

import base64
import json
import subprocess
import sys

import _loader  # noqa: F401  (把 tests/ 加进 sys.path)

_loader.bootstrap()

from astrbot_plugin_mc_whitelist.core.crypto import AESCrypto, CryptoError  # noqa: E402
from astrbot_plugin_mc_whitelist.core.protocol import (  # noqa: E402
    PROTO_VERSION,
    Envelope,
    MsgIdCache,
    MsgType,
    ProtocolError,
)

PASSED: list[str] = []
FAILED: list[tuple[str, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASSED.append(name)
    else:
        FAILED.append((name, detail))


def expect_raises(name: str, exc_type: type[BaseException], fn, *a, **kw) -> None:
    try:
        fn(*a, **kw)
    except exc_type:
        PASSED.append(name)
    except BaseException as e:  # noqa: BLE001
        FAILED.append((name, f"抛出了 {type(e).__name__}: {e}"))
    else:
        FAILED.append((name, "未抛出异常"))


# ---------------------------------------------------------------- 1. 密钥派生
KEY32 = "your_32_byte_key_here_12345678"  # 模组文档 §17.4 示例里的 32 字节密钥
c = AESCrypto(KEY32)
check("密钥取前16字节", c.key_bytes == b"your_32_byte_key", c.key_bytes)
check("32字节密钥等价于前16字节", AESCrypto(KEY32).key_hex == AESCrypto("your_32_byte_key").key_hex)
check("短密钥补0", AESCrypto("abc").key_bytes == b"abc" + b"\x00" * 13, AESCrypto("abc").key_bytes)
check("中文密钥按UTF-8截断", AESCrypto("密钥密钥密钥密钥密钥密钥密钥密钥密钥").key_bytes == "密钥密钥密钥密钥密钥密钥密钥密钥密钥".encode()[:16])

# ---------------------------------------------------------------- 2. openssl 交叉验证
PLAIN = "你好 MC v10.3 — chat payload"
b64 = c.encrypt(PLAIN)
try:
    out = subprocess.run(
        ["openssl", "enc", "-aes-128-ecb", "-K", c.key_hex, "-base64", "-A"],
        input=PLAIN.encode("utf-8"),
        capture_output=True,
        timeout=20,
    )
    if out.returncode != 0:
        FAILED.append(("openssl 交叉验证", out.stderr.decode(errors="replace")[:200]))
    else:
        expected = out.stdout.decode().strip()
        check("openssl AES-128-ECB 逐字节一致", b64 == expected, f"\n  python : {b64}\n  openssl: {expected}")
        # 反向：openssl 加密 → python 解密
        dec = subprocess.run(
            ["openssl", "enc", "-d", "-aes-128-ecb", "-K", c.key_hex, "-base64", "-A"],
            input=b64.encode(),
            capture_output=True,
            timeout=20,
        )
        check("openssl 解密 python 密文", dec.stdout.decode("utf-8") == PLAIN, dec.stderr.decode(errors="replace")[:200])
except FileNotFoundError:
    print("[skip] 未找到 openssl，跳过跨实现验证")

# ---------------------------------------------------------------- 3. 加解密边界
check("往返(含中文/emoji)", c.decrypt(c.encrypt("测试 🎮 123 abc")) == "测试 🎮 123 abc")
check("往返(空串)", c.decrypt(c.encrypt("")) == "")
check("往返(正好16字节)", c.decrypt(c.encrypt("a" * 16)) == "a" * 16)
check("往返(长文本)", c.decrypt(c.encrypt("x" * 5000)) == "x" * 5000)
expect_raises("错误密钥无法解密", CryptoError, AESCrypto("another_key_16b").decrypt, b64)
expect_raises("非法Base64", CryptoError, c.decrypt, "!!!not-base64!!!")
expect_raises("非法密文长度", CryptoError, c.decrypt, base64.b64encode(b"short").decode())
expect_raises("空密钥", CryptoError, AESCrypto, "")

# ---------------------------------------------------------------- 4. 信封
enc = Envelope(crypto=AESCrypto(KEY32), prefix="[MC]")
raw = enc.encode(MsgType.CHAT, {"source": "qq", "sender": "小坣", "content": "hi", "server": "生存服"})
outer = json.loads(raw)
check("外层含 type", outer.get("type") == "chat", raw[:120])
check("外层含 seq", outer.get("seq") == 1)
check("外层含 msg_id", bool(outer.get("msg_id")))
check("外层含 proto_version", outer.get("proto_version") == PROTO_VERSION)
check("外层含 encrypted", isinstance(outer.get("encrypted"), str))
check("外层无明文 data", "data" not in outer)

msg = enc.decode(raw)
check("解码 type", msg.type == "chat")
check("解码 data", msg.data.get("sender") == "小坣")
check("解码 encrypted=True", msg.encrypted is True)
check("seq 自增", json.loads(enc.encode(MsgType.CHAT, {}))["seq"] == 2)

# 明文模式（none/prefix）
plain_env = Envelope(crypto=None, prefix="[MC]")
praw = plain_env.encode(MsgType.PLAYER_EVENT, {"event": "join", "player": "Steve", "target_groups": [123]})
pouter = json.loads(praw)
check("明文模式套外层", pouter.get("prefix") == "[MC]" and pouter.get("data", {}).get("player") == "Steve")
check("明文模式解码", plain_env.decode(praw).data.get("target_groups") == [123])

# 前缀必填（契约 §1.2 规则 1）
bad = json.dumps(
    {
        "type": "chat",
        "seq": 1,
        "msg_id": "x",
        "proto_version": 1,
        "prefix": "",
        "data": {},
    }
)
expect_raises("前缀缺失被拒收", ProtocolError, plain_env.decode, bad)
bad2 = json.dumps({"type": "chat", "seq": 1, "proto_version": 1, "prefix": "[其他]", "data": {}})
expect_raises("前缀不匹配被拒收", ProtocolError, plain_env.decode, bad2)

# proto_version 不兼容
bad3 = json.dumps({"type": "chat", "seq": 1, "proto_version": 99, "prefix": "[MC]", "data": {}})
expect_raises("协议版本不兼容被拒收", ProtocolError, plain_env.decode, bad3)

# 解密失败（密钥不匹配）
other = Envelope(crypto=AESCrypto("another_key_16b"), prefix="[MC]")
expect_raises("密钥不匹配解码失败", ProtocolError, other.decode, raw)

# ---------------------------------------------------------------- 5. 去重
cache = MsgIdCache(capacity=4)
check("首次不重复", cache.is_duplicate("a") is False)
check("二次重复", cache.is_duplicate("a") is True)
check("None 不算重复", cache.is_duplicate(None) is False and cache.is_duplicate(None) is False)
for i in range(10):
    cache.is_duplicate(f"id{i}")
check("LRU 容量生效", len(cache) <= 4, str(len(cache)))
check("被淘汰的不再算重复", cache.is_duplicate("a") is False)

# ---------------------------------------------------------------- 结果
print(f"\n通过 {len(PASSED)} 项")
for name in PASSED:
    print(f"  ✅ {name}")
if FAILED:
    print(f"\n失败 {len(FAILED)} 项")
    for name, detail in FAILED:
        print(f"  ❌ {name}\n     {detail}")
    sys.exit(1)
print("\n全部通过 ✅")
