"""The conversation: a Strategist and a Risk Manager, each its own Claude conversation, talking through the engine."""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable

from .config import PRICES, UNKNOWN_MODEL_PRICE, Config, D
from .db import DB
from .market import CANDLE_INTERVALS, Market
from .research import test_rule
from .risk import IMMEDIATE, Order

MAX_TOKENS = 16000
MAX_CANDLES = 200
FALLBACK_BETA = "server-side-fallback-2026-07-01"
DESK_MAX_STEPS = 4
BRIEF_TEXT_LIMIT = 600


class BudgetExceeded(Exception):
    pass


class AgentFailed(Exception):
    pass


def has_credentials() -> bool:
    return bool(
        os.environ.get("ANTHROPIC_API_KEY")
        or os.environ.get("ANTHROPIC_AUTH_TOKEN")
        or (Path.home() / ".config" / "anthropic").exists()
    )


class Budget:
    def __init__(self, db: DB, config: Config):
        self.db = db
        self.agents = config.agents

    def _spent(self, where: str, params) -> float:
        return self.db.one(f"SELECT COALESCE(SUM(cost_usd), 0) AS s FROM api_usage WHERE {where}", params)["s"]

    def today(self) -> float:
        now = time.time()
        return self._spent("ts >= ?", (now - now % 86400,))

    def this_month(self) -> float:
        start = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return self._spent("ts >= ?", (start.timestamp(),))

    def total(self) -> float:
        return self._spent("1 = 1", ())

    def session(self, session_id: int) -> float:
        return self._spent("session_id = ?", (session_id,))

    def blocker(self, session_id: int | None = None) -> str | None:
        """Why the agents can't spend more right now, or None."""
        a = self.agents
        if self.this_month() >= a.monthly_budget_usd:
            return f"the ${a.monthly_budget_usd:.2f} monthly API budget is used up"
        if self.today() >= a.daily_budget_usd:
            return f"the ${a.daily_budget_usd:.2f} daily API budget is used up"
        if session_id is not None and self.session(session_id) >= a.session_budget_usd:
            return f"this session hit its ${a.session_budget_usd:.2f} API limit"
        return None


def _shared_prompt(config: Config) -> str:
    cheap_trip, full_trip = config.maker_pct * 2, config.taker_pct * 2
    return f"""You are one of two Claude agents jointly running a small spot crypto portfolio on Kraken. Its owner set this up as an experiment, knows it can lose money, and reads everything you write on a dashboard.

What counts: net profit after trading fees and API costs, compared with simply buying BTC on day one and holding it. Staying in cash or holding still is a real choice and is often the right one. A round trip costs about {cheap_trip}% in fees using resting orders and {full_trip}% using immediate ones, so a trade needs an expected move well beyond that.

What you can see: the briefing and the candle tool, both from Kraken. You have no news, order flow or on-chain data, so say so when that limits your confidence. Only state numbers that come from those sources.

The rules: spot only, no leverage, no shorting. Code enforces the limits in the briefing, and neither of you can change them. You only exist during these sessions. Between them, code checks prices every few seconds. It runs the stops and targets, and carries out any approved plans with no further review.

How to write: plain text with no markdown. Lead with your conclusion, give the numbers behind it, and keep it short. Talk to each other directly, and disagree when you disagree.

Each session costs real money, so look up only what would change your decision. test_rule tries a simple rule on years of real prices, after fees. A good result is a lead and not proof, because the best of many tries usually looks better than it is."""


