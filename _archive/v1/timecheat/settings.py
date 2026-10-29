"""Local files (``~/.timecheat``) and the system keychain."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from urllib.parse import urlparse

DEFAULT_SERVER = "https://jira.example.com/"
KEYRING_SERVICE = "timecheat"
PAT_ACCOUNT = "__personal_access_token__"


def home_dir() -> Path:
    return Path(os.environ.get("TIMECHEAT_HOME", Path.home() / ".timecheat"))


@dataclass
class Settings:
    server: str = DEFAULT_SERVER
    auth_mode: str = "password"
    username: str = ""
    last_project: str = ""

    @classmethod
    def path(cls) -> Path:
        return home_dir() / "settings.json"

    @classmethod
    def load(cls) -> Settings:
        try:
            data = json.loads(cls.path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})

    def save(self) -> None:
        path = self.path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")


def server_host(server: str) -> str:
    return urlparse(server).netloc or server


def _keyring_service(server: str) -> str:
    return f"{KEYRING_SERVICE}:{server_host(server)}"


def load_secret(server: str, account: str) -> str | None:
    try:
        import keyring
        from keyring.errors import KeyringError
    except ImportError:  # pragma: no cover - handled by requirements.txt
        return None
    try:
        return keyring.get_password(_keyring_service(server), account)
    except KeyringError:
        return None


def save_secret(server: str, account: str, secret: str) -> bool:
    try:
        import keyring
        from keyring.errors import KeyringError
    except ImportError:  # pragma: no cover - handled by requirements.txt
        return False
    try:
        keyring.set_password(_keyring_service(server), account, secret)
    except KeyringError:
        return False
    return True


def delete_secret(server: str, account: str) -> None:
    try:
        import keyring
        from keyring.errors import KeyringError
    except ImportError:  # pragma: no cover - handled by requirements.txt
        return
    try:
        keyring.delete_password(_keyring_service(server), account)
    except KeyringError:
        pass
