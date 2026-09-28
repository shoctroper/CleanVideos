from __future__ import annotations

from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_config(path: Path | None = None) -> dict:
    cfg_path = path or (PROJECT_ROOT / "config.yaml")
    with open(cfg_path) as f:
        cfg = yaml.safe_load(f)

    for key in ("watch_folder", "processing_dir", "output_dir", "logs_dir"):
        cfg[key] = str((PROJECT_ROOT / cfg[key]).resolve())
    cfg["state_db"] = str((PROJECT_ROOT / cfg["state_db"]).resolve())
    cfg["models_dir"] = str((PROJECT_ROOT / "models").resolve())

    return cfg
