import pytest

from freelance_agents.sandbox import Sandbox, SandboxError


@pytest.fixture
def sb(tmp_path):
    return Sandbox(tmp_path / "proj", ["pytest", "pandas", "beautifulsoup4"], timeout=60)


def test_paths_stay_inside(sb):
    sb.write("src/app.py", "print('hi')")
    assert sb.read("src/app.py") == "print('hi')"
    assert sb.files() == ["src/app.py"]
    for bad in ("../escape.txt", "..\\escape.txt", "C:/Windows/evil.txt", ".venv/x.py"):
        with pytest.raises(SandboxError):
            sb.write(bad, "x")


def test_command_allowlist(sb, monkeypatch):
    monkeypatch.setattr(sb, "_python", lambda: "PY")
    assert sb.command_argv("python app.py") == ["PY", "app.py"]
    assert sb.command_argv("pytest -q") == ["PY", "-m", "pytest", "-q"]
    assert sb.command_argv("pip install pandas beautifulsoup4==4.12") == ["PY", "-m", "pip", "install", "--quiet", "pandas", "beautifulsoup4==4.12"]
    for bad in ("rm -rf /", "powershell Get-Item", "python a.py && del x", "python a.py > out", "pip install evilpkg",
                "pip uninstall pandas", "pip install --index-url http://x pandas", "curl http://x", ""):
        with pytest.raises(SandboxError):
            sb.command_argv(bad)


def test_environment_has_no_secrets(sb, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-should-not-leak")
    env = sb._env()
    assert "DEEPSEEK_API_KEY" not in env and "FREELANCER_OAUTH_TOKEN" not in env
    assert set(env) <= {"PATH", "TEMP", "TMP", "HOME", "USERPROFILE", "PYTHONIOENCODING", "PYTHONUTF8",
                        "PIP_DISABLE_PIP_VERSION_CHECK", "SYSTEMROOT", "PIP_NO_CACHE_DIR", "LOCALAPPDATA",
                        "APPDATA", "XDG_CACHE_HOME"}
    assert env["PIP_NO_CACHE_DIR"] == "1" and env["LOCALAPPDATA"].endswith("tmp")


def test_package_excludes_venv(sb, tmp_path):
    sb.write("main.py", "x = 1")
    for junk in (".venv/junk.txt", "appdata/local/pip/cache/x.body"):
        (sb.root / junk).parent.mkdir(parents=True, exist_ok=True)
        (sb.root / junk).write_text("junk")
    import zipfile
    z = zipfile.ZipFile(sb.package(tmp_path / "out.zip", {"HANDOFF.md": "# Setup"}))
    assert sorted(z.namelist()) == ["HANDOFF.md", "main.py"]
