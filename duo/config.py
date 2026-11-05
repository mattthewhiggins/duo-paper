"""Loads config.toml and .env into validated, read-only settings."""
from __future__ import annotations

import os
import tomllib
import urllib.parse
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
QUOTES = {"USD", "GBP", "EUR"}
EFFORTS = {"low", "medium", "high", "xhigh", "max"}
LIVE_CONFIRM_PHRASE = "trade real money"
# Binance's public market data, tried in this order. Both serve Binance's own spot klines. data-api.binance.vision is
# its market-data-only host, which answers from places where api.binance.com refuses (HTTP 451, as for US addresses).
BINANCE_URLS = ("https://data-api.binance.vision", "https://api.binance.com")

# USD per million tokens: input, output, cache read. Cache writes cost 1.25x input.
# Includes the models the API can fall back to when a request is declined.
PRICES = {
    "claude-fable-5-1": (10.0, 50.0, 0.25),
    "claude-opus-5-5": (4.0, 20.0, 0.20),
    "claude-opus-5": (5.0, 25.0, 0.50),
    "claude-opus-4-8": (5.0, 25.0, 0.50),
    "claude-sonnet-5-5": (2.0, 10.0, 0.20),
    "claude-sonnet-5": (2.0, 10.0, 0.20),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
}
# An unlisted model is billed at the highest rate, so budgets fail on the safe side.
UNKNOWN_MODEL_PRICE = (10.0, 50.0, 1.0)


def D(value) -> Decimal:
    return Decimal(str(value))


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class RiskConfig:
    max_order_pct: Decimal
    max_asset_pct: Decimal
    min_cash_pct: Decimal
    max_buys_per_day: int
    max_fees_30d_pct: Decimal
    min_target_fee_multiple: Decimal
    resting_ttl_minutes: int
    max_active_plans: int
    max_plan_hours: int
    max_trigger_distance_pct: Decimal
    max_limit_slippage_pct: Decimal
    stop_limit_slippage_pct: Decimal
    min_stop_distance_pct: Decimal
    max_stop_distance_pct: Decimal
    daily_loss_pause_pct: Decimal
    drawdown_halt_pct: Decimal
    max_quote_age_seconds: int


@dataclass(frozen=True)
class AgentConfig:
    model: str
    effort: str
    discussion_minutes: int
    discussion_model: str
    discussion_effort: str
    max_decisions_per_day: int
    max_rounds: int
    max_steps_per_turn: int
    min_check_in_minutes: int
    max_check_in_minutes: int
    wake_on_move_pct: Decimal
    session_budget_usd: float
    daily_budget_usd: float
    monthly_budget_usd: float


@dataclass(frozen=True)
class SystematicConfig:
    """The [systematic] section: a research strategy run by code, with its own hard limits. Paper only."""
    enabled: bool
    strategy: str
    agents_enabled: bool            # Agent discussions in systematic mode. They can never trade either way.
    starting_capital_gbp: Decimal   # The research's account size. Converted once to the book's currency at start.
    maker_pct: Decimal              # Kraken Pro Tier 3, which preregistration section 5 says GBP 20,000 qualifies for.
    taker_pct: Decimal
    loop_minutes: int               # Health checks, data freshness and order management.
    settle_minutes: int             # The daily decision waits this long after 00:00 UTC.
    reprice_minutes: int            # A resting order is moved to the current best price this often.
    taker_after_minutes: int        # After this long, what's left crosses the spread...
    taker_cap_pct: Decimal          # ...at no worse than this far from the quote.
    history_days: int               # Daily bars fetched for each decision.
    max_asset_pct: Decimal          # Per-asset ceiling, whatever the strategy's sleeve.
    asset_margin_pct: Decimal       # Per-asset limit = largest sleeve + this, capped at max_asset_pct.
    max_gross_pct: Decimal          # Sum of holdings. No shorts, no leverage.
    order_margin_pct: Decimal       # No order above its target change + this much of equity.
    daily_loss_pct: Decimal         # Down this much from the UTC day's start: no buys until tomorrow.
    kill_drawdown_pct: Decimal      # Down this much from peak: cancel, flatten, halt until resumed.
    signal_stale_minutes: int       # No new orders if the just-closed bar is still missing this long after 00:00 UTC.
    max_quote_age_seconds: int      # No new orders on a quote older than this...
    max_frozen_quote_seconds: int   # ...or one that hasn't changed at all for this long.
    max_api_errors: int             # No new orders after this many API errors in a row.
    heartbeat_stale_minutes: int    # The watchdog reports the bot down past this.
    degraded_alert_minutes: int = 30    # ...and alerts when it has been degraded (no new orders) this long.
    allow_capped_targets: bool = False  # Run a strategy whose targets can pass max_asset_pct, capped.
    binance_urls: tuple[str, ...] = BINANCE_URLS    # Signal data hosts, tried in order.
    # Scheduled runs only: python -m duo tick, one poll per fresh process every 15 to 60 minutes. The loop ignores them.
    cron_gap_minutes: int = 180             # A longer gap since the last heartbeat is an outage.
    cron_decision_grace_minutes: int = 120  # The decision-overdue alert waits this long after 00:00 UTC.
    cron_slack_seconds: int = 120           # Re-pricing and the switch to taker come this much early (cron jitter).


