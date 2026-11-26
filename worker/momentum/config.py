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

def _strategy(overrides):
    from .strategy import load_strategy
    return load_strategy(overrides)

def _watchlist(raw):
    """Empty or omitted WATCHLIST agrees with the liquid IEX preset universe."""
    from .presets import LIQUID_IEX_UNIVERSE
    symbols = tuple(dict.fromkeys(part.strip().upper() for part in str(raw).split(",") if part.strip()))
    return symbols or LIQUID_IEX_UNIVERSE

class ConfigError(ValueError):
    pass

def _ai_cotrade(env):
    """Trading-relevant AI settings, only when AI-only paper entries are switched on."""
    from .ai_advisor import AdvisorConfig, AdvisorConfigError
    try:
        ai = AdvisorConfig.from_env(env)
    except AdvisorConfigError:
        return {}  # the advisor turns itself off on invalid settings
    if not (ai.active and ai.mode == "cotrade"):
        return {}
    return {"mode": ai.mode, "model": ai.model, "min_confidence": ai.min_confidence,
            "confluence_min_confidence": ai.confluence_min_confidence,
            "max_trades_per_day": ai.max_trades_per_day, "signal_max_age_seconds": ai.signal_max_age_seconds}

def _ai_paper_execute(env):
    """Reviewed Gemini paper-execution settings, only when paper_execute is switched on."""
    from .ai_paper_trader import reviewed_execute_settings
    return reviewed_execute_settings(env)

def flag(value, name):
    if value is None or value == "":
        return False
    if str(value).lower() not in {"true", "false", "1", "0"}:
        raise ConfigError(f"{name} must be true or false")
    return str(value).lower() in {"true", "1"}

