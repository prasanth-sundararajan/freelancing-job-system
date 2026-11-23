from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def load_config(path: str | Path | None = None) -> dict:
    """Load config.yaml. Relative file paths inside it resolve against the project root."""
    path = Path(path) if path else ROOT / "config.yaml"
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    for key in ("database", "skills_file", "portfolio_file"):
        value = cfg.get(key)
        if value and value != ":memory:" and not Path(value).is_absolute():
            cfg[key] = str(ROOT / value)
    return cfg