@dataclass(frozen=True)
class Config:
    mode: str
    quote: str
    starting_capital: Decimal
    assets: tuple[str, ...]
    maker_pct: Decimal
    taker_pct: Decimal
    paper_slippage_pct: Decimal
    paper_trade_through_pct: Decimal
    risk: RiskConfig
    agents: AgentConfig
    poll_seconds: int
    reconcile_seconds: int
    snapshot_minutes: int
    host: str
    port: int
    data_dir: Path
    systematic: SystematicConfig | None = None

    @property
    def systematic_on(self) -> bool:
        return self.systematic is not None and self.systematic.enabled

    @property
    def db_path(self) -> Path:
        # Paper and live keep separate ledgers, so paper results can't leak into live accounting.
        # The systematic runner has its own, so it never writes to the agents' paper.db.
        if self.systematic_on:
            return self.data_dir / f"{self.mode}_systematic.db"
        return self.data_dir / f"{self.mode}.db"


def load_env(path: Path = ROOT / ".env") -> None:
    """Reads KEY=VALUE lines into the environment. Real environment variables win."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip("'\"")
        if value:
            os.environ.setdefault(key.strip(), value)


def _pct(value, name: str, low: float = 0, high: float = 100) -> Decimal:
    number = D(value)
    if not (low <= number <= high):
        raise ConfigError(f"{name} must be between {low} and {high}, got {value}")
    return number


SYSTEMATIC_DEFAULTS = {
    "enabled": False, "strategy": "F1__ENS__BTC_ETH__vol40", "agents_enabled": False,
    "starting_capital_gbp": 20000, "maker_pct": 0.22, "taker_pct": 0.38,
    "loop_minutes": 5, "settle_minutes": 2, "reprice_minutes": 15, "taker_after_minutes": 120, "taker_cap_pct": 0.5,
    "history_days": 600, "max_asset_pct": 50, "asset_margin_pct": 2, "max_gross_pct": 100, "order_margin_pct": 2,
    "daily_loss_pct": 8, "kill_drawdown_pct": 45, "signal_stale_minutes": 30, "max_quote_age_seconds": 90,
    "max_frozen_quote_seconds": 600, "max_api_errors": 5, "heartbeat_stale_minutes": 15,
    "degraded_alert_minutes": 30, "allow_capped_targets": False, "binance_urls": list(BINANCE_URLS),
    "cron_gap_minutes": 180, "cron_decision_grace_minutes": 120, "cron_slack_seconds": 120,
}


def _https_urls(value, name: str) -> tuple[str, ...]:
    """A non-empty list of plain https:// base URLs, each without a trailing slash."""
    problem = f"{name} must be a list of https:// URLs, at least one"
    if not isinstance(value, (list, tuple)) or not value:
        raise ConfigError(problem)
    urls = []
    for url in value:
        parsed = urllib.parse.urlsplit(url) if isinstance(url, str) else None
        if parsed is None or parsed.scheme != "https" or not parsed.hostname or "@" in parsed.netloc \
                or parsed.query or parsed.fragment:
            raise ConfigError(f"{problem}, got {url!r}")
        urls.append(url.rstrip("/"))
    return tuple(urls)


