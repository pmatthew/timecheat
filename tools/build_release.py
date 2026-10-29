"""Build the zip that users download: ``python tools/build_release.py [--team]``.

The zip holds one ``TimeCheat-<version>/`` folder with the app, ``README.md`` and the ``start.bat`` / ``start.command`` launchers. ``--team`` also adds the
``timecheat_defaults.json`` from the project root (Jira server, meeting story, ...), so
colleagues do not have to set anything up. That file is git-ignored: share the team zip
inside the company only, and publish the generic one.
"""

from __future__ import annotations

import argparse
import json
import re
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = ["run.py", "requirements.txt", "README.md", "start.bat", "start.command",
         "timecheat_defaults.example.json"]
PACKAGE = "timecheat"
DEFAULTS = "timecheat_defaults.json"
EXECUTABLE = {"start.command"}
SKIP_PARTS = {"__pycache__", ".DS_Store"}


def version(root: Path = ROOT) -> str:
    match = re.search(r'__version__\s*=\s*"([^"]+)"', (root / PACKAGE / "__init__.py").read_text(encoding="utf-8"))
    if not match:
        raise SystemExit("No __version__ in timecheat/__init__.py")
    return match.group(1)


def _sources(root: Path, team: bool) -> list[str]:
    names = list(FILES)
    names += sorted(
        p.relative_to(root).as_posix()
        for p in (root / PACKAGE).rglob("*")
        if p.is_file() and not SKIP_PARTS & set(p.parts) and p.suffix != ".pyc"
    )
    if team:
        defaults = root / DEFAULTS
        if not defaults.is_file():
            raise SystemExit(f"--team needs {DEFAULTS} in {root} (start from timecheat_defaults.example.json)")
        try:
            json.loads(defaults.read_text(encoding="utf-8"))
        except ValueError as error:
            raise SystemExit(f"{DEFAULTS} is not valid JSON: {error}") from None
        names.append(DEFAULTS)
    missing = [n for n in names if not (root / n).is_file()]
    if missing:
        raise SystemExit(f"Missing files: {', '.join(missing)}")
    return names


def _content(path: Path) -> bytes:
    data = path.read_bytes()
    if path.suffix == ".bat":
        # cmd.exe misreads labels in LF-only batch files.
        return data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
    if path.suffix in {".command", ".sh"}:
        return data.replace(b"\r\n", b"\n")
    return data


def build(root: Path = ROOT, out_dir: Path | None = None, team: bool = False) -> Path:
    release = version(root)
    out_dir = out_dir or root / "dist"
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"timecheat-{release}{'-team' if team else ''}.zip"
    folder = f"TimeCheat-{release}"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in _sources(root, team):
            info = zipfile.ZipInfo(f"{folder}/{name}", date_time=(2026, 1, 1, 0, 0, 0))
            info.create_system = 3  # Unix, so that the permissions below are honoured on extraction
            info.external_attr = (0o100755 if name in EXECUTABLE else 0o100644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, _content(root / name))
    return target


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Build the TimeCheat release zip.")
    parser.add_argument("--team", action="store_true", help=f"include {DEFAULTS} (share inside the company only)")
    parser.add_argument("--out", type=Path, help="output folder (default: dist/)")
    args = parser.parse_args(argv)
    print(build(out_dir=args.out, team=args.team))


if __name__ == "__main__":
    main()
