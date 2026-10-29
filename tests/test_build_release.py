import importlib.util
import json
import shutil
import stat
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("build_release", ROOT / "tools" / "build_release.py")
build_release = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build_release)


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "src"
    for name in build_release.FILES:
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, root / name)
    shutil.copytree(ROOT / "timecheat", root / "timecheat")
    (root / "timecheat" / "__pycache__").mkdir(exist_ok=True)
    (root / "timecheat" / "__pycache__" / "x.pyc").write_bytes(b"")
    (root / "timecheat" / ".DS_Store").write_bytes(b"")
    (root / "start.bat").write_bytes(b"@echo off\n:label\r\necho hi\n")
    return root


def test_generic_zip_contents(project, tmp_path):
    target = build_release.build(project, tmp_path / "dist")
    version = build_release.version(project)
    assert target.name == f"timecheat-{version}.zip"
    with zipfile.ZipFile(target) as archive:
        names = archive.namelist()
        folder = f"TimeCheat-{version}/"
        assert all(n.startswith(folder) for n in names)
        short = {n[len(folder):] for n in names}
        assert {"run.py", "requirements.txt", "start.bat", "start.command", "timecheat/app.py",
                "timecheat/fullcalendar/lib/index.global.min.js"} <= short
        assert not any("__pycache__" in n or ".DS_Store" in n or n.endswith(".pyc") for n in short)
        assert "timecheat_defaults.json" not in short
        assert not any(n.startswith(("tests/", "_archive/", ".venv/")) for n in short)
        assert archive.read(folder + "start.bat") == b"@echo off\r\n:label\r\necho hi\r\n"
        mode = archive.getinfo(folder + "start.command").external_attr >> 16
        assert mode & stat.S_IXUSR
        assert not (archive.getinfo(folder + "run.py").external_attr >> 16) & stat.S_IXUSR


def test_team_zip_needs_and_ships_the_defaults(project, tmp_path):
    with pytest.raises(SystemExit, match="timecheat_defaults.json"):
        build_release.build(project, tmp_path / "dist", team=True)
    (project / "timecheat_defaults.json").write_text("{broken")
    with pytest.raises(SystemExit, match="not valid JSON"):
        build_release.build(project, tmp_path / "dist", team=True)
    (project / "timecheat_defaults.json").write_text(json.dumps({"server": "https://jira.example.com/"}))
    target = build_release.build(project, tmp_path / "dist", team=True)
    assert target.name.endswith("-team.zip")
    with zipfile.ZipFile(target) as archive:
        assert any(n.endswith("/timecheat_defaults.json") for n in archive.namelist())