def prompts(config: Config) -> tuple[str, str]:
    """The two decision prompts. Built once per run and never changed, so they stay cacheable."""
    shared = _shared_prompt(config)
    strategist = f"""{shared}

Between decisions, faster and less careful agents discuss the market every few minutes. Their notes are in the briefing. Treat them as leads to check, not as findings.

You are the Strategist. You own the plan.

Keep a short written strategy: what you're trying to do, in what conditions, and what would prove you wrong. Change it when the evidence warrants, not because of the latest candle.

Propose trades only when you can say why the expected move beats the fees. A buy gives quote_amount, stop_price and target_price; a sell gives sell_fraction. Code exits the whole position when either the stop or the target is hit, even while you're asleep, so set prices you'd really act on.

Every order chooses its execution. A resting order waits in the order book at the best price for {config.risk.resting_ttl_minutes} minutes and pays {config.maker_pct}%, but may not fill, especially if the price moves away. An immediate order fills now and pays {config.taker_pct}%. Pay the extra only when getting filled now is worth it.

You don't have to be awake to act on a price. Set trigger_price on a buy to make it a plan: code buys when the market reaches that price, and each plan can wait up to {config.risk.max_plan_hours} hours. Resting works for buying dips, but a trigger above the price has to be immediate. Prefer a plan to checking in often, because a plan costs nothing while it waits. Plans for an asset are cancelled if its stop-loss fires. Use cancel_plans to withdraw any that no longer make sense.

Choose the next check-in by when something you're waiting on could actually be decided. Checking sooner than that just spends money.

Always finish your turn by calling strategist_reply. Anything outside that call goes unread."""

    risk = f"""{shared}

You are the Risk Manager. You review what the Strategist proposes.

Your job is to catch bad decisions, not to block everything. You answer for avoidable losses and equally for fees burned on low-conviction churn. Where it matters, check the Strategist's claims against the candles yourself. Ask whether the reasoning would still hold if the last few candles hadn't happened, and whether the stop sits somewhere that makes sense.

Your verdict decides what happens:
- approve: the proposal executes exactly as written.
- revise: it goes back to the Strategist with your message. Say what would change your mind.
- reject: nothing executes this session.

A plan deserves particular care, because code carries it out later with no further review, whatever has changed by then. Ask whether the trigger, stop and target still make sense together if the market gets there in a very different way from what the Strategist expects.

If no trades are proposed, you're reviewing the decision to hold, any open positions and any waiting plans. Approve if that's sound, or use revise to ask for a position to be cut, a stop moved or a plan cancelled.

Always finish your turn by calling risk_reply. Anything outside that call goes unread."""
    return strategist, risk


def desk_prompts(config: Config) -> tuple[str, str]:
    """The two discussion prompts. Nothing said here executes, so these agents can think out loud."""
    shared = _shared_prompt(config)
    minutes = config.agents.discussion_minutes
    desk = f"""{shared}

This is the desk discussion. You and your colleague talk every {minutes} minutes, all day, on a fast model. Nothing you say here changes the portfolio. A separate decision session on a stronger model makes every change, and it costs about 40 times as much as this conversation.

Use your time to find what the portfolio should do next: read what has changed since your last exchange, look for setups, and develop new strategy ideas. Test ideas with test_rule when you can. Build on the conversation so far instead of repeating it. If nothing has changed, say so in a sentence or two and pick one idea to dig into. Use note to record anything worth remembering, such as a tested result or a lesson, in one or two sentences. Skip it if there's nothing new."""
    strategist = f"""{desk}

You are the Strategist. Open the conversation. Use suggest_change only when you'd really act on it now, and say exactly what you'd change and why.

Always finish your turn by calling desk_reply. Anything outside that call goes unread."""
    risk = f"""{desk}

You are the Risk Manager. Reply to the Strategist and challenge the weakest part of the reasoning. Ask whether a result came from one lucky period or from trying many variations. Use escalate only when a decision session should run right now and the case justifies paying for it, and give the reason in one sentence. Filling it in always starts that session, so if it depends on something that hasn't happened yet, leave it null and put the condition in your note. The desk will see it next time. Never escalate just because time has passed or to look busy.

Always finish your turn by calling desk_review. Anything outside that call goes unread."""
    return strategist, risk