def _systematic(raw: dict | None) -> SystematicConfig | None:
    if raw is None:
        return None
    unknown = set(raw) - set(SYSTEMATIC_DEFAULTS)
    if unknown:
        raise ConfigError(f"[systematic] has unknown settings: {', '.join(sorted(unknown))}")
    s = {**SYSTEMATIC_DEFAULTS, **raw}
    whole = lambda name, low=1, high=10**6: _whole(s[name], f"systematic.{name}", low, high)
    pct = lambda name, low=0, high=100: _pct(s[name], f"systematic.{name}", low, high)
    if not all(isinstance(s[k], bool) for k in ("enabled", "agents_enabled", "allow_capped_targets")):
        raise ConfigError("systematic.enabled, systematic.agents_enabled and systematic.allow_capped_targets must be "
                          "true or false")
    if not isinstance(s["strategy"], str) or not s["strategy"].strip():
        raise ConfigError("systematic.strategy must be a research variant id")
    capital = D(s["starting_capital_gbp"])
    if not capital.is_finite() or capital <= 0:
        raise ConfigError("systematic.starting_capital_gbp must be above zero")
    config = SystematicConfig(
        enabled=s["enabled"], strategy=s["strategy"].strip(), agents_enabled=s["agents_enabled"],
        starting_capital_gbp=capital, maker_pct=pct("maker_pct", 0, 10), taker_pct=pct("taker_pct", 0, 10),
        loop_minutes=whole("loop_minutes", 1, 60), settle_minutes=whole("settle_minutes", 0, 60),
        reprice_minutes=whole("reprice_minutes", 1, 600), taker_after_minutes=whole("taker_after_minutes", 1, 1440),
        taker_cap_pct=pct("taker_cap_pct", 0, 5), history_days=whole("history_days", 300, 1000),
        max_asset_pct=pct("max_asset_pct", 1, 100), asset_margin_pct=pct("asset_margin_pct", 0, 20),
        max_gross_pct=pct("max_gross_pct", 1, 100), order_margin_pct=pct("order_margin_pct", 0, 20),
        daily_loss_pct=pct("daily_loss_pct", 0.1, 100), kill_drawdown_pct=pct("kill_drawdown_pct", 1, 50),
        signal_stale_minutes=whole("signal_stale_minutes", 1, 1440),
        max_quote_age_seconds=whole("max_quote_age_seconds", 1, 3600),
        max_frozen_quote_seconds=whole("max_frozen_quote_seconds", 30, 86400),
        max_api_errors=whole("max_api_errors", 1, 1000), heartbeat_stale_minutes=whole("heartbeat_stale_minutes", 1, 1440),
        degraded_alert_minutes=whole("degraded_alert_minutes", 1, 1440),
        allow_capped_targets=s["allow_capped_targets"],
        binance_urls=_https_urls(s["binance_urls"], "systematic.binance_urls"),
        cron_gap_minutes=whole("cron_gap_minutes", 1, 1440),
        cron_decision_grace_minutes=whole("cron_decision_grace_minutes", 1, 1440),
        cron_slack_seconds=whole("cron_slack_seconds", 0, 3600),
    )
    if config.daily_loss_pct >= config.kill_drawdown_pct:
        raise ConfigError("systematic.daily_loss_pct must be below systematic.kill_drawdown_pct")
    if config.reprice_minutes >= config.taker_after_minutes:
        raise ConfigError("systematic.reprice_minutes must be below systematic.taker_after_minutes")
    if config.cron_slack_seconds >= config.reprice_minutes * 60:
        raise ConfigError("systematic.cron_slack_seconds must be below systematic.reprice_minutes, in seconds")
    return config


