"""通过涂鸦官方 tuya-device-sharing-sdk 用 Smart Life 扫码获取设备 local_key。

仅在配置阶段临时连涂鸦云，运行时完全本地。
复用 HA 公开的 device-sharing 应用凭据（client_id / schema），
与 HA 官方 Tuya 集成、tuya-local 同源，无需 Tuya IoT 开发者账号。
"""
from __future__ import annotations

import base64
import io
import logging
from typing import Any

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# Home Assistant 公开的 device-sharing 应用注册（非密钥，见于 HA/tuya-local 源码）。
CLIENT_ID = "HA_3y9q4ak7g4ephrvke"
SCHEMA = "haauthorize"
# 二维码 scheme，支持 smartlife 或 tuyaSmart
QR_SCHEME = "smartlife"


class TuyaSharingError(Exception):
    """涂鸦共享登录失败。"""


def _login_control():
    try:
        from tuya_sharing import LoginControl
    except ImportError as exc:  # pragma: no cover
        raise TuyaSharingError(
            "缺少依赖 tuya-device-sharing-sdk，请重新安装集成"
        ) from exc
    return LoginControl()


async def async_mint_qr_token(hass: HomeAssistant, user_code: str) -> str:
    """向涂鸦申请登录二维码 token。"""

    def _do() -> str:
        resp = _login_control().qr_code(CLIENT_ID, SCHEMA, user_code)
        if not resp.get("success"):
            code = resp.get("code", "unknown")
            msg = resp.get("msg", "")
            _LOGGER.warning("申请二维码失败: code=%s msg=%s", code, msg)
            raise TuyaSharingError(f"{code}: {msg}")
        return resp["result"]["qrcode"]

    return await hass.async_add_executor_job(_do)


def qr_png_b64(token: str) -> str:
    """把 token 生成二维码 PNG，返回 data:base64 字符串。"""
    import qrcode

    buf = io.BytesIO()
    qrcode.make(f"{QR_SCHEME}--qrLogin?token={token}").save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/png;base64,{b64}"


async def async_poll_login(
    hass: HomeAssistant, token: str, user_code: str
) -> dict[str, Any] | None:
    """轮询一次登录结果，成功返回 session dict，否则 None。"""

    def _do() -> dict[str, Any] | None:
        try:
            ok, result = _login_control().login_result(token, CLIENT_ID, user_code)
        except Exception:  # noqa: BLE001 - SDK 偶发网络异常
            return None
        if not ok:
            return None
        token_fields = ("t", "uid", "expire_time", "access_token", "refresh_token")
        return {
            "client_id": CLIENT_ID,
            "user_code": user_code,
            "terminal_id": result.get("terminal_id"),
            "endpoint": result.get("endpoint") or result.get("end_point"),
            "token_info": {k: result.get(k) for k in token_fields},
        }

    return await hass.async_add_executor_job(_do)


async def async_fetch_devices(
    hass: HomeAssistant, session: dict[str, Any]
) -> list[dict[str, Any]]:
    """用 session 拉取设备列表，返回 [{id, name, local_key, ...}]。"""

    def _do() -> list[dict[str, Any]]:
        from tuya_sharing import Manager

        manager = Manager(
            session.get("client_id", CLIENT_ID),
            session["user_code"],
            session["terminal_id"],
            session["endpoint"],
            session["token_info"],
            None,
        )
        manager.update_device_cache()
        devices = []
        for dev in manager.device_map.values():
            devices.append(
                {
                    "id": getattr(dev, "id", ""),
                    "name": getattr(dev, "name", "") or getattr(dev, "id", ""),
                    "local_key": getattr(dev, "local_key", ""),
                    "product_id": getattr(dev, "product_id", ""),
                    "category": getattr(dev, "category", ""),
                    "online": bool(getattr(dev, "online", False)),
                    "support_local": bool(getattr(dev, "support_local", False)),
                }
            )
        return devices

    try:
        return await hass.async_add_executor_job(_do)
    except Exception as exc:  # noqa: BLE001
        _LOGGER.exception("拉取涂鸦设备列表失败")
        raise TuyaSharingError(f"拉取设备列表失败: {exc}") from exc
