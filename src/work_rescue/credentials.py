"""Repository-scoped OS credentials. No plaintext-file fallback."""

from __future__ import annotations

import hashlib
from pathlib import Path

import keyring
from keyring.errors import KeyringError

from .core import RescueError


class CredentialsUnavailable(RescueError):
    pass


class CredentialStore:
    SERVICE = "work-rescue.repository.v1"
    ALLOWED = frozenset(
        {
            "keyring.backends.Windows",
            "keyring.backends.macOS",
            "keyring.backends.SecretService",
            "keyring.backends.libsecret",
            "keyring.backends.kwallet",
        }
    )

    def __init__(self, repository: Path):
        self.account = hashlib.sha256(
            str(repository.expanduser().resolve()).encode("utf-8")
        ).hexdigest()

    def backend(self):
        try:
            selected = keyring.get_keyring()
            choices = getattr(selected, "backends", [selected])
            for backend in choices:
                if type(backend).__module__ in self.ALLOWED and backend.priority > 0:
                    return backend
        except (KeyringError, RuntimeError, ImportError, OSError) as exc:
            raise CredentialsUnavailable("系统凭据服务不可用，可手动输入口令使用") from exc
        raise CredentialsUnavailable("没有可用的系统凭据服务；不会将口令保存为明文文件")

    def load(self) -> str | None:
        backend = self.backend()
        try:
            return backend.get_password(self.SERVICE, self.account)
        except Exception as exc:
            raise CredentialsUnavailable("无法读取系统保存的备份口令") from exc

    def save(self, password: str):
        if not password:
            raise RescueError("不能保存空口令")
        backend = self.backend()
        try:
            backend.set_password(self.SERVICE, self.account, password)
            remembered = backend.get_password(self.SERVICE, self.account)
        except Exception as exc:
            raise CredentialsUnavailable("系统未能保存备份口令") from exc
        if remembered != password:
            raise CredentialsUnavailable("系统凭据写入后未能确认，口令仍需手动保存")

    def forget(self):
        backend = self.backend()
        try:
            backend.delete_password(self.SERVICE, self.account)
        except Exception as exc:
            raise CredentialsUnavailable("无法删除系统保存的备份口令") from exc
