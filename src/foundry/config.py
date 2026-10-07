"""Loads foundry.toml. Everything configurable comes from there."""

import os
import tomllib
from pathlib import Path

ROOT = Path(os.environ.get("FOUNDRY_ROOT", Path(__file__).resolve().parents[2]))


def load(path: Path | None = None) -> dict:
    path = path or Path(os.environ.get("FOUNDRY_CONFIG", ROOT / "foundry.toml"))
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    for key, value in cfg["paths"].items():
        cfg["paths"][key] = Path(value).expanduser()
    cfg["paths"]["state"].mkdir(parents=True, exist_ok=True)
    return cfg


def base_url(cfg: dict) -> str:
    return f"http://{cfg['server']['host']}:{cfg['server']['port']}/v1"


def state_dir(cfg: dict, *parts: str) -> Path:
    d = cfg["paths"]["state"].joinpath(*parts)
    d.mkdir(parents=True, exist_ok=True)
    return d


def repo_path(cfg: dict, name: str) -> Path:
    """A bare name resolves inside paths.repos; anything with a slash is a path."""
    if "/" in name or name in (".", ".."):
        return Path(name).expanduser().resolve()
    return cfg["paths"]["repos"] / name
