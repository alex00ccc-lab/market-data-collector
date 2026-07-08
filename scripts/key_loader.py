"""Key loader for market-data-collector — mirrors parent project's pattern.

Priority: env var > market_data/config/keys.yaml > D:\\.keys.yaml > default
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

ENV_MAP = {
    "alpha_vantage_api_key": "ALPHA_VANTAGE_API_KEY",
}

_SCRIPT_DIR = Path(__file__).resolve().parent.parent  # market_data/
_PROJECT_KEYS = _SCRIPT_DIR / "config" / "keys.yaml"
_PLATFORM_KEYS = Path("D:/.keys.yaml")


def _try_load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        import yaml
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return data if isinstance(data, dict) else {}
    except ImportError:
        return {}
    except Exception as e:
        logger.debug("Failed to parse %s: %s", path, e)
        return {}


def get_key(name: str, default: str = "") -> str:
    # 1. Env var
    env_name = ENV_MAP.get(name)
    if env_name:
        val = os.getenv(env_name, "").strip()
        if val:
            return val

    # 2. market_data/config/keys.yaml
    val = _try_load_yaml(_PROJECT_KEYS).get(name, "")
    if val and isinstance(val, str) and val.strip():
        return val.strip()

    # 3. D:\.keys.yaml
    val = _try_load_yaml(_PLATFORM_KEYS).get(name, "")
    if val and isinstance(val, str) and val.strip():
        return val.strip()

    return default
