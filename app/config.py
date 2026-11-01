"""Runtime settings, read from environment variables / a local .env file."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    raw = _env(name, "1" if default else "0").lower()
    return raw in {"1", "true", "yes", "on"}


def _int_list(name: str, default: str) -> tuple[int, ...]:
    raw = _env(name, default)
    return tuple(sorted({int(x) for x in raw.split(",") if x.strip().isdigit()}, reverse=True))


@dataclass(frozen=True)
class Settings:
    # eBay Browse API (developer.ebay.com -> Application Keys, "Production" keyset)
    ebay_client_id: str = _env("EBAY_CLIENT_ID")
    ebay_client_secret: str = _env("EBAY_CLIENT_SECRET")
    ebay_env: str = _env("EBAY_ENV", "production")
    ebay_marketplace: str = _env("EBAY_MARKETPLACE", "EBAY_US")
    ship_to_country: str = _env("SHIP_TO_COUNTRY", "SG")
    ship_to_zip: str = _env("SHIP_TO_ZIP", "")
    ebay_daily_call_budget: int = int(_float("EBAY_DAILY_CALL_BUDGET", 5000))

    # Telegram (talk to @BotFather to create a bot and get the token)
    telegram_token: str = _env("TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str = _env("TELEGRAM_CHAT_ID")

    # Schedules
    scan_interval_hours: float = _float("SCAN_INTERVAL_HOURS", 3)
    track_interval_minutes: float = _float("TRACK_INTERVAL_MINUTES", 5)
    alert_minutes: tuple[int, ...] = field(default_factory=lambda: _int_list("ALERT_MINUTES", "60,15,5"))
    max_results_per_keyword: int = int(_float("MAX_RESULTS_PER_KEYWORD", 100))
    max_active_tracked: int = int(_float("MAX_ACTIVE_TRACKED", 40))
    auto_track_hours: float = _float("AUTO_TRACK_HOURS", 24)  # auto-track deals ending within this window

    # Price-list tally
    near_pct: float = _float("NEAR_PCT", 10)  # within this % above target counts as "near"
    import_tax_pct: float = _float("IMPORT_TAX_PCT", 9)  # SG GST eBay collects on low-value imports
    base_currency: str = "SGD"
    timezone: str = _env("TIMEZONE", "Asia/Singapore")

    # App
    host: str = _env("HOST", "127.0.0.1")
    port: int = int(_float("PORT", 8787))
    db_path: Path = Path(_env("DB_PATH", str(ROOT / "data" / "carnival.db")))
    force_demo: bool = _bool("DEMO_MODE", False)

    @property
    def demo_mode(self) -> bool:
        return self.force_demo or not (self.ebay_client_id and self.ebay_client_secret)

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_token)


settings = Settings()