def _rule_tool(assets: tuple[str, ...]) -> dict:
    return {
        "name": "test_rule",
        "description": (
            "Test a simple long-only rule on years of real 5-minute prices, after fees. Returns results by calendar year "
            "alongside holding the asset. A dip rule rests a buy entry_pct below the average_hours moving average, with "
            "a target_pct profit target and a stop_pct stop. A breakout rule buys immediately above the lookback_hours "
            "high, with a stop_pct stop and a trail_pct trailing stop. Set fields another rule uses to null."
        ),
        "strict": True,
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["asset", "rule", "stop_pct", "entry_pct", "target_pct", "average_hours", "only_in_uptrend",
                         "lookback_hours", "trail_pct"],
            "properties": {
                "asset": {"type": "string", "enum": list(assets)},
                "rule": {"type": "string", "enum": ["dip", "breakout"]},
                "stop_pct": {"type": "number", "description": "Stop distance below entry, 0.5 to 30."},
                "entry_pct": {"type": ["number", "null"], "description": "Dip: how far below the average to bid, 0.1 to 20."},
                "target_pct": {"type": ["number", "null"], "description": "Dip: profit target above entry, 0.3 to 50."},
                "average_hours": {"type": ["integer", "null"], "description": "Dip: moving average length in hours, 1 to 240."},
                "only_in_uptrend": {"type": ["boolean", "null"], "description": "Dip: only buy while above the 7-day average."},
                "lookback_hours": {"type": ["integer", "null"], "description": "Breakout: which recent high to beat, in hours, 1 to 240."},
                "trail_pct": {"type": ["number", "null"], "description": "Breakout: trailing stop distance, 0.3 to 30."},
            },
        },
    }


DESK_STRATEGIST_TOOL = {
    "name": "desk_reply",
    "description": "Finish your turn in the desk discussion.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["message", "note", "suggest_change"],
        "properties": {
            "message": {"type": "string", "description": "What you want to say to the Risk Manager."},
            "note": {"type": ["string", "null"], "description": "Anything worth keeping in the research notes, or null."},
            "suggest_change": {"type": ["string", "null"], "description": "A specific change you'd make now, or null."},
        },
    },
}

DESK_RISK_TOOL = {
    "name": "desk_review",
    "description": "Finish your turn in the desk discussion.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["message", "note", "escalate"],
        "properties": {
            "message": {"type": "string", "description": "What you want to say to the Strategist."},
            "note": {"type": ["string", "null"], "description": "Anything worth keeping in the research notes, or null."},
            "escalate": {"type": ["string", "null"], "description": "One sentence on why a decision session has to run right now. Anything else, including a condition that hasn't happened yet, means null. Filling this in always starts a paid session."},
        },
    },
}


def _candle_tool(assets: tuple[str, ...]) -> dict:
    return {
        "name": "get_candles",
        "description": "Fetch recent OHLCV candles for an asset from Kraken, oldest first. The newest candle is still forming.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["asset", "interval_minutes", "count"],
            "properties": {
                "asset": {"type": "string", "enum": list(assets)},
                "interval_minutes": {"type": "integer", "enum": list(CANDLE_INTERVALS)},
                "count": {"type": "integer", "description": f"How many candles, up to {MAX_CANDLES}."},
            },
        },
    }


