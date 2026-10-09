"""UDP 状态推送监听：接收门锁主动广播的状态变化，无需轮询。"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from collections.abc import Callable
from typing import Any

import tinytuya

_LOGGER = logging.getLogger(__name__)

UDP_PORTS = (6666, 6667, 7000)
_FRAME_PREFIX = b"\x00\x00\x55\xaa"
_VERSION_PREFIXES = (b"3.1", b"3.2", b"3.3", b"3.4", b"3.5")


def _decode_packet(data: bytes, local_key: str) -> dict[str, Any] | None:
    """解出广播 JSON。优先 tinytuya 官方解密，再按多种布局尝试本地密钥。"""
    key_bytes = local_key.encode() if isinstance(local_key, str) else local_key
    texts: list[str] = []
    try:
        texts.append(tinytuya.decrypt_udp(data))
    except Exception:
        pass
    if len(data) >= 20 and data[:4] == _FRAME_PREFIX:
        raw = b""
        try:
            header = tinytuya.parse_header(data)
            # length 含帧尾 crc(4)+suffix(4), 去掉后再取内容区
            raw = data[16 : 16 + header.length - 8]
        except Exception:
            raw = b""
        # 兼容两种布局: payload 前带/不带 4 字节 retcode
        for variant in (raw, raw[4:]):
            if not variant:
                continue
            if variant[:1] == b"{":
                try:
                    texts.append(variant.decode())
                except Exception:
                    pass
            if variant[:3] in _VERSION_PREFIXES:
                variant = variant[3:]
            try:
                texts.append(tinytuya.decrypt(variant, key_bytes))
            except Exception:
                pass
            try:
                texts.append(tinytuya.decrypt(base64.b64decode(variant), key_bytes))
            except Exception:
                pass
    for text in texts:
        if not isinstance(text, str):
            continue
        try:
            decoded = json.loads(text.rstrip("\x00"))
        except ValueError:
            continue
        if isinstance(decoded, dict):
            if decoded.get("gwId"):
                return decoded
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug("广播已解出 JSON 但缺少 gwId: %s", text)
    return None


class TuyaUdpListener:
    """监听涂鸦设备 UDP 广播（6666/6667/7000），回调在事件循环线程执行。"""

    def __init__(
        self,
        device_id: str,
        local_key: str,
        callback: Callable[[dict[str, Any], str], None],
        raw_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self._device_id = device_id
        self._local_key = local_key
        self._callback = callback
        # raw_callback：每个收到的广播报文都先调用一次（无论是否解码成功），用于调试捕获
        self._raw_callback = raw_callback
        self._transports: list[asyncio.DatagramTransport] = []

    async def async_start(self) -> None:
        loop = asyncio.get_running_loop()
        bound: list[int] = []
        for port in UDP_PORTS:
            try:
                # reuse_port=True 让多个集成（如 localtuya）可共存监听同一广播端口，
                # 内核会把收到的广播报文分发给每一个绑定的 socket
                transport, _ = await loop.create_datagram_endpoint(
                    lambda port=port: _BroadcastProtocol(self._on_datagram, port),
                    local_addr=("0.0.0.0", port),
                    reuse_port=True,
                )
                self._transports.append(transport)
                bound.append(port)
            except OSError as err:
                _LOGGER.warning(
                    "UDP 端口 %s 监听失败（可能被其他集成占用），该端口广播将被忽略: %s",
                    port,
                    err,
                )
        if self._transports:
            _LOGGER.info(
                "门锁 %s 的 UDP 状态推送监听已启动（端口 %s）",
                self._device_id,
                bound,
            )

    def _on_datagram(self, data: bytes, addr: tuple, port: int) -> None:
        payload = _decode_packet(data, self._local_key)
        # 无论是否解码成功都记录原始报文（调试捕获），并附带端口/来源/解码结果
        if self._raw_callback is not None:
            try:
                self._raw_callback(
                    {
                        "ip": addr[0],
                        "port": port,
                        "size": len(data),
                        "hex": data[:512].hex(" "),
                        "decoded": payload,
                        "is_mine": bool(
                            payload
                            and str(payload.get("gwId") or payload.get("id"))
                            == self._device_id
                        ),
                    }
                )
            except Exception:
                _LOGGER.exception("调试捕获回调异常")
        if payload is None:
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug(
                    "广播包解码失败（来自 %s:%s，%d 字节）: %s",
                    addr[0],
                    port,
                    len(data),
                    data[:512].hex(" "),
                )
            return
        if payload.get("from") == "app":
            return
        gw_id = payload.get("gwId") or payload.get("id")
        if not gw_id or str(gw_id) != self._device_id:
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug("忽略其他设备广播 gwId=%s（来自 %s）", gw_id, addr[0])
            return
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug("收到本设备广播（来自 %s）: %s", addr[0], payload)
        self._callback(payload, addr[0])

    @property
    def listening(self) -> bool:
        return bool(self._transports)

    def async_stop(self) -> None:
        for transport in self._transports:
            transport.close()
        self._transports = []


class _BroadcastProtocol(asyncio.DatagramProtocol):
    def __init__(
        self, on_datagram: Callable[[bytes, tuple], None], port: int
    ) -> None:
        self._on_datagram = on_datagram
        self._port = port

    def datagram_received(self, data: bytes, addr: tuple) -> None:
        self._on_datagram(data, addr, self._port)

    def error_received(self, exc: Exception) -> None:
        _LOGGER.debug("UDP 监听错误（端口 %s）: %s", self._port, exc)
