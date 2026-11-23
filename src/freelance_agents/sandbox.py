"""Confined project workspace for the Builder and QA agents.

What it enforces:
- Every file path must stay inside the project folder.
- Only `python ...`, `pytest ...` and `pip install <allowlisted packages>` can run,
  with no shell features (pipes, redirects, chaining).
- Commands run with a minimal environment: none of the system's API keys or tokens.
- Each project gets its own virtualenv in `.venv`; commands time out.

What it does NOT enforce without Docker: network isolation and CPU/memory limits.
Use a Docker-based runner for that when Docker is available.
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
import zipfile
from pathlib import Path

ALLOWED_COMMANDS = {"python", "pytest", "pip"}
SKIP_DIRS = {".venv", "tmp", "__pycache__", ".pytest_cache", "appdata", ".cache"}
SHELL_CHARS = re.compile(r"[|&;<>`$]")


class SandboxError(Exception):
    pass


class Sandbox:
    def __init__(self, root: str | Path, allowed_packages: list[str], timeout: int = 180):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "tmp").mkdir(exist_ok=True)
        self.allowed_packages = {p.lower() for p in allowed_packages}
        self.timeout = timeout

    # --- files -----------------------------------------------------------------
    def resolve(self, rel: str) -> Path:
        path = (self.root / rel).resolve()
        if path != self.root and self.root not in path.parents:
            raise SandboxError(f"path outside the project folder: {rel}")
        if any(part in (".venv",) for part in path.relative_to(self.root).parts):
            raise SandboxError(f"can't write inside .venv: {rel}")
        return path

    def write(self, rel: str, content: str) -> None:
        path = self.resolve(rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def read(self, rel: str, limit: int = 20000) -> str:
        path = self.resolve(rel)
        if not path.is_file():
            raise SandboxError(f"no such file: {rel}")
        return path.read_text(encoding="utf-8", errors="replace")[:limit]

    def files(self) -> list[str]:
        out = []
        for path in sorted(self.root.rglob("*")):
            rel = path.relative_to(self.root)
            if path.is_file() and not (set(rel.parts) & SKIP_DIRS):
                out.append(rel.as_posix())
        return out

    def all_text(self) -> str:
        chunks = []
        for rel in self.files():
            try:
                chunks.append(self.read(rel, limit=200000))
            except (SandboxError, OSError):
                pass
        return "\n".join(chunks)

    # --- commands --------------------------------------------------------------
    def _python(self) -> Path:
        venv = self.root / ".venv"
        py = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        if not py.exists():
            subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True, timeout=600, capture_output=True)
        return py

    def _env(self) -> dict:
        scripts = self.root / ".venv" / ("Scripts" if os.name == "nt" else "bin")
        system_root = os.environ.get("SYSTEMROOT", r"C:\Windows")
        tmp = str(self.root / "tmp")
        env = {
            "PATH": os.pathsep.join([str(scripts), os.path.join(system_root, "System32"), "/usr/bin", "/bin"]),
            "TEMP": tmp, "TMP": tmp, "HOME": str(self.root), "USERPROFILE": str(self.root),
            "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            # keep pip's cache and app data out of the deliverable folder
            "PIP_NO_CACHE_DIR": "1", "LOCALAPPDATA": tmp, "APPDATA": tmp, "XDG_CACHE_HOME": tmp,
        }
        if os.name == "nt":
            env["SYSTEMROOT"] = system_root
        return env

    def command_argv(self, command: str) -> list[str]:
        if SHELL_CHARS.search(command):
            raise SandboxError("shell features (| & ; < > ` $) are not allowed; run one command at a time")
        parts = shlex.split(command, posix=True)
        if not parts or parts[0] not in ALLOWED_COMMANDS:
            raise SandboxError("only `python ...`, `pytest ...` and `pip install <packages>` are allowed")
        py = str(self._python())
        if parts[0] == "pip":
            if parts[1:2] != ["install"] or len(parts) < 3:
                raise SandboxError("only `pip install <packages>` is allowed")
            pkgs = parts[2:]
            if any(p.startswith("-") for p in pkgs):
                raise SandboxError("pip flags are not allowed")
            bad = [p for p in pkgs if re.split(r"[<>=!\[~ ]", p)[0].lower() not in self.allowed_packages]
            if bad:
                raise SandboxError(f"packages not on the allowlist (config sandbox.allowed_packages): {bad}")
            return [py, "-m", "pip", "install", "--quiet", *pkgs]
        if parts[0] == "pytest":
            return [py, "-m", "pytest", *parts[1:]]
        return [py, *parts[1:]]

    def run(self, command: str) -> tuple[int, str]:
        argv = self.command_argv(command)
        try:
            proc = subprocess.run(argv, cwd=self.root, env=self._env(), capture_output=True, text=True,
                                  timeout=self.timeout, encoding="utf-8", errors="replace")
        except subprocess.TimeoutExpired:
            return 124, f"timed out after {self.timeout}s"
        return proc.returncode, (proc.stdout + proc.stderr)[-6000:]

    # --- packaging -------------------------------------------------------------
    def package(self, dest: str | Path, extra: dict[str, str] | None = None) -> Path:
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
            for rel in self.files():
                z.write(self.root / rel, rel)
            for name, text in (extra or {}).items():
                z.writestr(name, text)
        return dest