def _day_trade_profile(raw):
    from .day_trade_profiles import normalize
    try:
        return normalize(raw)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc

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
    # Separate from observation. Default false leaves crypto watch-only.
    crypto_paper_execution_enabled: bool = False
    oanda_practice_api_token: str = field(default="", repr=False)
    oanda_practice_account_id: str = field(default="", repr=False)
    oanda_environment: str = ""
    oanda_practice_orders_enabled: bool = False
    forex_watchlist: tuple = ()  # optional read-only practice forex, never stock strategy inputs
    forex_risk_overrides: dict = field(default_factory=dict)
    # Opt-in stock day-trade profile (DAY_TRADE_PROFILES.md). Empty = off: the
    # default strategy runs and the reviewed hash is unchanged.
    day_trade_profile: str = ""
    db_path: str = "./data/momentum.sqlite3"
    export_dir: str = "./exports"
    metadata_path: str = "./symbol_metadata.json"
    host: str = "127.0.0.1"
    port: int = 8787
    poll_seconds: float = 5.0
    journal_sample_seconds: float = 60.0
    journal_min_free_mb: int = 64
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
    # Non-empty only when the optional AI advisor runs in cotrade mode; then it is
    # part of review_hash(), so AI-only paper entries need an explicit re-review.
    ai_cotrade: dict = field(default_factory=dict)
    # AI_ADVISOR_MODE=paper_execute only (AI_PAPER_TRADING.md). Empty means the
    # rule loop decides; absent from the hash until armed.
    ai_paper_execute: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.mode not in {"demo", "paper"}:
            raise ConfigError("MOMENTUM_MODE must be demo or paper; live execution is not implemented")
        if type(self.crypto_paper_execution_enabled) is not bool:
            raise ConfigError("CRYPTO_PAPER_EXECUTION_ENABLED must be true or false")
        if self.data_feed not in {"iex", "sip"}:
            raise ConfigError("ALPACA_DATA_FEED must be iex or sip")
        if len(self.watchlist) > 10 or any(not SYMBOL.fullmatch(s) for s in self.watchlist):
            raise ConfigError("WATCHLIST must contain at most 10 valid uppercase equity symbols")
        if self.crypto_watchlist:
            try:
                from .crypto_observation import _pairs
                from .paper_risk import CRYPTO_PAPER_PAIRS
                _pairs(self.crypto_watchlist)
                # This first implementation is restricted to reviewed US spot pairs.
                if any(p not in CRYPTO_PAPER_PAIRS for p in self.crypto_watchlist):
                    raise ValueError
            except (TypeError, ValueError) as exc:
                raise ConfigError("CRYPTO_WATCHLIST must contain 1-10 unique uppercase USD crypto pairs") from exc
        try:
            from .forex_risk import ForexRiskConfig
            from .oanda import parse_forex_watchlist, validate_account_id, validate_token
            if self.oanda_environment not in {"", "practice"}:
                raise ValueError("OANDA_ENVIRONMENT must be practice; live fxTrade is not implemented")
            if self.forex_watchlist:
                if parse_forex_watchlist(",".join(self.forex_watchlist)) != tuple(self.forex_watchlist):
                    raise ValueError
            if self.oanda_practice_api_token:
                validate_token(self.oanda_practice_api_token)
            if self.oanda_practice_account_id:
                validate_account_id(self.oanda_practice_account_id)
            if not isinstance(self.forex_risk_overrides, dict):
                raise ValueError("OANDA_FOREX_RISK_CONFIG_JSON must be a JSON object")
            ForexRiskConfig(**self.forex_risk_overrides)
        except (TypeError, ValueError, ArithmeticError) as exc:
            raise ConfigError("OANDA practice forex settings are invalid") from exc
        try:
            from .day_trade_profiles import normalize
            if normalize(self.day_trade_profile) != self.day_trade_profile:
                raise ValueError
        except ValueError as exc:
            raise ConfigError("DAY_TRADE_PROFILE must be empty, large_cap_day_trade_v1, ross_cameron_small_cap_v1 "
                              "or small_cap_candle_patterns_iex_v1") from exc
        if not 1 <= self.port <= 65535:
            raise ConfigError("PORT must be between 1 and 65535")
        if not 0 <= self.journal_sample_seconds <= 3600:
            raise ConfigError("JOURNAL_SAMPLE_SECONDS must be between 0 and 3600")
        if not 0 <= self.journal_min_free_mb <= 10240:
            raise ConfigError("JOURNAL_MIN_FREE_MB must be between 0 and 10240")
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
            from .risk import RiskConfig
            _strategy(self.strategy_overrides)
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
        try:
            from .oanda import parse_forex_watchlist, refuse_host_overrides, refuse_public_oanda_secrets
            refuse_host_overrides(env)
            refuse_public_oanda_secrets(env)
            forex_watchlist = parse_forex_watchlist(env.get("OANDA_FOREX_WATCHLIST", ""))
        except ValueError as exc:
            raise ConfigError(str(exc) or "Invalid OANDA practice configuration") from exc
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
                watchlist=_watchlist(env.get("WATCHLIST", "")),
                crypto_watchlist=tuple(s.strip().upper() for s in env.get("CRYPTO_WATCHLIST", "").split(",") if s.strip()),
                crypto_paper_execution_enabled=flag(env.get("CRYPTO_PAPER_EXECUTION_ENABLED"), "CRYPTO_PAPER_EXECUTION_ENABLED"),
                oanda_practice_api_token=env.get("OANDA_PRACTICE_API_TOKEN", ""),
                oanda_practice_account_id=env.get("OANDA_PRACTICE_ACCOUNT_ID", ""),
                oanda_environment=env.get("OANDA_ENVIRONMENT", "").strip().lower(),
                oanda_practice_orders_enabled=flag(env.get("OANDA_PRACTICE_ORDERS_ENABLED"), "OANDA_PRACTICE_ORDERS_ENABLED"),
                forex_watchlist=forex_watchlist,
                forex_risk_overrides=obj("OANDA_FOREX_RISK_CONFIG_JSON"),
                day_trade_profile=_day_trade_profile(env.get("DAY_TRADE_PROFILE", "")),
                db_path=env.get("JOURNAL_PATH", "./data/momentum.sqlite3"),
                export_dir=env.get("EXPORT_DIR", "./exports"),
                metadata_path=env.get("SYMBOL_METADATA_PATH", "./symbol_metadata.json"),
                host=env.get("HOST", "127.0.0.1"), port=int(env.get("PORT", "8787")),
                poll_seconds=float(env.get("POLL_SECONDS", "5")),
                journal_sample_seconds=float(env.get("JOURNAL_SAMPLE_SECONDS", "60")),
                journal_min_free_mb=int(env.get("JOURNAL_MIN_FREE_MB", "64")),
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
                ai_cotrade=_ai_cotrade(env),
                ai_paper_execute=_ai_paper_execute(env),
            )
        except (ValueError, ArithmeticError) as exc:
            if isinstance(exc, ConfigError):
                raise
            raise ConfigError("Invalid numeric worker configuration") from exc

    def _day_trade_report(self):
        from .day_trade_profiles import report
        return report(self.day_trade_profile, watchlist=self.watchlist, data_feed=self.data_feed,
                      risk_overrides=self.risk_overrides)

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
        strategy = _strategy(self.strategy_overrides)
        if strategy.allowed_symbols and any(symbol not in strategy.allowed_symbols for symbol in self.watchlist):
            reasons.append("watchlist_outside_strategy_universe")
        if strategy.requires_float_catalyst and not Path(self.metadata_path).is_file():
            reasons.append("point_in_time_symbol_metadata_missing")
        if strategy.preset_spec.gated and not strategy.enable_gated_presets:
            reasons.append("gated_preset_disabled")
        if strategy.preset_spec.required_feed == "sip" and self.data_feed != "sip":
            reasons.append("small_cap_requires_sip_feed")
        if strategy.preset_spec.required_feed == "iex" and self.data_feed != "iex":
            reasons.append("liquid_preset_requires_iex")
        if self.oanda_practice_orders_enabled:
            if self.oanda_environment != "practice":
                reasons.append("oanda_environment_not_practice")
            if not self.oanda_practice_api_token or not self.oanda_practice_account_id:
                reasons.append("oanda_practice_credentials_missing")
        if self.crypto_paper_execution_enabled and not self.crypto_watchlist:
            reasons.append("crypto_watchlist_missing")
        if self.day_trade_profile:
            from .day_trade_profiles import blockers
            reasons.extend(blockers(self.day_trade_profile, watchlist=self.watchlist, data_feed=self.data_feed))
        if self.paper_reviewed_config_hash != self.review_hash():
            reasons.append("exact_paper_configuration_not_reviewed")
        return reasons

    def review_hash(self):
        from .risk import RiskConfig
        strategy = _strategy(self.strategy_overrides)
        settings = {"strategy_config_id": strategy.config_id,
                    "strategy_preset": strategy.preset,
                    "risk_config_id": RiskConfig(**self.risk_overrides).config_id,
                    "watchlist": list(self.watchlist), "feed": self.data_feed,
                    "quote_max_age_seconds": self.quote_max_age_seconds,
                    "order_ttl_seconds": self.order_ttl_seconds,
                    "accounting_cost_policy": self.accounting_cost_policy,
                    "accounting_history_start": self.accounting_history_start,
                    "order_type": "limit", "time_in_force": "day", "extended_hours": True,
                    "execution_host": PAPER_API_HOST}
        if self.ai_cotrade:
            settings["ai_cotrade"] = dict(self.ai_cotrade)
        if self.ai_paper_execute:
            settings["ai_paper_execute"] = dict(self.ai_paper_execute)
        if self.oanda_practice_orders_enabled:
            from .forex_risk import ForexRiskConfig
            settings["oanda_practice_orders"] = {
                "enabled": True,
                "environment": self.oanda_environment,
                "rest_host": "api-fxpractice.oanda.com",
                "forex_risk_config_id": ForexRiskConfig(**self.forex_risk_overrides).config_id,
            }
        # Off by default, and absent from the hash until armed, so a crypto
        # watchlist alone still matches today's reviewed equity configuration.
        if self.crypto_paper_execution_enabled:
            settings["crypto_paper_orders"] = {
                "enabled": True,
                "execution_host": PAPER_API_HOST,
                "pairs": list(self.crypto_watchlist),
                "time_in_force": ["gtc", "ioc"],
            }
        # Off by default and absent from the hash, so today's reviewed hash is unchanged.
        if self.day_trade_profile:
            from .day_trade_profiles import hash_settings
            settings["day_trade_profile"] = hash_settings(self.day_trade_profile, self.risk_overrides, self.data_feed)
        return sha256(json.dumps(settings, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def public(self):
        return {
            "mode": self.mode, "paper_enabled": self.paper_enabled,
            "paper_host": PAPER_API_HOST, "data_host": DATA_API_HOST, "data_feed": self.data_feed,
            "paper_credentials_present": bool(self.api_key and self.api_secret),
            "watchlist": list(self.watchlist), "crypto_watchlist": list(self.crypto_watchlist),
            "crypto_paper_execution_enabled": bool(self.crypto_paper_execution_enabled),
            "crypto_paper_order_host": PAPER_API_HOST,
            "forex_watchlist": list(self.forex_watchlist),
            "oanda_practice_configured": bool(self.oanda_practice_api_token and self.oanda_practice_account_id),
            "oanda_environment": self.oanda_environment or None,
            "oanda_practice_orders_enabled": bool(self.oanda_practice_orders_enabled),
            "forex_risk_overrides": dict(self.forex_risk_overrides),
            "oanda_practice_rest_host": "api-fxpractice.oanda.com",
            "oanda_practice_stream_host": "stream-fxpractice.oanda.com",
            "strategy_preset": _strategy(self.strategy_overrides).preset,
            "day_trade_profile": self._day_trade_report(),
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
            "ai_cotrade_reviewed_settings": dict(self.ai_cotrade) or None,
            "ai_paper_execute_reviewed_settings": dict(self.ai_paper_execute) or None,
            "paper_blockers": self.paper_blockers(), "live_execution_supported": False,
        }

