"""Settings, local cache directory (``~/.timecheat``) and the system keychain.

Settings are layered: built-in defaults, then the optional ``timecheat_defaults.json``
next to ``run.py`` (a team can ship its Jira server and meeting story with the app),
then the user's own ``~/.timecheat/settings.json``. Only values that differ from the
defaults are written to the user's file, so changed team defaults still reach them.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from urllib.parse import urlparse

DEFAULT_SERVER = ""
DEMO_MEETING_STORY = "General meetings"
KEYRING_SERVICE = "timecheat"
PAT_ACCOUNT = "__personal_access_token__"
APP_DIR = Path(__file__).resolve().parents[1]
SETTINGS_FILE = "settings.json"
DEMO_SETTINGS_FILE = "settings.demo.json"
DEFAULTS_FILE = "timecheat_defaults.json"
# Before 1.1 the settings lived next to run.py.
LEGACY_SETTINGS_FILE = "timecheat_settings.json"
_HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$|^24:00$")

_settings_path: Path | None = None
_demo = False


def home_dir() -> Path:
    return Path(os.environ.get("TIMECHEAT_HOME", Path.home() / ".timecheat"))


def configure(demo: bool) -> None:
    """Demo runs keep their own settings file so they never touch the real one."""
    global _settings_path, _demo
    _demo = demo
    override = os.environ.get("TIMECHEAT_SETTINGS")
    _settings_path = Path(override) if override else home_dir() / (DEMO_SETTINGS_FILE if demo else SETTINGS_FILE)


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def parse_hhmm(value: str | None) -> int | None:
    match = _HHMM.match((value or "").strip())
    if not match:
        return None
    if match.group(0) == "24:00":
        return 24 * 60
    return int(match.group(1)) * 60 + int(match.group(2))


def hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


@dataclass
class Settings:
    server: str = DEFAULT_SERVER
    auth_mode: str = "password"
    username: str = ""
    last_project: str = ""
    default_project: str = ""
    work_start: str = "09:00"
    work_end: str = "17:00"
    # Summary of the story whose sub-tasks hold the Outlook meetings; empty turns auto-matching off.
    meeting_story: str = ""

    @classmethod
    def path(cls) -> Path:
        if _settings_path is None:
            configure(demo=False)
        assert _settings_path is not None
        return _settings_path

    @classmethod
    def defaults(cls) -> dict:
        """Built-in defaults overlaid with the team defaults file (not used in demo mode)."""
        data = asdict(cls())
        if _demo:
            data["meeting_story"] = DEMO_MEETING_STORY
        else:
            data.update(cls._known(_read_json(APP_DIR / DEFAULTS_FILE)))
        return data

    @classmethod
    def _known(cls, data: dict) -> dict:
        known = {f.name for f in fields(cls)}
        return {k: v for k, v in data.items() if k in known and isinstance(v, str)}

    @classmethod
    def load(cls) -> Settings:
        path = cls.path()
        source = path
        legacy = APP_DIR / LEGACY_SETTINGS_FILE
        # Older versions wrote both files, so the newer of the two is the current one.
        if (not _demo and not os.environ.get("TIMECHEAT_SETTINGS") and legacy.is_file()
                and (not path.is_file() or legacy.stat().st_mtime > path.stat().st_mtime)):
            source = legacy
        defaults = cls.defaults()
        settings = cls(**{**defaults, **cls._known(_read_json(source))})
        if validate_work_period(settings.work_start, settings.work_end):
            settings.work_start, settings.work_end = defaults["work_start"], defaults["work_end"]
            if validate_work_period(settings.work_start, settings.work_end):
                settings.work_start, settings.work_end = cls.work_start, cls.work_end
        return settings

    def save(self) -> None:
        path = self.path()
        defaults = self.defaults()
        own = {k: v for k, v in asdict(self).items() if v != defaults.get(k)}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(own, indent=2, ensure_ascii=False), encoding="utf-8")

    @property
    def start_project(self) -> str:
        return self.default_project or self.last_project

    @property
    def work_start_minutes(self) -> int:
        return parse_hhmm(self.work_start) or 0

    @property
    def work_end_minutes(self) -> int:
        return parse_hhmm(self.work_end) or 0

    @property
    def target_seconds(self) -> int:
        return (self.work_end_minutes - self.work_start_minutes) * 60


def validate_work_period(start: str, end: str) -> str | None:
    """An error message, or None if ``start``-``end`` is a usable working period."""
    begin, finish = parse_hhmm(start), parse_hhmm(end)
    if begin is None or finish is None:
        return "Use the HH:MM format, e.g. 09:00."
    if finish <= begin:
        return "The end of the working period must be after its start."
    return None


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