def _strategist_tool(assets: tuple[str, ...]) -> dict:
    return {
        "name": "strategist_reply",
        "description": "Finish your turn: say what you think and propose any changes. The Risk Manager reviews them before anything happens.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["message", "orders", "stop_updates", "cancel_plans", "strategy_update", "next_check_in_minutes", "summary"],
            "properties": {
                "message": {"type": "string", "description": "What you want the Risk Manager and the owner to read."},
                "orders": {
                    "type": "array",
                    "description": "Trades to make now, and plans to act on later. Empty if none.",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["asset", "side", "quote_amount", "sell_fraction", "stop_price", "target_price",
                                     "execution", "trigger_price", "trigger_hours", "reason"],
                        "properties": {
                            "asset": {"type": "string", "enum": list(assets)},
                            "side": {"type": "string", "enum": ["buy", "sell"]},
                            "quote_amount": {"type": ["number", "null"], "description": "Buys: amount to spend. Null for sells."},
                            "sell_fraction": {"type": ["number", "null"], "description": "Sells: share of the position, above 0 and up to 1. Null for buys."},
                            "stop_price": {"type": ["number", "null"], "description": "Buys: required stop-loss price for the whole position. Null for sells."},
                            "target_price": {"type": ["number", "null"], "description": "Buys: required take-profit price for the whole position. Null for sells."},
                            "execution": {"type": "string", "enum": ["resting", "immediate"], "description": "resting waits in the order book at the lower fee and may not fill. immediate fills now at the higher fee."},
                            "trigger_price": {"type": ["number", "null"], "description": "Buys only: wait until the market reaches this price. Null to act now."},
                            "trigger_hours": {"type": ["integer", "null"], "description": "How long a triggered buy can wait. Null for the longest allowed. Null when there's no trigger."},
                            "reason": {"type": "string"},
                        },
                    },
                },
                "stop_updates": {
                    "type": "array",
                    "description": "Stop and target changes on positions already held. Empty if none.",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["asset", "stop_price", "target_price"],
                        "properties": {
                            "asset": {"type": "string", "enum": list(assets)},
                            "stop_price": {"type": "number"},
                            "target_price": {"type": ["number", "null"], "description": "New target, or null to keep the current one."},
                        },
                    },
                },
                "cancel_plans": {
                    "type": "array",
                    "description": "IDs of waiting plans to withdraw, from the briefing. Empty if none.",
                    "items": {"type": "integer"},
                },
                "strategy_update": {"type": ["string", "null"], "description": "The full new strategy text, or null to keep the current one."},
                "next_check_in_minutes": {"type": "integer", "description": "When to talk next if nothing else wakes you."},
                "summary": {"type": "string", "description": "One line for your journal, which you'll see in future sessions."},
            },
        },
    }


RISK_TOOL = {
    "name": "risk_reply",
    "description": "Finish your turn with your verdict on the Strategist's latest proposal.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["message", "verdict"],
        "properties": {
            "message": {"type": "string", "description": "What you want the Strategist and the owner to read."},
            "verdict": {"type": "string", "enum": ["approve", "revise", "reject"]},
        },
    },
}


@dataclass
class Agent:
    name: str
    system: str
    tools: list[dict]
    reply_tool: str
    messages: list = field(default_factory=list)
    pending_results: list = field(default_factory=list)
    model: str | None = None        # None means the decision model and settings.
    effort: str | None = None
    max_steps: int | None = None

    def tell(self, text: str) -> None:
        # Every tool call from the previous turn must be answered in the next user message.
        self.messages.append({"role": "user", "content": [*self.pending_results, {"type": "text", "text": text}]})
        self.pending_results = []


@dataclass
class Turn:
    reply: dict
    tools_used: list[str]
    reasoning: str
    cost_usd: float
    model: str


@dataclass
class Proposal:
    message: str
    orders: list[Order]
    stop_updates: list[tuple[str, Decimal, Decimal | None]]
    strategy: str | None
    next_check_in_minutes: int
    summary: str
    cancel_plans: list[int] = field(default_factory=list)


@dataclass
class SessionResult:
    outcome: str    # approved, held, rejected or no_agreement
    orders: list[Order]
    stop_updates: list[tuple[str, Decimal, Decimal | None]]
    strategy: str | None
    next_check_in_minutes: int
    summary: str
    cancel_plans: list[int] = field(default_factory=list)


@dataclass
class DiscussionResult:
    summary: str
    escalate: str | None


def _number(value) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("expected a number")
    number = D(value)
    if not number.is_finite():
        raise ValueError("expected a finite number")
    return number


