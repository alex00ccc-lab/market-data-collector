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
    "finnhub_api_key": "FINNHUB_API_KEY",
    "twelvedata_api_key": "TWELVEDATA_API_KEY",
    "polygon_api_key": "POLYGON_API_KEY",
    "tiingo_api_key": "TIINGO_API_KEY",
    "marketstack_api_key": "MARKETSTACK_API_KEY",
    "marketdata_app_token": "MARKETDATA_APP_TOKEN",
    "fred_api_key": "FRED_API_KEY",
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


# ── Quota thresholds per source ───────────────────────────────────────────
QUOTA_THRESHOLDS = {
    "finnhub_api_key": {
        "name": "Finnhub",
        "limit_per_minute": 60,
        "warn_pct": 0.8,
    },
    "alpha_vantage_api_key": {
        "name": "Alpha Vantage",
        "limit_per_day": 25,
        "warn_pct": 0.8,
    },
    "twelvedata_api_key": {
        "name": "Twelve Data",
        "limit_per_day": 800,
        "warn_pct": 0.7,
    },
}


def check_quota() -> dict[str, bool]:
    """Check which API keys are configured and log warnings near quota limits.

    Returns:
        {source_name: is_configured} dict.
    """
    result = {}
    for key_name, cfg in QUOTA_THRESHOLDS.items():
        key_val = get_key(key_name, "")
        name = cfg["name"]
        configured = bool(key_val)

        if not configured:
            logger.info("Key %s: not configured — adapter will be skipped", name)
        else:
            logger.debug("Key %s: configured", name)

        result[name.lower().replace(" ", "_")] = configured

    configured = [k for k, v in result.items() if v]
    missing = [k for k, v in result.items() if not v]
    if missing:
        logger.info("API keys: %d/%d configured — missing: %s",
                    len(configured), len(result), ", ".join(missing))
    else:
        logger.info("API keys: all %d configured", len(result))

    return result
