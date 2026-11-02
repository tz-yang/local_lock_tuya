"""fnOS WebSocket API 客户端。

实现 RSA+AES 鉴权、HMAC-SHA256 签名、资源监控数据获取。
参考：https://fnosp.github.io/fnnas-api/
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import uuid
from typing import Any

from cryptography.hazmat.primitives import padding, serialization
from cryptography.hazmat.primitives.asymmetric import padding as asym_padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

import websockets
from websockets.protocol import State

from .const import WS_USER_AGENT

_LOGGER = logging.getLogger(__name__)

# fnOS 关机命令（前端 hK 表逆向确认：poweroff=`power.poweroff`，reboot=`power.reboot`）
CMD_SHUTDOWN = "power.poweroff"

# 前端 AES 密钥源字符串的字符集（index JS: uHe(32)，数字+小写+大写）
_XY_CHARS = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"


# 服务端错误码：4224 = 没有登录（会话失效）
ERRNO_NOT_LOGIN = 4224


class FnosApiError(Exception):
    """fnOS API 错误。"""

    def __init__(self, message: str, errno: int | None = None) -> None:
        super().__init__(message)
        self.errno = errno


class FnosClient:
    """fnOS WebSocket 客户端。"""

    def __init__(self, host: str, port: int, username: str, password: str) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._token: str | None = None
        self._hmac_key: bytes | None = None
        self._aes_key: bytes | None = None
        self._aes_iv: bytes | None = None
        self._connected = False

    @property
    def connected(self) -> bool:
        return (
            self._connected
            and self._ws is not None
            and self._ws.state is State.OPEN
        )

    async def connect(self) -> None:
        """建立 WebSocket 连接。"""
        uri = f"ws://{self._host}:{self._port}/websocket?type=main"
        # fnOS 不回复协议层 ping 帧，必须关闭库的心跳，保活靠应用层请求
        # 握手头需携带浏览器 UA + Origin，否则服务端静默黑洞所有消息
        self._ws = await websockets.connect(
            uri,
            open_timeout=10,
            ping_interval=None,
            additional_headers={
                "User-Agent": WS_USER_AGENT,
                "Origin": f"http://{self._host}:{self._port}",
            },
        )
        self._connected = True
        _LOGGER.debug("已连接到 fnOS: %s", uri)

    async def close(self) -> None:
        """关闭连接。"""
        self._connected = False
        if self._ws:
            await self._ws.close()
            self._ws = None

    async def _send_raw(
        self, message: str, match_reqid: str | None = None
    ) -> dict[str, Any]:
        """发送原始消息并等待匹配的响应。

        match_reqid：用于外层不带 reqid 的消息（如 encrypted 加密包），
        按解密后响应中带回的 reqid 匹配。
        """
        if not self._ws:
            raise FnosApiError("未连接")
        data = json.loads(message)
        if match_reqid is None:
            reqid = data.get("reqid") or uuid.uuid4().hex[:16]
            data["reqid"] = reqid
        else:
            reqid = match_reqid
        payload = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
        _LOGGER.debug("fnOS: 发送 %.200s", payload)
        await self._ws.send(payload)

        # 等待匹配响应
        while True:
            try:
                resp_text = await asyncio.wait_for(self._ws.recv(), timeout=15)
            except asyncio.TimeoutError as err:
                raise FnosApiError(
                    f"等待响应超时（15s 无回包）: req={data.get('req')}"
                ) from err
            resp = json.loads(resp_text)
            if resp.get("reqid") == reqid:
                return resp
            _LOGGER.debug(
                "fnOS: 跳过不匹配消息 req=%s reqid=%s（等待 %s）",
                resp.get("req"), resp.get("reqid"), reqid,
            )

    async def _get_rsa_pub(self) -> tuple[str, str]:
        """获取 RSA 公钥和 si。"""
        resp = await self._send_raw(
            json.dumps({"req": "util.crypto.getRSAPub"})
        )
        if resp.get("result") != "succ":
            raise FnosApiError(f"获取 RSA 公钥失败: {resp}")
        return resp["pub"], resp.get("si", "")

    @staticmethod
    def _aes_encrypt(key: bytes, iv: bytes, plaintext: bytes) -> bytes:
        """AES-128-CBC 加密（PKCS7 填充）。"""
        padder = padding.PKCS7(128).padder()
        padded = padder.update(plaintext) + padder.finalize()
        cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
        encryptor = cipher.encryptor()
        return encryptor.update(padded) + encryptor.finalize()

    @staticmethod
    def _aes_decrypt(key: bytes, iv: bytes, ciphertext: bytes) -> bytes:
        """AES-128-CBC 解密（PKCS7 去填充）。"""
        cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
        decryptor = cipher.decryptor()
        padded = decryptor.update(ciphertext) + decryptor.finalize()
        unpadder = padding.PKCS7(128).unpadder()
        return unpadder.update(padded) + unpadder.finalize()

    async def login(self) -> None:
        """登录 fnOS，获取 token 和签名密钥。"""
        # 浏览器实测：连接后首条消息即 getRSAPub（无需 getSI），si 由公钥响应携带
        pub_pem, si = await self._get_rsa_pub()
        _LOGGER.debug("fnOS: 已获取 RSA 公钥，si=%s", si)

        # 复刻前端实现（index JS 逆向）：
        #   xY  = 32 字符随机串（0-9a-zA-Z）
        #   key = xY 的 32 个 ASCII 字节 → AES-256-CBC
        #   rsa = RSA-PKCS1v15 加密 xY 字符串本身（jsencrypt 路径，外层无 v 字段）
        xy = "".join(
            secrets.choice(_XY_CHARS) for _ in range(32)
        )
        self._aes_key = xy.encode("utf-8")
        self._aes_iv = os.urandom(16)

        public_key = serialization.load_pem_public_key(pub_pem.encode())
        rsa_encrypted = public_key.encrypt(
            xy.encode("utf-8"), asym_padding.PKCS1v15()
        )

        # 构造登录数据（服务端按解密后载荷中的 reqid 回复）
        login_reqid = uuid.uuid4().hex[:16]
        login_data = {
            "req": "user.login",
            "reqid": login_reqid,
            "user": self._username,
            "password": self._password,
            "deviceType": "Browser",
            "deviceName": "HomeAssistant",
            "stay": False,
            "si": si,
        }
        login_json = json.dumps(login_data, separators=(",", ":"), ensure_ascii=False)
        aes_encrypted = self._aes_encrypt(
            self._aes_key, self._aes_iv, login_json.encode("utf-8")
        )
        # 浏览器实测：外层只有 req/iv/rsa/aes 四个字段，不带 reqid；
        # 响应按内层（解密后）载荷的 reqid 匹配
        enc_req = {
            "req": "encrypted",
            "iv": base64.b64encode(self._aes_iv).decode(),
            "rsa": base64.b64encode(rsa_encrypted).decode(),
            "aes": base64.b64encode(aes_encrypted).decode(),
        }
        _LOGGER.debug("fnOS: 已发送加密登录请求（AES-256，密钥源长度 %d）", len(xy))
        resp = await self._send_raw(
            json.dumps(enc_req), match_reqid=login_reqid
        )
        _LOGGER.debug("fnOS: 登录响应 result=%s", resp.get("result"))

        if resp.get("result") != "succ":
            raise FnosApiError(f"登录失败: {resp}")

        self._token = resp.get("token")
        # 解密 secret 得到 HMAC 签名密钥
        secret_b64 = resp.get("secret", "")
        if secret_b64 and self._aes_key and self._aes_iv:
            secret_enc = base64.b64decode(secret_b64)
            self._hmac_key = self._aes_decrypt(self._aes_key, self._aes_iv, secret_enc)

        _LOGGER.info("fnOS 登录成功，admin=%s", resp.get("admin"))

    def _sign(self, body_json: str) -> str:
        """对请求体计算 HMAC-SHA256 签名。"""
        if not self._hmac_key:
            raise FnosApiError("未登录，无法签名")
        sig = hmac.new(self._hmac_key, body_json.encode("utf-8"), hashlib.sha256)
        return base64.b64encode(sig.digest()).decode("utf-8")

    async def call(self, req: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        """调用需要签名的 API。

        会话在服务端失效（errno=4224）时，WS 连接可能仍然处于 OPEN，
        此时自动重新登录并重试一次，避免传感器静默失效、关机失败。
        """
        try:
            return await self._call_once(req, data)
        except FnosApiError as err:
            if err.errno != ERRNO_NOT_LOGIN:
                raise
            _LOGGER.warning("fnOS 会话已失效（errno=4224），重新登录后重试: req=%s", req)
            await self.login()
            return await self._call_once(req, data)

    async def _call_once(
        self, req: str, data: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """发送一次签名请求并等待响应。"""
        body: dict[str, Any] = {"req": req, "reqid": uuid.uuid4().hex[:16]}
        if data:
            body.update(data)
        body_json = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
        sign = self._sign(body_json)
        if not self._ws:
            raise FnosApiError("未连接")
        # 签名前缀 + JSON（reqid 必须包含在签名体内）
        _LOGGER.debug("fnOS: 发送签名请求 %.200s", body_json)
        await self._ws.send(sign + body_json)

        reqid = body["reqid"]
        next_id = f"{int(reqid, 16) + 1:016x}"
        while True:
            try:
                resp_text = await asyncio.wait_for(self._ws.recv(), timeout=15)
            except asyncio.TimeoutError as err:
                raise FnosApiError(f"等待响应超时（15s 无回包）: req={req}") from err
            resp = json.loads(resp_text)
            # 浏览器抓包显示：签名请求的响应 reqid 可能是请求 reqid+1
            if resp.get("reqid") in (reqid, next_id) or resp.get("req") == req:
                result = resp.get("result")
                if result is not None and result != "succ":
                    raise FnosApiError(
                        f"API {req} 失败: {resp}", errno=resp.get("errno")
                    )
                return resp
            _LOGGER.debug(
                "fnOS: 跳过不匹配消息 req=%s reqid=%s 内容=%.120s（等待 %s）",
                resp.get("req"), resp.get("reqid"), resp_text, reqid,
            )

    async def get_cpu(self) -> dict[str, Any]:
        """获取 CPU 使用率和温度（返回 data.cpu 对象）。"""
        resp = await self.call("appcgi.resmon.cpu")
        return resp.get("data", {}).get("cpu", {})

    async def get_memory(self) -> dict[str, Any]:
        """获取内存使用情况（返回 data 对象，含 mem/swap）。"""
        resp = await self.call("appcgi.resmon.mem")
        return resp.get("data", {})

    async def get_net(self) -> dict[str, Any]:
        """获取网络速率（返回 data 对象，含 ifs 网卡数组，receive/transmit 为每秒字节数）。"""
        resp = await self.call("appcgi.resmon.net")
        return resp.get("data", {})

    async def get_gpu(self) -> dict[str, Any]:
        """获取 GPU 占用（返回 data 对象，含 num 和 gpu 数组，占用率在 gpu[].engine 中）。"""
        resp = await self.call("appcgi.resmon.gpu")
        return resp.get("data", {})

    async def get_host_name(self) -> dict[str, Any]:
        """获取设备主机名。"""
        resp = await self.call("appcgi.sysinfo.getHostName")
        return resp.get("data", {})

    async def shutdown(self) -> None:
        """关机。"""
        await self.call(CMD_SHUTDOWN)
        _LOGGER.info("已发送关机指令")

    async def ping(self) -> bool:
        """心跳检测。"""
        try:
            if not self._ws:
                return False
            await self._ws.send(json.dumps({"req": "ping"}))
            resp_text = await asyncio.wait_for(self._ws.recv(), timeout=5)
            resp = json.loads(resp_text)
            return resp.get("res") == "pong"
        except Exception:
            return False