def parse_proposal(data: dict) -> Proposal:
    """Checks a strategist_reply call. Raises ValueError with a message the agent can act on."""
    try:
        orders = [
            Order(
                asset=o["asset"],
                side=o["side"],
                quote_amount=_number(o["quote_amount"]),
                sell_fraction=_number(o["sell_fraction"]),
                stop_price=_number(o["stop_price"]),
                reason=str(o["reason"]),
                target_price=_number(o.get("target_price")),
                execution=str(o.get("execution", IMMEDIATE)),
                trigger_price=_number(o.get("trigger_price")),
                trigger_hours=None if o.get("trigger_hours") is None else int(o["trigger_hours"]),
            )
            for o in data["orders"]
        ]
        stops = [(s["asset"], _number(s["stop_price"]), _number(s.get("target_price"))) for s in data["stop_updates"]]
        strategy = data["strategy_update"]
        return Proposal(
            message=str(data["message"]).strip(),
            orders=orders,
            stop_updates=stops,
            strategy=strategy.strip() if strategy else None,
            next_check_in_minutes=int(data["next_check_in_minutes"]),
            summary=str(data["summary"]).strip(),
            cancel_plans=[int(plan_id) for plan_id in data.get("cancel_plans") or []],
        )
    except (KeyError, TypeError, InvalidOperation) as error:
        raise ValueError(f"strategist_reply is missing or has an invalid field: {error}") from error


def proposal_text(proposal: Proposal, checks: list[str | None], stop_checks: list[str | None]) -> str:
    """The Strategist's turn as the Risk Manager reads it, with the limit checks attached."""
    lines = [proposal.message]
    if proposal.orders:
        lines.append("\nProposed orders:")
        for order, check in zip(proposal.orders, checks):
            size = f"spend {order.quote_amount}, stop {order.stop_price}, target {order.target_price}" if order.side == "buy" else f"sell {order.sell_fraction} of the position"
            size += f", {order.execution}"
            if order.trigger_price is not None:
                size += f", only once the price reaches {order.trigger_price} (carried out later with no further review)"
            verdict = "passes the limits" if check is None else f"BLOCKED by the limits: {check}"
            lines.append(f"- {order.side} {order.asset}: {size}. Why: {order.reason}. {verdict}.")
    if proposal.stop_updates:
        lines.append("\nProposed stop changes:")
        for (asset, stop, target), check in zip(proposal.stop_updates, stop_checks):
            verdict = "passes the limits" if check is None else f"BLOCKED by the limits: {check}"
            new_target = f", target to {target}" if target is not None else ""
            lines.append(f"- {asset} stop to {stop}{new_target}. {verdict}.")
    if proposal.cancel_plans:
        lines.append(f"\nProposed plan cancellations: {', '.join(str(p) for p in proposal.cancel_plans)}.")
    if proposal.strategy:
        lines.append(f"\nProposed strategy:\n{proposal.strategy}")
    if not (proposal.orders or proposal.stop_updates or proposal.strategy or proposal.cancel_plans):
        lines.append("\nNo changes proposed.")
    return "\n".join(lines)


def usage_entries(response) -> list[tuple[str, int, int, int, int]]:
    """Token counts per model. When a fallback ran, usage.iterations holds the full picture."""
    usage = response.usage
    rows = []
    for entry in getattr(usage, "iterations", None) or [usage]:
        rows.append((
            getattr(entry, "model", None) or response.model,
            getattr(entry, "input_tokens", 0) or 0,
            getattr(entry, "output_tokens", 0) or 0,
            getattr(entry, "cache_creation_input_tokens", 0) or 0,
            getattr(entry, "cache_read_input_tokens", 0) or 0,
        ))
    return rows


def cost_usd(model: str, input_tokens: int, output_tokens: int, cache_write: int, cache_read: int) -> float:
    per_input, per_output, per_cache_read = PRICES.get(model, UNKNOWN_MODEL_PRICE)
    return (
        input_tokens * per_input
        + output_tokens * per_output
        + cache_write * per_input * 1.25
        + cache_read * per_cache_read
    ) / 1_000_000


