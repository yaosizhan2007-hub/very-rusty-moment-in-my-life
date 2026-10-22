"""Windows 下的凭据安全存储（DPAPI，按当前 Windows 用户绑定）。

为什么不用明文 JSON：OBS 密码、云端 AI 的 API Key 不能写进普通配置文件或日志。
DPAPI（``CryptProtectData``）不需要额外服务、不需要管理员权限，密文只有同一
Windows 用户在同一台机器上才能解开，适合本项目这种本机娱乐工具。

若 pywin32 不可用（非 Windows 或打包时漏了依赖），:attr:`SecretStore.available`
为 ``False``，调用方必须退化为「每次启动手动输入」，而不是把密码写盘。
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

try:  # pragma: no cover - 平台相关导入
    import win32crypt  # type: ignore

    _HAS_DPAPI = True
except Exception:  # pragma: no cover
    win32crypt = None  # type: ignore
    _HAS_DPAPI = False


class SecretStoreError(Exception):
    """凭据存储不可用或读写失败。"""


def default_secret_path() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home())
    return Path(base) / "RustyMoment" / "secrets.bin"


class SecretStore:
    """把一组 key/value 凭据加密后存成单个文件。"""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else default_secret_path()
        self._cache: dict[str, str] | None = None

    # -- 能力 --------------------------------------------------------------- #
    @property
    def available(self) -> bool:
        return _HAS_DPAPI

    @property
    def unavailable_reason(self) -> str:
        if _HAS_DPAPI:
            return ""
        return "当前环境缺少 pywin32，无法使用 Windows 凭据加密；请在每次启动时手动输入密码。"

    # -- 读写 --------------------------------------------------------------- #
    def get_secret(self, key: str) -> str | None:
        return self._load().get(key)

    def set_secret(self, key: str, value: str) -> None:
        if not value:
            self.delete_secret(key)
            return
        if not self.available:
            raise SecretStoreError(self.unavailable_reason)
        data = self._load()
        data[key] = value
        self._save(data)

    def delete_secret(self, key: str) -> None:
        data = self._load()
        if data.pop(key, None) is not None:
            self._save(data)

    def clear(self) -> None:
        self._cache = {}
        try:
            if self.path.exists():
                self.path.unlink()
        except OSError as exc:  # pragma: no cover
            raise SecretStoreError(f"清除凭据失败：{exc}") from exc

    def has(self, key: str) -> bool:
        return bool(self.get_secret(key))

    # -- 内部 --------------------------------------------------------------- #
    def _load(self) -> dict[str, str]:
        if self._cache is not None:
            return self._cache
        self._cache = {}
        if not self.path.exists():
            return self._cache
        if not self.available:
            log.warning("凭据文件存在但当前环境无法解密：%s", self.path)
            return self._cache
        try:
            blob = self.path.read_bytes()
            _desc, plain = win32crypt.CryptUnprotectData(blob, None, None, None, 0)  # type: ignore[union-attr]
            raw: Any = json.loads(plain.decode("utf-8"))
            if isinstance(raw, dict):
                self._cache = {str(k): str(v) for k, v in raw.items()}
        except Exception as exc:  # 解密失败常见于换了 Windows 用户/机器
            log.error("凭据解密失败（%s），请重新输入密码", exc)
            self._cache = {}
        return self._cache

    def _save(self, data: dict[str, str]) -> None:
        if not self.available:
            raise SecretStoreError(self.unavailable_reason)
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        blob = win32crypt.CryptProtectData(payload, "RustyMoment", None, None, None, 0)  # type: ignore[union-attr]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_bytes(blob)
        os.replace(tmp, self.path)
        self._cache = dict(data)
