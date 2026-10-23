"""
零依赖 AES 实现（AES-128/192/256 + CBC + PKCS7）

用途：企业微信 / 飞书回调消息解密。标准库无 AES，本项目坚持"零硬依赖"，
因此这里用纯 Python 实现一份正确、可测的 AES。若环境中装有 `pycryptodome`，
高层封装会自动优先使用它（更快），无则用本实现兜底。

正确性保证：用 NIST FIPS-197 标准测试向量自测（见 __main__ / tests）。
"""

from __future__ import annotations

import base64
import os
import struct

# ---- AES S-box ----
_SBOX = bytes(
    [
        0x63, 0x7C, 0x77, 0x7B, 0xF2, 0x6B, 0x6F, 0xC5, 0x30, 0x01, 0x67, 0x2B, 0xFE, 0xD7, 0xAB, 0x76,
        0xCA, 0x82, 0xC9, 0x7D, 0xFA, 0x59, 0x47, 0xF0, 0xAD, 0xD4, 0xA2, 0xAF, 0x9C, 0xA4, 0x72, 0xC0,
        0xB7, 0xFD, 0x93, 0x26, 0x36, 0x3F, 0xF7, 0xCC, 0x34, 0xA5, 0xE5, 0xF1, 0x71, 0xD8, 0x31, 0x15,
        0x04, 0xC7, 0x23, 0xC3, 0x18, 0x96, 0x05, 0x9A, 0x07, 0x12, 0x80, 0xE2, 0xEB, 0x27, 0xB2, 0x75,
        0x09, 0x83, 0x2C, 0x1A, 0x1B, 0x6E, 0x5A, 0xA0, 0x52, 0x3B, 0xD6, 0xB3, 0x29, 0xE3, 0x2F, 0x84,
        0x53, 0xD1, 0x00, 0xED, 0x20, 0xFC, 0xB1, 0x5B, 0x6A, 0xCB, 0xBE, 0x39, 0x4A, 0x4C, 0x58, 0xCF,
        0xD0, 0xEF, 0xAA, 0xFB, 0x43, 0x4D, 0x33, 0x85, 0x45, 0xF9, 0x02, 0x7F, 0x50, 0x3C, 0x9F, 0xA8,
        0x51, 0xA3, 0x40, 0x8F, 0x92, 0x9D, 0x38, 0xF5, 0xBC, 0xB6, 0xDA, 0x21, 0x10, 0xFF, 0xF3, 0xD2,
        0xCD, 0x0C, 0x13, 0xEC, 0x5F, 0x97, 0x44, 0x17, 0xC4, 0xA7, 0x7E, 0x3D, 0x64, 0x5D, 0x19, 0x73,
        0x60, 0x81, 0x4F, 0xDC, 0x22, 0x2A, 0x90, 0x88, 0x46, 0xEE, 0xB8, 0x14, 0xDE, 0x5E, 0x0B, 0xDB,
        0xE0, 0x32, 0x3A, 0x0A, 0x49, 0x06, 0x24, 0x5C, 0xC2, 0xD3, 0xAC, 0x62, 0x91, 0x95, 0xE4, 0x79,
        0xE7, 0xC8, 0x37, 0x6D, 0x8D, 0xD5, 0x4E, 0xA9, 0x6C, 0x56, 0xF4, 0xEA, 0x65, 0x7A, 0xAE, 0x08,
        0xBA, 0x78, 0x25, 0x2E, 0x1C, 0xA6, 0xB4, 0xC6, 0xE8, 0xDD, 0x74, 0x1F, 0x4B, 0xBD, 0x8B, 0x8A,
        0x70, 0x3E, 0xB5, 0x66, 0x48, 0x03, 0xF6, 0x0E, 0x61, 0x35, 0x57, 0xB9, 0x86, 0xC1, 0x1D, 0x9E,
        0xE1, 0xF8, 0x98, 0x11, 0x69, 0xD9, 0x8E, 0x94, 0x9B, 0x1E, 0x87, 0xE9, 0xCE, 0x55, 0x28, 0xDF,
        0x8C, 0xA1, 0x89, 0x0D, 0xBF, 0xE6, 0x42, 0x68, 0x41, 0x99, 0x2D, 0x0F, 0xB0, 0x54, 0xBB, 0x16,
    ]
)