class SessionRunner:
    def __init__(self, config: Config, db: DB, market: Market, client):
        self.config = config
        self.db = db
        self.market = market
        self.client = client
        self.budget = Budget(db, config)
        self.strategist_prompt, self.risk_prompt = prompts(config)
        self.desk_strategist_prompt, self.desk_risk_prompt = desk_prompts(config)
        candle_tool = _candle_tool(market.tracked)
        rule_tool = _rule_tool(market.tracked)
        self.strategist_tools = [candle_tool, rule_tool, _strategist_tool(config.assets)]
        self.risk_tools = [candle_tool, rule_tool, RISK_TOOL]
        self.desk_strategist_tools = [candle_tool, rule_tool, DESK_STRATEGIST_TOOL]
        self.desk_risk_tools = [candle_tool, rule_tool, DESK_RISK_TOOL]

    def _call(self, agent: Agent, session_id: int):
        model = agent.model or self.config.agents.model
        params = dict(
            model=model,
            max_tokens=MAX_TOKENS,
            system=agent.system,
            tools=agent.tools,
            messages=agent.messages,
            tool_choice={"type": "auto"},
            thinking={"type": "adaptive", "display": "summarized"},
            output_config={"effort": agent.effort or self.config.agents.effort},
            cache_control={"type": "ephemeral"},
        )
        if not model.startswith("claude-haiku"):  # Haiku has no server-side fallback.
            params["fallbacks"] = "default"
            params["betas"] = [FALLBACK_BETA]
        response = self.client.beta.messages.create(**params)
        self._record_usage(response, session_id)
        return response

    def _record_usage(self, response, session_id: int) -> None:
        for model, input_tokens, output_tokens, cache_write, cache_read in usage_entries(response):
            self.db.execute(
                "INSERT INTO api_usage(ts, session_id, model, input_tokens, output_tokens, "
                "cache_write_tokens, cache_read_tokens, cost_usd) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                (time.time(), session_id, model, input_tokens, output_tokens, cache_write, cache_read,
                 cost_usd(model, input_tokens, output_tokens, cache_write, cache_read)),
            )

    def _run_candle_tool(self, call) -> tuple[dict, str]:
        try:
            asset = call.input["asset"]
            interval = int(call.input["interval_minutes"])
            count = max(1, min(int(call.input["count"]), MAX_CANDLES))
            candles = self.market.candles(asset, interval, count)
        except Exception as error:  # A failed lookup goes back to the agent, not up the stack.
            return {"type": "tool_result", "tool_use_id": call.id, "content": f"Lookup failed: {error}", "is_error": True}, "candle lookup failed"
        rows = ["time_utc,open,high,low,close,volume"]
        for c in candles:
            when = datetime.fromtimestamp(c.ts, timezone.utc).strftime("%Y-%m-%d %H:%M")
            rows.append(f"{when},{c.open},{c.high},{c.low},{c.close},{c.volume:.2f}")
        label = f"{asset} {interval}m candles ({len(candles)})"
        return {"type": "tool_result", "tool_use_id": call.id, "content": "\n".join(rows)}, label

    def _run_rule_tool(self, call) -> tuple[dict, str]:
        try:
            text, label = test_rule(self.config, self.db, call.input)
        except ValueError as error:
            return {"type": "tool_result", "tool_use_id": call.id, "content": str(error), "is_error": True}, "rule test rejected"
        except Exception as error:  # A failed test goes back to the agent, not up the stack.
            return {"type": "tool_result", "tool_use_id": call.id, "content": f"Test failed: {error}", "is_error": True}, "rule test failed"
        return {"type": "tool_result", "tool_use_id": call.id, "content": text}, label

    def _turn(self, agent: Agent, session_id: int, validate: Callable[[dict], None]) -> Turn:
        """Runs one agent until it finishes with its reply tool."""
        tools_used: list[str] = []
        reasoning: list[str] = []
        spent_before = self.budget.session(session_id)
        steps = agent.max_steps or self.config.agents.max_steps_per_turn
        for _ in range(steps):
            blocker = self.budget.blocker(session_id)
            if blocker:
                raise BudgetExceeded(blocker)
            response = self._call(agent, session_id)
            if response.stop_reason == "refusal":
                details = getattr(response, "stop_details", None)
                raise AgentFailed(f"The {agent.name} request was declined ({getattr(details, 'category', 'no category')})")
            if response.stop_reason == "max_tokens":
                raise AgentFailed(f"The {agent.name} ran out of output room before finishing")

            agent.messages.append({"role": "assistant", "content": response.content})
            reasoning += [b.thinking for b in response.content if b.type == "thinking" and b.thinking]
            calls = [b for b in response.content if b.type == "tool_use"]
            if not calls:
                agent.tell(f"Please finish your turn by calling {agent.reply_tool}. Text outside that call isn't delivered.")
                continue

            results, reply = [], None
            for call in calls:
                if call.name == agent.reply_tool:
                    try:
                        validate(call.input)
                    except ValueError as error:
                        results.append({"type": "tool_result", "tool_use_id": call.id, "content": str(error), "is_error": True})
                    else:
                        reply = call
                elif call.name == "get_candles":
                    result, label = self._run_candle_tool(call)
                    results.append(result)
                    tools_used.append(label)
                elif call.name == "test_rule":
                    result, label = self._run_rule_tool(call)
                    results.append(result)
                    tools_used.append(label)
                else:
                    results.append({"type": "tool_result", "tool_use_id": call.id, "content": f"Unknown tool {call.name}", "is_error": True})
            if reply is not None:
                results.append({"type": "tool_result", "tool_use_id": reply.id, "content": "Delivered."})
                agent.pending_results = results
                return Turn(reply.input, tools_used, "\n\n".join(reasoning),
                            self.budget.session(session_id) - spent_before, response.model)
            agent.messages.append({"role": "user", "content": results})
        raise AgentFailed(f"The {agent.name} used all {steps} steps without finishing")

    def _log(self, session_id: int, speaker: str, text: str, turn: Turn | None = None,
             orders: dict | None = None, verdict: str | None = None) -> None:
        self.db.execute(
            "INSERT INTO messages(session_id, ts, speaker, text, reasoning, orders, verdict, tools, model, cost_usd) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (session_id, time.time(), speaker, text,
             turn.reasoning if turn else None,
             json.dumps(orders) if orders else None,
             verdict,
             json.dumps(turn.tools_used) if turn and turn.tools_used else None,
             turn.model if turn else None,
             turn.cost_usd if turn else 0),
        )

    def log_system(self, session_id: int, text: str) -> None:
        self._log(session_id, "system", text)

    def run(self, session_id: int, brief: str, must_review: bool,
            check: Callable[[Proposal], tuple[list[str | None], list[str | None]]]) -> SessionResult:
        """Runs the conversation and returns what was agreed. Executes nothing itself."""
        strategist = Agent("Strategist", self.strategist_prompt, self.strategist_tools, "strategist_reply")
        risk = Agent("Risk Manager", self.risk_prompt, self.risk_tools, "risk_reply")
        rounds = self.config.agents.max_rounds
        strategist.tell(f"{brief}\n\nIt's your move, Strategist. You have up to {rounds} rounds with the Risk Manager this session.")

        proposal = None
        for round_number in range(1, rounds + 1):
            turn = self._turn(strategist, session_id, parse_proposal)
            proposal = parse_proposal(turn.reply)
            checks, stop_checks = check(proposal)
            self._log(session_id, "strategist", proposal.message, turn, _orders_for_display(proposal, checks, stop_checks))

            changes = proposal.orders or proposal.stop_updates or proposal.strategy or proposal.cancel_plans
            if not changes and not must_review:
                self.log_system(session_id, "No changes proposed and nothing is open, so the Risk Manager wasn't consulted.")
                return SessionResult("held", [], [], None, proposal.next_check_in_minutes, proposal.summary)

            text = proposal_text(proposal, checks, stop_checks)
            if round_number == 1:
                risk.tell(f"{brief}\n\nThe Strategist says:\n\n{text}")
            else:
                risk.tell(f"The Strategist replies:\n\n{text}")

            turn = self._turn(risk, session_id, _validate_risk)
            verdict, message = turn.reply["verdict"], str(turn.reply["message"]).strip()
            self._log(session_id, "risk", message, turn, verdict=verdict)

            if verdict == "approve":
                outcome = "approved" if changes else "held"
                return SessionResult(outcome, proposal.orders, proposal.stop_updates, proposal.strategy,
                                     proposal.next_check_in_minutes, proposal.summary, proposal.cancel_plans)
            if verdict == "reject":
                return SessionResult("rejected", [], [], None, proposal.next_check_in_minutes,
                                     f"{proposal.summary} (Rejected: {message})")
            if round_number < rounds:
                strategist.tell(f"The Risk Manager asks for changes (round {round_number + 1} of {rounds} next):\n\n{message}")

        return SessionResult("no_agreement", [], [], None, proposal.next_check_in_minutes,
                             f"{proposal.summary} (No agreement after {rounds} rounds.)")


    def _desk_agent(self, name: str, system: str, tools: list[dict], reply_tool: str) -> Agent:
        agents = self.config.agents
        return Agent(name, system, tools, reply_tool, model=agents.discussion_model,
                     effort=agents.discussion_effort, max_steps=DESK_MAX_STEPS)

    def _add_note(self, session_id: int, speaker: str, note: str | None) -> None:
        if note and note.strip():
            self.db.execute("INSERT INTO research_notes(ts, session_id, speaker, text) VALUES(?, ?, ?, ?)",
                            (time.time(), session_id, speaker, note.strip()))

    def run_discussion(self, session_id: int, brief: str) -> DiscussionResult:
        """One exchange on the desk. It can record notes and ask for a decision session, but it can't change anything."""
        strategist = self._desk_agent("Strategist", self.desk_strategist_prompt, self.desk_strategist_tools, "desk_reply")
        risk = self._desk_agent("Risk Manager", self.desk_risk_prompt, self.desk_risk_tools, "desk_review")

        strategist.tell(f"{brief}\n\nIt's your turn to open, Strategist.")
        turn = self._turn(strategist, session_id, _validate_desk)
        message = str(turn.reply["message"]).strip()
        suggestion = turn.reply.get("suggest_change")
        said = f"{message}\n\nSuggested change: {suggestion.strip()}" if suggestion and suggestion.strip() else message
        self._log(session_id, "strategist", said, turn)
        self._add_note(session_id, "strategist", turn.reply.get("note"))

        risk.tell(f"{brief}\n\nThe Strategist says:\n\n{said}")
        turn = self._turn(risk, session_id, _validate_desk)
        reply = str(turn.reply["message"]).strip()
        escalate = turn.reply.get("escalate")
        escalate = escalate.strip() if escalate and escalate.strip() else None
        self._log(session_id, "risk", reply, turn, verdict="escalate" if escalate else None)
        self._add_note(session_id, "risk", turn.reply.get("note"))
        return DiscussionResult(escalate or (message if len(message) <= 200 else f"{message[:200].rstrip()}…"), escalate)