def _whole(value, name: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ConfigError(f"{name} must be a whole number from {low} to {high}, got {value!r}")
    return value


def load(path: Path = ROOT / "config.toml", data_dir: Path | None = None) -> Config:
    try:
        raw = tomllib.loads(Path(path).read_text())
        portfolio, fees, risk, agents = raw["portfolio"], raw["fees"], raw["risk"], raw["agents"]
        engine, dashboard = raw["engine"], raw["dashboard"]

        mode = raw["mode"]
        if mode not in ("paper", "live"):
            raise ConfigError(f'mode must be "paper" or "live", got {mode!r}')
        quote = portfolio["quote"].upper()
        if quote not in QUOTES:
            raise ConfigError(f"quote must be one of {sorted(QUOTES)}, got {quote!r}")
        capital = D(portfolio["starting_capital"])
        if capital <= 0:
            raise ConfigError("starting_capital must be above zero")
        assets = tuple(a.upper() for a in portfolio["assets"])
        if not assets:
            raise ConfigError("assets can't be empty")
        if agents["effort"] not in EFFORTS or agents["discussion_effort"] not in EFFORTS:
            raise ConfigError(f"effort must be one of {sorted(EFFORTS)}")
        if agents["min_check_in_minutes"] > agents["max_check_in_minutes"]:
            raise ConfigError("min_check_in_minutes can't exceed max_check_in_minutes")

        risk_cfg = RiskConfig(
            max_order_pct=_pct(risk["max_order_pct"], "max_order_pct"),
            max_asset_pct=_pct(risk["max_asset_pct"], "max_asset_pct"),
            min_cash_pct=_pct(risk["min_cash_pct"], "min_cash_pct"),
            max_buys_per_day=int(risk["max_buys_per_day"]),
            max_fees_30d_pct=_pct(risk["max_fees_30d_pct"], "max_fees_30d_pct"),
            min_target_fee_multiple=_pct(risk["min_target_fee_multiple"], "min_target_fee_multiple", 1, 20),
            resting_ttl_minutes=int(risk["resting_ttl_minutes"]),
            max_active_plans=int(risk["max_active_plans"]),
            max_plan_hours=int(risk["max_plan_hours"]),
            max_trigger_distance_pct=_pct(risk["max_trigger_distance_pct"], "max_trigger_distance_pct"),
            max_limit_slippage_pct=_pct(risk["max_limit_slippage_pct"], "max_limit_slippage_pct", 0, 10),
            stop_limit_slippage_pct=_pct(risk["stop_limit_slippage_pct"], "stop_limit_slippage_pct", 0, 20),
            min_stop_distance_pct=_pct(risk["min_stop_distance_pct"], "min_stop_distance_pct"),
            max_stop_distance_pct=_pct(risk["max_stop_distance_pct"], "max_stop_distance_pct"),
            daily_loss_pause_pct=_pct(risk["daily_loss_pause_pct"], "daily_loss_pause_pct"),
            drawdown_halt_pct=_pct(risk["drawdown_halt_pct"], "drawdown_halt_pct"),
            max_quote_age_seconds=int(risk["max_quote_age_seconds"]),
        )
        if risk_cfg.min_stop_distance_pct >= risk_cfg.max_stop_distance_pct:
            raise ConfigError("min_stop_distance_pct must be below max_stop_distance_pct")

        return Config(
            mode=mode,
            quote=quote,
            starting_capital=capital,
            assets=assets,
            maker_pct=_pct(fees["maker_pct"], "maker_pct", 0, 10),
            taker_pct=_pct(fees["taker_pct"], "taker_pct", 0, 10),
            paper_slippage_pct=_pct(fees["paper_slippage_pct"], "paper_slippage_pct", 0, 10),
            paper_trade_through_pct=_pct(fees["paper_trade_through_pct"], "paper_trade_through_pct", 0, 10),
            risk=risk_cfg,
            agents=AgentConfig(
                model=agents["model"],
                effort=agents["effort"],
                discussion_minutes=int(agents["discussion_minutes"]),
                discussion_model=agents["discussion_model"],
                discussion_effort=agents["discussion_effort"],
                max_decisions_per_day=int(agents["max_decisions_per_day"]),
                max_rounds=int(agents["max_rounds"]),
                max_steps_per_turn=int(agents["max_steps_per_turn"]),
                min_check_in_minutes=int(agents["min_check_in_minutes"]),
                max_check_in_minutes=int(agents["max_check_in_minutes"]),
                wake_on_move_pct=D(agents["wake_on_move_pct"]),
                session_budget_usd=float(agents["session_budget_usd"]),
                daily_budget_usd=float(agents["daily_budget_usd"]),
                monthly_budget_usd=float(agents["monthly_budget_usd"]),
            ),
            poll_seconds=int(engine["poll_seconds"]),
            reconcile_seconds=int(engine["reconcile_seconds"]),
            snapshot_minutes=int(engine["snapshot_minutes"]),
            host=dashboard["host"],
            port=int(dashboard["port"]),
            data_dir=Path(data_dir or os.environ.get("DUO_DATA_DIR") or ROOT / "data"),
            systematic=_systematic(raw.get("systematic")),
        )
    except KeyError as missing:
        raise ConfigError(f"config.toml is missing {missing}") from None
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"config.toml isn't valid: {error}") from None
