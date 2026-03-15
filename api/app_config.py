from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AppConfig:
    test_mode: bool = False
    mock_data_dir: str = "Test"
    mock_ply_file: str = "mockup.ply"
    mock_glb_file: str = "mockup.glb"
    mock_sleep_seconds: int = 10
    auth_mode: bool = False
    api_keys: frozenset = frozenset()


def load_app_config() -> AppConfig:
    """Load app configuration from JSON file.

    Config file path can be overridden with APP_CONFIG_FILE.
    Defaults to repository-root app_config.json.
    """
    config_path = Path(os.environ.get("APP_CONFIG_FILE", "app_config.json"))
    if not config_path.exists():
        return AppConfig()

    with config_path.open("r", encoding="utf-8") as f:
        raw = json.load(f)

    return AppConfig(
        test_mode=bool(raw.get("TestMode", False)),
        mock_data_dir=str(raw.get("MockDataDir", "Test")),
        mock_ply_file=str(raw.get("MockPlyFile", "mockup.ply")),
        mock_glb_file=str(raw.get("MockGlbFile", "mockup.glb")),
        mock_sleep_seconds=int(raw.get("MockSleepSeconds", 10)),
        auth_mode=bool(raw.get("AuthMode", False)),
        api_keys=frozenset(str(k) for k in raw.get("ApiKeys", [])),
    )