def _validate_desk(data: dict) -> None:
    if not str(data.get("message", "")).strip():
        raise ValueError("message can't be empty")


def _validate_risk(data: dict) -> None:
    if data.get("verdict") not in ("approve", "revise", "reject"):
        raise ValueError("verdict must be approve, revise or reject")
    if not str(data.get("message", "")).strip():
        raise ValueError("message can't be empty")


def _orders_for_display(proposal: Proposal, checks, stop_checks) -> dict | None:
    if not (proposal.orders or proposal.stop_updates or proposal.strategy or proposal.cancel_plans):
        return None
    number = lambda value: None if value is None else float(value)
    return {
        "orders": [
            {
                "asset": o.asset, "side": o.side, "quote_amount": number(o.quote_amount),
                "sell_fraction": number(o.sell_fraction), "stop_price": number(o.stop_price),
                "target_price": number(o.target_price), "execution": o.execution,
                "trigger_price": number(o.trigger_price),
                "reason": o.reason, "blocked": check,
            }
            for o, check in zip(proposal.orders, checks)
        ],
        "stops": [
            {"asset": asset, "stop_price": number(stop), "target_price": number(target), "blocked": check}
            for (asset, stop, target), check in zip(proposal.stop_updates, stop_checks)
        ],
        "cancel_plans": proposal.cancel_plans,
        "strategy_changed": bool(proposal.strategy),
    }