def _xtime(a: int) -> int:
    a <<= 1
    if a & 0x100:
        a ^= 0x11B
    return a & 0xFF


def _gmul(a: int, b: int) -> int:
    p = 0
    for _ in range(8):
        if b & 1:
            p ^= a
        hi = a & 0x80
        a = (a << 1) & 0xFF
        if hi:
            a ^= 0x1B
        b >>= 1
    return p


class _AESCore:
    def __init__(self, key: bytes):
        if len(key) not in (16, 24, 32):
            raise ValueError("AES 密钥长度必须为 16/24/32 字节")
        self.Nk = len(key) // 4
        self.Nr = self.Nk + 6
        self._w = self._expand_key(key)

    @staticmethod
    def _expand_key(key: bytes):
        Nk = len(key) // 4
        Nr = Nk + 6
        RCON = [0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36]
        w = [list(key[4 * i : 4 * i + 4]) for i in range(Nk)]
        for i in range(Nk, 4 * (Nr + 1)):
            temp = list(w[i - 1])
            if i % Nk == 0:
                temp = temp[1:] + temp[:1]            # RotWord
                temp = [_SBOX[b] for b in temp]        # SubWord
                temp[0] ^= RCON[i // Nk - 1]
            elif Nk > 6 and i % Nk == 4:
                temp = [_SBOX[b] for b in temp]
            w.append([w[i - Nk][j] ^ temp[j] for j in range(4)])
        return w

    @staticmethod
    def _bytes2state(b: bytes):
        return [[b[r + 4 * c] for c in range(4)] for r in range(4)]

    @staticmethod
    def _state2bytes(s):
        out = bytearray(16)
        for r in range(4):
            for c in range(4):
                out[r + 4 * c] = s[r][c]
        return bytes(out)

    def _encrypt_block(self, block: bytes) -> bytes:
        w, Nr = self._w, self.Nr
        state = self._bytes2state(block)
        # AddRoundKey(0)
        for c in range(4):
            for r in range(4):
                state[r][c] ^= w[c][r]
        for rnd in range(1, Nr):
            # SubBytes
            state = [[_SBOX[state[r][c]] for c in range(4)] for r in range(4)]
            # ShiftRows
            state = [
                [state[r][(c + r) % 4] for c in range(4)] for r in range(4)
            ]
            # MixColumns
            for c in range(4):
                s0, s1, s2, s3 = state[0][c], state[1][c], state[2][c], state[3][c]
                state[0][c] = _gmul(s0, 2) ^ _gmul(s1, 3) ^ s2 ^ s3
                state[1][c] = s0 ^ _gmul(s1, 2) ^ _gmul(s2, 3) ^ s3
                state[2][c] = s0 ^ s1 ^ _gmul(s2, 2) ^ _gmul(s3, 3)
                state[3][c] = _gmul(s0, 3) ^ s1 ^ s2 ^ _gmul(s3, 2)
            # AddRoundKey(rnd)
            for c in range(4):
                for r in range(4):
                    state[r][c] ^= w[4 * rnd + c][r]
        # 末轮（无 MixColumns）
        state = [[_SBOX[state[r][c]] for c in range(4)] for r in range(4)]
        state = [[state[r][(c + r) % 4] for c in range(4)] for r in range(4)]
        for c in range(4):
            for r in range(4):
                state[r][c] ^= w[4 * Nr + c][r]
        return self._state2bytes(state)

    def _decrypt_block(self, block: bytes) -> bytes:
        w, Nr = self._w, self.Nr
        state = self._bytes2state(block)
        # AddRoundKey(Nr)
        for c in range(4):
            for r in range(4):
                state[r][c] ^= w[4 * Nr + c][r]
        for rnd in range(Nr - 1, 0, -1):
            # InvShiftRows
            state = [
                [state[r][(c - r) % 4] for c in range(4)] for r in range(4)
            ]
            # InvSubBytes
            state = [[_SBOX_INV[state[r][c]] for c in range(4)] for r in range(4)]
            # AddRoundKey(rnd)
            for c in range(4):
                for r in range(4):
                    state[r][c] ^= w[4 * rnd + c][r]
            # InvMixColumns
            for c in range(4):
                s0, s1, s2, s3 = state[0][c], state[1][c], state[2][c], state[3][c]
                state[0][c] = (
                    _gmul(s0, 14) ^ _gmul(s1, 11) ^ _gmul(s2, 13) ^ _gmul(s3, 9)
                )
                state[1][c] = (
                    _gmul(s0, 9) ^ _gmul(s1, 14) ^ _gmul(s2, 11) ^ _gmul(s3, 13)
                )
                state[2][c] = (
                    _gmul(s0, 13) ^ _gmul(s1, 9) ^ _gmul(s2, 14) ^ _gmul(s3, 11)
                )
                state[3][c] = (
                    _gmul(s0, 11) ^ _gmul(s1, 13) ^ _gmul(s2, 9) ^ _gmul(s3, 14)
                )
        # 末轮（无 InvMixColumns）
        state = [[state[r][(c - r) % 4] for c in range(4)] for r in range(4)]
        state = [[_SBOX_INV[state[r][c]] for c in range(4)] for r in range(4)]
        for c in range(4):
            for r in range(4):
                state[r][c] ^= w[c][r]
        return self._state2bytes(state)


# 逆 S-box（仅解密需要）
_SBOX_INV = bytes(range(256))
for _i in range(256):
    _SBOX_INV = bytes(
        _SBOX_INV[: _SBOX.index(_i)] + bytes([_i]) + _SBOX_INV[_SBOX.index(_i) + 1 :]
    ) if False else _SBOX_INV
# 上面的推导写法有误，改为直接计算逆表
_SBOX_INV = bytearray(256)
for _i in range(256):
    _SBOX_INV[_SBOX[_i]] = _i
_SBOX_INV = bytes(_SBOX_INV)


def _get_core(key: bytes) -> _AESCore:
    return _AESCore(key)


# ---------------- CBC 模式 ----------------
def aes_cbc_encrypt(key: bytes, iv: bytes, plaintext: bytes) -> bytes:
    """AES-CBC 加密（支持 128/192/256）。iv 长度须为 16 字节。"""
    if len(iv) != 16:
        raise ValueError("iv 必须为 16 字节")
    core = _get_core(key)
    pad = 16 - (len(plaintext) % 16)
    data = plaintext + bytes([pad]) * pad
    out = bytearray()
    prev = iv
    for i in range(0, len(data), 16):
        block = bytes(a ^ b for a, b in zip(data[i : i + 16], prev))
        enc = core._encrypt_block(block)
        out += enc
        prev = enc
    return bytes(out)


def aes_cbc_decrypt(key: bytes, iv: bytes, ciphertext: bytes) -> bytes:
    """AES-CBC 解密。ciphertext 长度须为 16 的倍数。"""
    if len(iv) != 16:
        raise ValueError("iv 必须为 16 字节")
    if len(ciphertext) % 16 != 0:
        raise ValueError("密文长度须为 16 的倍数")
    core = _get_core(key)
    out = bytearray()
    prev = iv
    for i in range(0, len(ciphertext), 16):
        block = ciphertext[i : i + 16]
        dec = core._decrypt_block(block)
        out += bytes(a ^ b for a, b in zip(dec, prev))
        prev = block
    # 去 PKCS7 填充
    if not out:
        return b""
    pad = out[-1]
    if 1 <= pad <= 16 and out[-pad:] == bytes([pad]) * pad:
        return bytes(out[:-pad])
    return bytes(out)


# ---------------- 平台高层封装 ----------------
def _b64decode_key_with_pad(aes_key: str) -> bytes:
    """EncodingAESKey 缺 '='，补上后再 base64 解码。"""
    return base64.b64decode(aes_key + "=")


def _strip_platform_plain(plain: bytes, receive_id: str) -> bytes:
    """企业微信/飞书明文格式：random(16) + msg_len(4, BE) + msg + receiveid。
    校验 receive_id 后返回 msg 字节。"""
    if len(plain) < 20:
        raise ValueError("明文长度不足")
    msg_len = struct.unpack(">I", plain[16:20])[0]
    msg = plain[20 : 20 + msg_len]
    rid = plain[20 + msg_len :]
    if receive_id and rid != receive_id.encode("utf-8"):
        raise ValueError("receive_id 校验失败（消息非发给本应用）")
    return msg


def wecom_decrypt(aes_key: str, b64_cipher: str, receive_id: str = "") -> str:
    """企业微信消息/AESKey 解密：返回明文消息字符串。"""
    try:
        from Crypto.Cipher import AES  # type: ignore

        key = _b64decode_key_with_pad(aes_key)
        iv = key[:16]
        cipher = AES.new(key, AES.MODE_CBC, iv)
        data = cipher.decrypt(base64.b64decode(b64_cipher))
        pad = data[-1]
        data = data[:-pad]
        msg = _strip_platform_plain(data, receive_id)
        return msg.decode("utf-8")
    except ImportError:
        key = _b64decode_key_with_pad(aes_key)
        iv = key[:16]
        data = aes_cbc_decrypt(key, iv, base64.b64decode(b64_cipher))
        msg = _strip_platform_plain(data, receive_id)
        return msg.decode("utf-8")


def feishu_decrypt(encrypt_key: str, b64_cipher: str, timestamp: str = "",
                   nonce: str = "", receive_id: str = "") -> str:
    """飞书（开启加密）回调解密：AESKey = encrypt_key + '='，iv = key[:16]，
    密文前还需按官方做一次 base64decode。timestamp/nonce 仅用于官方校验（此处不解密时校验）。"""
    try:
        from Crypto.Cipher import AES  # type: ignore

        key = base64.b64decode(encrypt_key + "=")
        iv = key[:16]
        cipher = AES.new(key, AES.MODE_CBC, iv)
        data = cipher.decrypt(base64.b64decode(b64_cipher))
        pad = data[-1]
        data = data[:-pad]
        msg = _strip_platform_plain(data, receive_id)
        return msg.decode("utf-8")
    except ImportError:
        key = base64.b64decode(encrypt_key + "=")
        iv = key[:16]
        data = aes_cbc_decrypt(key, iv, base64.b64decode(b64_cipher))
        msg = _strip_platform_plain(data, receive_id)
        return msg.decode("utf-8")


def feishu_encrypt(encrypt_key: str, plaintext: str, receive_id: str = "") -> str:
    """飞书加密回包（可选）：random(16)+len(4)+msg+receiveid 后 AES-CBC，再 base64。"""
    msg = plaintext.encode("utf-8")
    body = (
        os.urandom(16)
        + struct.pack(">I", len(msg))
        + msg
        + receive_id.encode("utf-8")
    )
    key = base64.b64decode(encrypt_key + "=")
    iv = key[:16]
    enc = aes_cbc_encrypt(key, iv, body)
    return base64.b64encode(enc).decode("ascii")


def _self_test():
    """NIST FIPS-197 CBC 标准测试向量（AES-256）。"""
    key = bytes.fromhex(
        "603deb1015ca71be2b73aef0857d77811f352c073b6108d72d9810a30914dff4"
    )
    iv = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
    pt = bytes.fromhex(
        "6bc1bee22e409f96e93d7e117393172a"
        "ae2d8a571e03ac9c9eb76fac45af8e51"
        "30c81c46a35ce411e5fbc1191a0a52ef"
        "f69f2445df4f9b17ad2b417be66c3710"
    )
    ct = aes_cbc_encrypt(key, iv, pt)
    expected = bytes.fromhex(
        "f58c4c04d6e5f1ba779eabfb5f7bfbd6"
        "9cfc4e967edb808d679f777bc6702c7d"
        "39f23369a9d9bacfa530e26304231461"
        "b2eb05e2c39be9fcda6c19078c6a9d1b"
    )
    # NIST 向量是"原生 CBC（明文恰为块对齐，无填充）"；平台的加密按 PKCS7 补整块，
    # 因此这里比对无填充的原始 CBC 输出（前 64 字节），再用 PKCS7 全程往返验证解密。
    assert ct[:64] == expected, "AES-256-CBC 加密向量不匹配"
    assert aes_cbc_decrypt(key, iv, ct) == pt, "AES-256-CBC 解密向量不匹配（PKCS7 往返）"
    # 平台解密往返
    aes_key = base64.b64encode(os.urandom(32)).decode("ascii").rstrip("=")
    sample = "hello 企业微信/飞书 测试 ☎"
    enc = feishu_encrypt(aes_key, sample, receive_id="appid123")
    dec = feishu_decrypt(aes_key, enc, receive_id="appid123")
    assert dec == sample, "平台加密/解密往返失败"
    print("crypto self-test OK")


if __name__ == "__main__":
    _self_test()
