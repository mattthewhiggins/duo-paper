"""The dashboard: a small local web server over the bot's database."""
from __future__ import annotations

import base64
import csv
import hmac
import io
import json
import os
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .config import D, Config
from .engine import Engine, day_start

STATIC = Path(__file__).parent / "static"
LOOPBACK = {"127.0.0.1", "localhost", "::1"}
RANGES = {"24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400, "all": None}
MAX_CHART_POINTS = 400


def _num(value) -> float | None:
    return None if value is None else float(value)


def _loop_stale(runner, heartbeat, now: float) -> bool:
    """The last loop is too old to speak for now: two loops and a minute, as the header uses."""
    return heartbeat is None or now - heartbeat["ts"] > runner.s.loop_minutes * 60 * 2 + 60


class Dashboard:
    def __init__(self, engine: Engine, config: Config):
        self.engine = engine
        self.config = config
        self.db = engine.db
        self._compare_cache: tuple | None = None

    def _systematic_status(self, status: dict) -> dict:
        """In systematic mode the header speaks for the runner: its halt, and whether its loop is alive (finding 63)."""
        runner = self.engine.systematic
        now = time.time()
        heartbeat = self.db.one("SELECT ts, status, halts FROM sys_heartbeats ORDER BY id DESC LIMIT 1")
        halted = self.db.get("sys_kill") == "1"
        status.update(halted=halted, halt_reason=self.db.get("sys_kill_reason"), agents_ready=False, running=False,
                      next_check_in=None, flattening=False)
        if halted:
            code, label = "halted", f"Systematic trading is halted: {status['halt_reason']}. Press Resume to restart it."
        elif _loop_stale(runner, heartbeat, now):
            code, label = "warn", "The systematic loop hasn't run lately. Nothing is being checked or traded."
        elif heartbeat["status"] != "ok":
            code, label = "warn", "Systematic runner: " + "; ".join(json.loads(heartbeat["halts"]))
        else:
            code, label = "active", f"Systematic runner on {runner.strategy.id}. Next decision {datetime.fromtimestamp(runner.next_decision_at(now), timezone.utc).strftime('%H:%M UTC %d %b')}."
        status.update(code=code, label=label)
        return status

    def compare(self) -> dict:
        """The paper-against-backtest report, recomputed only when a decision or fill is added."""
        from . import compare
        key = (self.db.one("SELECT COUNT(*) AS n FROM sys_decisions")["n"],
               self.db.one("SELECT COUNT(*) AS n FROM fills")["n"])
        if self._compare_cache is None or self._compare_cache[0] != key:
            try:
                report = compare.compare(self.db)
            except Exception as error:
                report = {"error": f"{type(error).__name__}: {error}", "days": [], "orders": [], "summary": {}}
            self._compare_cache = (key, report)
        return self._compare_cache[1]

    def systematic(self) -> dict | None:
        runner = self.engine.systematic
        if runner is None:
            return None
        snapshot = runner.snapshot()
        report = self.compare()
        snapshot["compare"] = {"summary": report.get("summary", {}), "days": report.get("days", [])[-60:],
                               "orders": report.get("orders", [])[-30:], "notes": report.get("notes", []),
                               "error": report.get("error")}
        return snapshot

    def _status(self) -> dict:
        engine, db = self.engine, self.db
        blocker = engine.budget.blocker()
        next_check_in = db.get("next_check_in")
        status = {
            "next_check_in": _num(next_check_in) if engine.runner else None,
            "next_discussion": engine.next_discussion(),
            "halted": db.get("halted") == "1",
            "halt_reason": db.get("halt_reason"),
            "running": engine.session_running(),
            "agents_ready": engine.runner is not None,
            "agents_error": db.get("agents_error"),
            "flattening": db.get("flatten_requested") == "1",
        }
        if engine.systematic is not None:
            return self._systematic_status(status)
        if status["halted"]:
            code, label = "halted", f"Agent trading is stopped: {status['halt_reason']}. Stop-losses stay armed."
        elif db.get("market_down_since"):
            code, label = "warn", "Can't get prices from Kraken. Stop-losses can't fire until this clears."
        elif status["running"]:
            code, label = "active", "The agents are talking."
        elif engine.runner is None:
            code, label = "setup", "The agents are off. Add ANTHROPIC_API_KEY to .env and restart."
        elif blocker:
            code, label = "warn", f"The agents are asleep: {blocker}. Stop-losses keep running."
        else:
            code, label = "idle", "The agents are asleep."
        status.update(code=code, label=label)
        return status

    def _balances(self, metrics: dict) -> list[dict]:
        portfolio, marks, equity = metrics["portfolio"], metrics["marks"], metrics["equity"]
        exits = {r["asset"]: r for r in self.db.all("SELECT asset, stop_price, target_price FROM stops")}
        rows = [{
            "asset": self.config.quote, "qty": None, "value": _num(metrics["cash"]), "cash": True,
            "share_pct": _num(metrics["cash"] / equity * 100) if equity else 0,
        }]
        held = [a for a, p in portfolio.positions.items() if p.qty > 0 and a not in self.config.assets]
        for asset in (*self.config.assets, *sorted(held)):  # A TOP-n strategy can hold what config.assets doesn't list.
            position = portfolio.positions.get(asset)
            if position and position.qty > 0:
                value = position.qty * marks[asset]
                rows.append({
                    "asset": asset, "qty": _num(position.qty), "price": _num(marks[asset]), "value": _num(value),
                    "share_pct": _num(value / equity * 100) if equity else 0,
                    "avg_cost": _num(position.avg_cost), "unrealized": _num(value - position.cost),
                    "stop": _num(exits[asset]["stop_price"]) if asset in exits else None,
                    "target": _num(exits[asset]["target_price"]) if asset in exits else None,
                })
        return rows

    def _limits(self, metrics: dict) -> list[dict]:
        engine, config, risk = self.engine, self.config, self.config.risk
        now = time.time()
        equity = metrics["equity"]
        day_open = engine.day_open_equity(now) or equity
        peak = D(self.db.get("peak_equity", "0")) or equity
        largest = max((r["share_pct"] for r in self._balances(metrics) if not r.get("cash")), default=0)
        loss_today = max((day_open - equity) / day_open * 100, Decimal(0)) if day_open else Decimal(0)
        drawdown = max((peak - equity) / peak * 100, Decimal(0)) if peak else Decimal(0)
        agents = config.agents
        return [
            {"label": "Buys today", "used": engine.buys_today(now), "max": risk.max_buys_per_day, "unit": "count"},
            {"label": "Fees, last 30 days", "used": float(engine.fees_30d(now) / equity * 100) if equity else 0,
             "max": float(risk.max_fees_30d_pct), "unit": "pct"},
            {"label": "Largest holding", "used": largest, "max": float(risk.max_asset_pct), "unit": "pct"},
            {"label": "Loss today", "used": float(loss_today), "max": float(risk.daily_loss_pause_pct), "unit": "pct"},
            {"label": "Drawdown from peak", "used": float(drawdown), "max": float(risk.drawdown_halt_pct), "unit": "pct"},
            {"label": "Opus decisions today", "used": engine.decisions_today(now), "max": agents.max_decisions_per_day, "unit": "count"},
            {"label": "API spend today", "used": engine.budget.today(), "max": agents.daily_budget_usd, "unit": "usd"},
            {"label": "API spend this month", "used": engine.budget.this_month(), "max": agents.monthly_budget_usd, "unit": "usd"},
        ]

    def _sys_limits(self, snapshot: dict | None, now: float | None = None) -> list[dict]:
        """The systematic runner's own limits ([systematic], checked in duo/sys_risk.py), at the last loop's readings.

        The agents' [risk] and [agents] limits don't apply in this mode, so they aren't shown. A reading the runner
        couldn't take is None, shown as unknown: the runner fails closed on it. So is every reading when the last loop
        is too old to speak for now, because nothing is being checked then.
        """
        s = self.config.systematic
        now = time.time() if now is None else now
        if snapshot and self.engine.systematic is not None and _loop_stale(self.engine.systematic, snapshot.get("heartbeat"), now):
            snapshot = None
        snapshot = snapshot or {}
        controls = {c["control"]: c for c in snapshot.get("controls") or []}
        heartbeat = snapshot.get("heartbeat") or {}
        halts = snapshot.get("halts") or {}
        limits = snapshot.get("asset_limits") or {}

        def reading(name):
            control = controls.get(name)
            return None if control is None else _num(control.get("value"))

        # Per asset: the holding closest to its own limit, which is the strategy's largest sleeve + the margin.
        asset, used, cap = None, 0.0, max(limits.values(), default=float(s.max_asset_pct) / 100) * 100
        worst = -1.0
        for row in snapshot.get("holdings") or []:
            if not row.get("qty"):
                continue
            weight, limit = row.get("weight"), limits.get(row["asset"])
            if weight is None or limit is None:
                asset, used, cap = row["asset"], None, (limit * 100 if limit is not None else None)
                break
            ratio = weight / limit if limit > 0 else float("inf")
            if ratio > worst:
                worst, asset, used, cap = ratio, row["asset"], weight * 100, limit * 100
        if not snapshot:
            used = None
        api_errors = halts.get("api_errors")
        # A quote fetched during the loop is stamped a moment after the loop's clock, so its age can read just below 0.
        quote_age = _num(heartbeat.get("quote_age_s")) if heartbeat else None
        quote_age = None if quote_age is None else max(quote_age, 0.0)
        return [
            {"label": f"Per asset ({asset})" if asset else "Per asset", "used": used, "max": cap, "unit": "pct",
             "note": f"The strategy's largest sleeve + {s.asset_margin_pct} points, never above {s.max_asset_pct}%. "
                     "A buy past it is cut or refused."},
            {"label": "Gross exposure", "used": reading("gross_exposure"), "max": float(s.max_gross_pct), "unit": "pct",
             "note": "No shorts and no leverage."},
            {"label": "Per order", "used": None, "max": float(s.order_margin_pct), "unit": "rule",
             "text": f"Target change + {s.order_margin_pct}% of equity",
             "note": "A sell: no more than its order set out to sell."},
            {"label": "Loss today", "used": reading("daily_loss"), "max": float(s.daily_loss_pct), "unit": "pct",
             "note": "At or past this below the UTC day's start: no buys until 00:00 UTC. Sells still run."},
            {"label": "Kill switch: drawdown from peak", "used": reading("kill_switch"),
             "max": float(s.kill_drawdown_pct), "unit": "pct",
             "note": "At or past this: cancels everything, sells at market (capped) and halts until you resume."},
            {"label": "Daily bar missing after 00:00 UTC", "used": reading("stale_data"),
             "max": float(s.signal_stale_minutes), "unit": "minutes",
             "note": "At or past this, no new orders until it arrives."},
            {"label": "Kraken quote age", "used": quote_age,
             "max": float(s.max_quote_age_seconds), "unit": "seconds",
             "note": f"Or unchanged for {s.max_frozen_quote_seconds} seconds: no new orders on that asset."},
            {"label": "API errors in a row", "used": None if api_errors is None or not snapshot else api_errors,
             "max": s.max_api_errors, "unit": "count", "note": "At this many, no new orders until a call succeeds."},
        ]

    def _sys_limits_note(self, snapshot: dict | None, now: float | None = None) -> str:
        """Says which loop the readings come from, and says so plainly when it's too old to count."""
        now = time.time() if now is None else now
        heartbeat = (snapshot or {}).get("heartbeat")
        if heartbeat is None:
            return "Systematic runner, enforced by code. No loop has run yet, so the readings are unknown."
        when = datetime.fromtimestamp(heartbeat["ts"], timezone.utc).strftime("%H:%M UTC %d %b")
        if _loop_stale(self.engine.systematic, heartbeat, now):
            return (f"Systematic runner, enforced by code. The last loop was at {when}, too long ago to count, "
                    "so the readings are unknown.")
        return f"Systematic runner, enforced by code. Last loop {when}."

    def _agents(self) -> dict:
        """What the agents may do, so the page never suggests they decide anything in systematic mode."""
        if not self.config.systematic_on:
            return {"enabled": self.engine.runner is not None, "can_trade": True, "talking": self.engine.runner is not None,
                    "label": ""}
        if not self.config.systematic.agents_enabled:
            return {"enabled": False, "can_trade": False, "talking": False,
                    "label": "The agents are off. In systematic mode they can't trade: only the systematic runner's "
                             "code places orders."}
        talking = self.engine.runner is not None
        return {"enabled": True, "can_trade": False, "talking": talking,
                "label": "The agents can talk, but they can't trade in systematic mode: nothing they say places or "
                         "changes an order. Only the systematic runner's code trades."
                         + ("" if talking else " They aren't running: there's no ANTHROPIC_API_KEY in .env.")}

    def _day_stats(self, metrics: dict) -> dict:
        """Daily results after fees and API costs. A day counts once it has finished, in UTC."""
        rows = self.db.all(
            "SELECT CAST(ts / 86400 AS INTEGER) AS day, equity, api_cost, MAX(ts) FROM snapshots GROUP BY day ORDER BY day"
        )
        today = int(time.time() // 86400)
        previous = float(metrics["starting"])
        profitable = losing = 0
        for row in rows:
            if row["day"] >= today:
                break
            value = row["equity"] - row["api_cost"]
            profitable += value > previous
            losing += value < previous
            previous = value
        return {
            "today_pnl": float(metrics["equity"] - metrics["api_cost"]) - previous,
            "profitable_days": profitable,
            "losing_days": losing,
            "finished_days": sum(row["day"] < today for row in rows),
        }

    def _orders(self) -> dict:
        now = time.time()
        marks = self.engine.market.quotes
        plans = []
        for plan in self.engine.waiting_plans():
            quote = marks.get(plan["asset"])
            trigger = float(plan["trigger_price"])
            plans.append({
                "id": plan["id"], "asset": plan["asset"], "amount": float(plan["quote_amount"]), "trigger": trigger,
                "direction": plan["direction"], "execution": plan["execution"], "status": plan["status"],
                "stop": float(plan["stop_price"]), "target": float(plan["target_price"]), "note": plan["note"],
                "seconds_left": plan["expires_at"] - now, "reason": plan["reason"],
                "away_pct": abs(float(quote.mid) - trigger) / float(quote.mid) * 100 if quote else None,
            })
        resting = [{
            "id": order["id"], "asset": order["asset"], "side": order["side"], "qty": float(order["qty"]),
            "limit": float(order["limit_price"]), "source": order["source"], "seconds_left": order["expires_at"] - now,
            "filled_pct": float(order["booked_qty"]) / float(order["qty"]) * 100,
        } for order in self.engine.open_resting()]
        stats = self.engine.resting_stats()
        return {"plans": plans, "resting": resting,
                "stats": {"closed": stats["closed"], "filled": stats["filled"], "fees_saved": float(stats["fees_saved"])}}

    def state(self) -> dict:
        metrics = self.engine.metrics()
        strategy = self.db.one("SELECT ts, text FROM strategy ORDER BY id DESC LIMIT 1")
        systematic_mode = self.config.systematic_on
        systematic = self.systematic()
        payload = {
            "mode": self.config.mode,
            "quote": self.config.quote,
            "status": self._status(),
            "strategy": dict(strategy) if strategy else None,
            "now": time.time(),
            "metrics": None,
            "orders": self._orders(),
            "notes": [dict(r) for r in self.db.all(
                "SELECT id, ts, speaker, text FROM research_notes ORDER BY id DESC LIMIT 15")],
            "rule_tests": int(self.db.get("rule_tests", "0")),
            "systematic": systematic,
            "systematic_mode": systematic_mode,
            "agents": self._agents(),
            "limits_kind": "systematic" if systematic_mode else "agents",
            "limits_note": self._sys_limits_note(systematic) if systematic_mode else "Enforced by code",
        }
        if metrics:
            keys = ("equity", "starting", "cash", "realized", "unrealized", "fees", "api_cost",
                    "net_pnl", "net_pnl_pct", "benchmark", "benchmark_pnl", "vs_benchmark")
            payload["metrics"] = {key: _num(metrics[key]) for key in keys}
            payload["metrics"].update(self._day_stats(metrics))
            payload["balances"] = self._balances(metrics)
            if not systematic_mode:
                payload["limits"] = self._limits(metrics)
            payload["prices"] = [
                {"asset": asset, "mid": _num(metrics["marks"][asset])}
                for asset in self.engine.market.tracked if asset in metrics["marks"]
            ]
        if systematic_mode:  # The runner's limits don't need prices, and an unknown reading must still show.
            payload["limits"] = self._sys_limits(systematic)
        return payload

    def equity(self, range_name: str) -> dict:
        seconds = RANGES.get(range_name, RANGES["7d"])
        since = 0 if seconds is None else time.time() - seconds
        rows = self.db.all("SELECT ts, equity, benchmark FROM snapshots WHERE ts >= ? ORDER BY ts", (since,))
        step = max(1, len(rows) // MAX_CHART_POINTS)
        kept = rows[::step]
        if rows and kept[-1] is not rows[-1]:
            kept.append(rows[-1])
        points = [{"ts": r["ts"], "equity": r["equity"], "benchmark": r["benchmark"]} for r in kept]
        metrics = self.engine.metrics()
        if metrics and points:
            points.append({"ts": time.time(), "equity": float(metrics["equity"]), "benchmark": float(metrics["benchmark"])})
        return {"points": points, "starting": float(self.config.starting_capital)}

    def trades(self, limit: int = 200) -> list[dict]:
        rows = self.db.all("SELECT * FROM fills ORDER BY id DESC LIMIT ?", (limit,))
        return [{
            "id": r["id"], "ts": r["ts"], "session_id": r["session_id"], "source": r["source"], "asset": r["asset"],
            "side": r["side"], "qty": _num(r["qty"]), "price": _num(r["price"]), "gross": _num(r["gross"]),
            "fee": _num(r["fee"]), "realized": _num(r["realized"]), "reason": r["reason"], "liquidity": r["liquidity"],
        } for r in rows]

    def trades_csv(self) -> str:
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["time_utc", "asset", "side", "quantity", "price", f"value_{self.config.quote}",
                         f"fee_{self.config.quote}", f"realized_{self.config.quote}", "order_type", "source", "session", "order_id", "reason"])
        for r in self.db.all("SELECT * FROM fills ORDER BY id"):
            when = datetime.fromtimestamp(r["ts"], timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            reason = r["reason"] or ""
            if reason[:1] in ("=", "+", "-", "@"):  # Spreadsheets would run these as formulas.
                reason = f"'{reason}"
            order_type = {"maker": "resting", "taker": "immediate"}.get(r["liquidity"], "")
            writer.writerow([when, r["asset"], r["side"], r["qty"], r["price"], r["gross"], r["fee"], r["realized"],
                             order_type, r["source"], r["session_id"], r["venue_id"], reason])
        return output.getvalue()

    def sessions(self, kind: str = "decision", limit: int = 30) -> list[dict]:
        result = []
        for s in self.db.all("SELECT * FROM sessions WHERE kind = ? ORDER BY id DESC LIMIT ?", (kind, limit)):
            messages = []
            for m in self.db.all("SELECT * FROM messages WHERE session_id = ? ORDER BY id", (s["id"],)):
                messages.append({
                    "id": m["id"], "ts": m["ts"], "speaker": m["speaker"], "text": m["text"], "reasoning": m["reasoning"],
                    "proposal": json.loads(m["orders"]) if m["orders"] else None, "verdict": m["verdict"],
                    "tools": json.loads(m["tools"]) if m["tools"] else [], "model": m["model"], "cost_usd": m["cost_usd"],
                })
            result.append({**dict(s), "cost_usd": self.engine.budget.session(s["id"]), "messages": messages})
        return result

    def events(self, limit: int = 40) -> list[dict]:
        return [dict(r) for r in self.db.all("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))]


def make_handler(dashboard: Dashboard, token: str | None, loopback_only: bool):
    engine = dashboard.engine

    class Handler(BaseHTTPRequestHandler):
        server_version = "Duo"
        sys_version = ""

        def log_message(self, *args):
            pass

        def _send(self, status: int, body: bytes, content_type: str, extra: dict | None = None):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'",
            )
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, payload, status: int = 200):
            self._send(status, json.dumps(payload).encode(), "application/json")

        def _allowed(self) -> bool:
            # The Host check stops other websites reaching this server through DNS tricks.
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
            if loopback_only and host not in LOOPBACK:
                self._send(403, b"Forbidden", "text/plain")
                return False
            if token:
                sent = self.headers.get("Authorization", "")
                password = ""
                if sent.startswith("Basic "):
                    try:
                        password = base64.b64decode(sent[6:]).decode().split(":", 1)[1]
                    except (ValueError, IndexError):
                        password = ""
                if not hmac.compare_digest(password, token):
                    self._send(401, b"Password required", "text/plain", {"WWW-Authenticate": 'Basic realm="Duo"'})
                    return False
            return True

        def do_GET(self):
            if not self._allowed():
                return
            url = urlparse(self.path)
            query = parse_qs(url.query)
            try:
                if url.path == "/":
                    self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
                elif url.path == "/api/state":
                    self._json(dashboard.state())
                elif url.path == "/api/equity":
                    self._json(dashboard.equity(query.get("range", ["7d"])[0]))
                elif url.path == "/api/trades":
                    self._json(dashboard.trades())
                elif url.path == "/api/sessions":
                    self._json(dashboard.sessions(query.get("kind", ["decision"])[0]))
                elif url.path == "/api/events":
                    self._json(dashboard.events())
                elif url.path == "/api/compare":
                    self._json(dashboard.compare())
                elif url.path == "/trades.csv":
                    self._send(200, dashboard.trades_csv().encode(), "text/csv; charset=utf-8",
                               {"Content-Disposition": f'attachment; filename="duo-{dashboard.config.mode}-trades.csv"'})
                else:
                    self._send(404, b"Not found", "text/plain")
            except Exception as error:
                dashboard.db.event("warn", "dashboard", f"{url.path} failed: {error}")
                self._json({"error": str(error)}, 500)

        def do_POST(self):
            if not self._allowed():
                return
            # Other websites can't set a custom header without a CORS approval this server never gives.
            if self.headers.get("X-Duo-Action") != "1":
                self._send(403, b"Forbidden", "text/plain")
                return
            actions = {
                "/api/halt": lambda: engine.halt("stopped from the dashboard"),
                "/api/resume": engine.resume,
                "/api/wake": engine.request_wake,
                "/api/flatten": engine.request_flatten,
            }
            path = urlparse(self.path).path
            parts = path.strip("/").split("/")
            if len(parts) == 4 and parts[:2] == ["api", "plans"] and parts[3] == "cancel" and parts[2].isdigit():
                self._json({"ok": engine.cancel_plan(int(parts[2]))})
                return
            if path == "/api/wake" and dashboard.config.systematic_on:
                # Decision sessions exist to trade, and the agents can't trade in this mode, so there's nothing to wake.
                self._json({"ok": False, "error": "Systematic mode is on: the agents can't trade, so there's no "
                                                  "decision session to wake."}, 409)
                return
            action = actions.get(path)
            if action is None:
                self._send(404, b"Not found", "text/plain")
                return
            action()
            self._json({"ok": True})

    return Handler


def serve(dashboard: Dashboard) -> ThreadingHTTPServer:
    config = dashboard.config
    token = os.environ.get("DUO_DASHBOARD_TOKEN") or None
    loopback_only = config.host in LOOPBACK
    if not loopback_only and not token:
        raise SystemExit(f"The dashboard is set to listen on {config.host}, which needs DUO_DASHBOARD_TOKEN set in .env.")
    server = ThreadingHTTPServer((config.host, config.port), make_handler(dashboard, token, loopback_only))
    threading.Thread(target=server.serve_forever, daemon=True, name="dashboard").start()
    return server
