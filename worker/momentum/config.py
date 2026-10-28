"""Validated configuration. There is deliberately no live execution mode."""
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from hashlib import sha256
import json
import os
from pathlib import Path
import re

PAPER_API_HOST = "https://paper-api.alpaca.markets"
DATA_API_HOST = "https://data.alpaca.markets"
SYMBOL = re.compile(r"^[A-Z][A-Z0-9.-]{0,9}$")

class ConfigError(ValueError):
    pass

def flag(value, name):
    if value is None or value == "":
        return False
    if str(value).lower() not in {"true", "false", "1", "0"}:
        raise ConfigError(f"{name} must be true or false")
    return str(value).lower() in {"true", "1"}

@dataclass(frozen=True)
class Config:
    mode: str = "demo"
    paper_enabled: bool = False
    paper_reviewed_config_hash: str = ""
    api_key: str = field(default="", repr=False)
    api_secret: str = field(default="", repr=False)
    control_token: str = field(default="", repr=False)
    data_feed: str = "iex"
    watchlist: tuple = ()
    crypto_watchlist: tuple = ()  # optional read-only spot crypto, never stock strategy inputs
    db_path: str = "./data/momentum.sqlite3"
    export_dir: str = "./exports"
    metadata_path: str = "./symbol_metadata.json"
    host: str = "127.0.0.1"
    port: int = 8787
    poll_seconds: float = 5.0
    quote_max_age_seconds: float = 2.0
    order_ttl_seconds: float = 15.0
    demo_equity: str = "10000"
    cors_origin: str = ""
    strategy_overrides: dict = field(default_factory=dict)
    risk_overrides: dict = field(default_factory=dict)
    supabase_url: str = ""
    supabase_service_role_key: str = field(default="", repr=False)
    owner_user_id: str = ""
    accounting_history_start: str = ""
    accounting_baseline_path: str = ""
    accounting_cost_policy: str = "actual"

    def __post_init__(self):
        if self.mode not in {"demo", "paper"}:
            raise ConfigError("MOMENTUM_MODE must be demo or paper; live execution is not implemented")
        if self.data_feed not in {"iex", "sip"}:
            raise ConfigError("ALPACA_DATA_FEED must be iex or sip")
        if len(self.watchlist) > 10 or any(not SYMBOL.fullmatch(s) for s in self.watchlist):
            raise ConfigError("WATCHLIST must contain at most 10 valid uppercase equity symbols")
        if self.crypto_watchlist:
            try:
                from .crypto_observation import _pairs
                _pairs(self.crypto_watchlist)
                # This first implementation is restricted to reviewed US spot pairs.
                if any(p not in {"BTC/USD", "ETH/USD", "SOL/USD"} for p in self.crypto_watchlist):
                    raise ValueError
            except (TypeError, ValueError) as exc:
                raise ConfigError("CRYPTO_WATCHLIST must contain 1-10 unique uppercase USD crypto pairs") from exc
        if not 1 <= self.port <= 65535:
            raise ConfigError("PORT must be between 1 and 65535")
        if not 1 <= self.poll_seconds <= 300:
            raise ConfigError("POLL_SECONDS must be between 1 and 300")
        if not 0 < self.quote_max_age_seconds <= 5:
            raise ConfigError("QUOTE_MAX_AGE_SECONDS must be greater than 0 and at most 5")
        if not 1 <= self.order_ttl_seconds <= 300:
            raise ConfigError("ORDER_TTL_SECONDS must be between 1 and 300")
        if not Decimal(self.demo_equity).is_finite() or Decimal(self.demo_equity) <= 0:
            raise ConfigError("DEMO_EQUITY must be a positive finite number")
        if self.cors_origin == "*":
            raise ConfigError("CORS_ORIGIN must be one exact origin, not a wildcard")
        if self.accounting_cost_policy not in {"actual", "paper_zero_fee_simulation"}:
            raise ConfigError("ACCOUNTING_COST_POLICY must be actual or paper_zero_fee_simulation")
        if self.accounting_history_start:
            try:
                start = datetime.fromisoformat(self.accounting_history_start.replace("Z", "+00:00"))
                if start.tzinfo is None:
                    raise ValueError
            except (ValueError, TypeError, AttributeError) as exc:
                raise ConfigError("ACCOUNTING_HISTORY_START must be a timezone-qualified timestamp") from exc
        if self.accounting_baseline_path and not self.accounting_history_start:
            raise ConfigError("An accounting baseline requires ACCOUNTING_HISTORY_START")
        try:
            from .strategy import StrategyConfig
            from .risk import RiskConfig
            StrategyConfig(**self.strategy_overrides)
            RiskConfig(**self.risk_overrides)
        except (ValueError, TypeError, ArithmeticError) as exc:
            raise ConfigError("Invalid or unsupported strategy/risk configuration") from exc

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        for name in ("APCA_API_BASE_URL", "ALPACA_API_BASE_URL", "ALPACA_BASE_URL"):
            if env.get(name) and env[name].rstrip("/") != PAPER_API_HOST:
                raise ConfigError(f"{name} cannot override the fixed paper API host")
        if env.get("ALPACA_PAPER_TRADE", "true").lower() not in {"true", "1", "yes"}:
            raise ConfigError("ALPACA_PAPER_TRADE cannot enable another execution environment")
        def obj(name):
            try:
                value = json.loads(env.get(name, "{}"))
            except json.JSONDecodeError as exc:
                raise ConfigError(f"{name} must be a JSON object") from exc
            if not isinstance(value, dict):
                raise ConfigError(f"{name} must be a JSON object")
            return value
        try:
            return cls(
                mode=env.get("MOMENTUM_MODE", "demo").lower(),
                paper_enabled=flag(env.get("PAPER_TRADING_ENABLED"), "PAPER_TRADING_ENABLED"),
                paper_reviewed_config_hash=env.get("PAPER_REVIEWED_CONFIG_HASH", ""),
                api_key=env.get("ALPACA_API_KEY", ""), api_secret=env.get("ALPACA_SECRET_KEY", ""),
                control_token=env.get("WORKER_CONTROL_TOKEN", ""),
                data_feed=env.get("ALPACA_DATA_FEED", "iex"),
                watchlist=tuple(dict.fromkeys(s.strip().upper() for s in env.get("WATCHLIST", "").split(",") if s.strip())),
                crypto_watchlist=tuple(s.strip().upper() for s in env.get("CRYPTO_WATCHLIST", "").split(",") if s.strip()),
                db_path=env.get("JOURNAL_PATH", "./data/momentum.sqlite3"),
                export_dir=env.get("EXPORT_DIR", "./exports"),
                metadata_path=env.get("SYMBOL_METADATA_PATH", "./symbol_metadata.json"),
                host=env.get("HOST", "127.0.0.1"), port=int(env.get("PORT", "8787")),
                poll_seconds=float(env.get("POLL_SECONDS", "5")),
                quote_max_age_seconds=float(env.get("QUOTE_MAX_AGE_SECONDS", "2")),
                order_ttl_seconds=float(env.get("ORDER_TTL_SECONDS", "15")),
                demo_equity=env.get("DEMO_EQUITY", "10000"),
                cors_origin=env.get("CORS_ORIGIN", ""),
                strategy_overrides=obj("STRATEGY_CONFIG_JSON"), risk_overrides=obj("RISK_CONFIG_JSON"),
                supabase_url=env.get("SUPABASE_URL", ""),
                supabase_service_role_key=env.get("SUPABASE_SERVICE_ROLE_KEY", ""),
                owner_user_id=env.get("OWNER_USER_ID", ""),
                accounting_history_start=env.get("ACCOUNTING_HISTORY_START", ""),
                accounting_baseline_path=env.get("ACCOUNTING_BASELINE_PATH", ""),
                accounting_cost_policy=env.get("ACCOUNTING_COST_POLICY", "actual"),
            )
        except (ValueError, ArithmeticError) as exc:
            if isinstance(exc, ConfigError):
                raise
            raise ConfigError("Invalid numeric worker configuration") from exc

    def paper_blockers(self):
        reasons = []
        if self.mode != "paper":
            reasons.append("demo_mode")
        if not self.paper_enabled:
            reasons.append("paper_execution_not_enabled")
        if not self.api_key or not self.api_secret:
            reasons.append("paper_credentials_missing")
        if not self.watchlist:
            reasons.append("watchlist_missing")
        if not Path(self.metadata_path).is_file():
            reasons.append("point_in_time_symbol_metadata_missing")
        if self.paper_reviewed_config_hash != self.review_hash():
            reasons.append("exact_paper_configuration_not_reviewed")
        return reasons

    def review_hash(self):
        from .risk import RiskConfig
        from .strategy import StrategyConfig
        settings = {"strategy_config_id": StrategyConfig(**self.strategy_overrides).config_id,
                    "risk_config_id": RiskConfig(**self.risk_overrides).config_id,
                    "watchlist": list(self.watchlist), "feed": self.data_feed,
                    "quote_max_age_seconds": self.quote_max_age_seconds,
                    "order_ttl_seconds": self.order_ttl_seconds,
                    "accounting_cost_policy": self.accounting_cost_policy,
                    "accounting_history_start": self.accounting_history_start,
                    "order_type": "limit", "time_in_force": "day", "extended_hours": True,
                    "execution_host": PAPER_API_HOST}
        return sha256(json.dumps(settings, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def public(self):
        return {
            "mode": self.mode, "paper_enabled": self.paper_enabled,
            "paper_host": PAPER_API_HOST, "data_host": DATA_API_HOST, "data_feed": self.data_feed,
            "paper_credentials_present": bool(self.api_key and self.api_secret),
            "watchlist": list(self.watchlist), "crypto_watchlist": list(self.crypto_watchlist),
            "quote_max_age_seconds": self.quote_max_age_seconds,
            "order_ttl_seconds": self.order_ttl_seconds,
            "strategy_overrides": self.strategy_overrides, "risk_overrides": self.risk_overrides,
            "demo_equity": self.demo_equity,
            "control_auth_configured": bool(self.control_token),
            "paper_configuration_hash": self.review_hash(),
            "paper_configuration_reviewed": self.paper_reviewed_config_hash == self.review_hash(),
            "supabase_publisher_configured": bool(self.supabase_url and self.supabase_service_role_key and self.owner_user_id),
            "accounting_history_start": self.accounting_history_start or None,
            "accounting_baseline_configured": bool(self.accounting_baseline_path),
            "accounting_cost_policy": self.accounting_cost_policy,
            "paper_blockers": self.paper_blockers(), "live_execution_supported": False,
        }

