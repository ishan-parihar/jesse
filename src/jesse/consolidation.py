from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

JOURNAL_PATH = Path.home() / ".mt5-mcp" / "trading_journal.db"

CONSOLIDATION_MIN_TRADES = 30
CONSOLIDATION_MAX_TRADES = 100
CONSOLIDATION_MAX_AGE_DAYS = 30


def _get_conn():
    if not JOURNAL_PATH.exists():
        return None
    conn = sqlite3.connect(str(JOURNAL_PATH))
    conn.row_factory = sqlite3.Row
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='trade_decisions'"
    ).fetchall()
    if not tables:
        conn.close()
        return None
    return conn


def get_recent_trades() -> list[dict[str, Any]]:
    conn = _get_conn()
    if conn is None:
        return []

    total = conn.execute("SELECT COUNT(*) FROM trade_decisions").fetchone()[0]
    window = max(CONSOLIDATION_MIN_TRADES, min(total // 3, CONSOLIDATION_MAX_TRADES))
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=CONSOLIDATION_MAX_AGE_DAYS)
    ).isoformat()

    cursor = conn.execute(
        "SELECT * FROM trade_decisions WHERE timestamp > ? ORDER BY timestamp DESC LIMIT ?",
        (cutoff, window),
    )
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return rows


def compute_statistics(trades: list[dict[str, Any]]) -> dict[str, Any]:
    if not trades:
        return {}
    total = len(trades)
    pnl = lambda t: (t.get("pnl") or 0)
    wins = [t for t in trades if pnl(t) > 0]
    losses = [t for t in trades if pnl(t) <= 0]

    def build_breakdown(trades_subset, key_fn):
        groups: dict[str, dict[str, Any]] = {}
        for t in trades_subset:
            key = key_fn(t) or "unknown"
            if key not in groups:
                groups[key] = {
                    "total": 0,
                    "wins": 0,
                    "total_pnl": 0.0,
                    "win_pnls": [],
                    "loss_pnls": [],
                }
            g = groups[key]
            g["total"] += 1
            g["total_pnl"] += pnl(t)
            if pnl(t) > 0:
                g["wins"] += 1
                g["win_pnls"].append(pnl(t))
            else:
                g["loss_pnls"].append(abs(pnl(t)))
        return groups

    by_regime = build_breakdown(trades, lambda t: t.get("regime"))
    by_symbol = build_breakdown(trades, lambda t: t.get("symbol"))
    by_emotion = build_breakdown(trades, lambda t: t.get("emotional_self_report"))

    for groups in (by_regime, by_symbol, by_emotion):
        for key in list(groups.keys()):
            d = groups[key]
            d["win_rate"] = d["wins"] / d["total"] if d["total"] > 0 else 0

            avg_win = sum(d["win_pnls"]) / len(d["win_pnls"]) if d["win_pnls"] else 0
            avg_loss = (
                sum(d["loss_pnls"]) / len(d["loss_pnls"]) if d["loss_pnls"] else 0
            )
            d["avg_win"] = round(avg_win, 2)
            d["avg_loss"] = round(avg_loss, 2)
            d["expected_value"] = round(
                d["win_rate"] * avg_win - (1 - d["win_rate"]) * avg_loss, 2
            )

            if d["win_pnls"]:
                top_q = sorted(d["win_pnls"], reverse=True)[
                    : max(1, len(d["win_pnls"]) // 4)
                ]
                d["avg_top_quartile_win"] = round(sum(top_q) / len(top_q), 2)

            d.pop("win_pnls", None)
            d.pop("loss_pnls", None)

    return {
        "total": total,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / total if total > 0 else 0,
        "total_pnl": sum(pnl(t) for t in trades),
        "by_regime": by_regime,
        "by_symbol": by_symbol,
        "by_emotion": by_emotion,
    }


def extract_patterns(stats: dict[str, Any]) -> list[dict[str, Any]]:
    patterns = []
    min_threshold = max(5, stats.get("total", 0) // 10)

    for regime, data in stats.get("by_regime", {}).items():
        if data["total"] < min_threshold:
            continue
        ev = data.get("expected_value", 0)
        wr = data["win_rate"]
        avg_rr = (
            data["avg_win"] / data["avg_loss"] if data.get("avg_loss", 0) > 0 else 0
        )

        if ev > 0 and wr > 0.55:
            optimal_risk = _confidence_to_risk_pct(wr)
            patterns.append(
                {
                    "text": (
                        f"STRONG EDGE: {regime} regime — EV=${ev}/trade, WR={wr:.0%}, "
                        f"avg R:R=1:{avg_rr:.1f} over {data['total']} trades. "
                        f"Total PnL: ${data['total_pnl']:.2f}. "
                        f"Size aggressively ({optimal_risk[0]}-{optimal_risk[1]}% risk) when this setup appears."
                    ),
                    "pattern_id": f"edge_favor_{regime}",
                    "metadata": {
                        "type": "edge_profile",
                        "regime": regime,
                        "expected_value": ev,
                        "win_rate": wr,
                        "avg_rr": round(avg_rr, 1),
                        "confidence": wr,
                        "optimal_risk_pct_min": optimal_risk[0],
                        "optimal_risk_pct_max": optimal_risk[1],
                        "trade_count": data["total"],
                    },
                }
            )
        elif ev < 0:
            patterns.append(
                {
                    "text": (
                        f"AVOID: {regime} regime — EV=${ev}/trade, WR={wr:.0%} "
                        f"over {data['total']} trades. You lose money here even when you win. "
                        f"Total PnL: ${data['total_pnl']:.2f}."
                    ),
                    "pattern_id": f"edge_avoid_{regime}",
                    "metadata": {
                        "type": "edge_avoid",
                        "regime": regime,
                        "expected_value": ev,
                        "win_rate": wr,
                        "confidence": 1.0 - wr,
                        "trade_count": data["total"],
                    },
                }
            )

    for symbol, data in stats.get("by_symbol", {}).items():
        if data["total"] < min_threshold:
            continue
        ev = data.get("expected_value", 0)
        wr = data["win_rate"]
        avg_rr = (
            data["avg_win"] / data["avg_loss"] if data.get("avg_loss", 0) > 0 else 0
        )

        if ev > 0 and wr > 0.55:
            optimal_risk = _confidence_to_risk_pct(wr)
            patterns.append(
                {
                    "text": (
                        f"STRONG EDGE: {symbol} — EV=${ev}/trade, WR={wr:.0%}, "
                        f"avg R:R=1:{avg_rr:.1f} over {data['total']} trades. "
                        f"Total PnL: ${data['total_pnl']:.2f}. "
                        f"Size aggressively ({optimal_risk[0]}-{optimal_risk[1]}% risk) on this symbol."
                    ),
                    "pattern_id": f"edge_favor_{symbol}",
                    "metadata": {
                        "type": "edge_profile",
                        "symbol": symbol,
                        "expected_value": ev,
                        "win_rate": wr,
                        "avg_rr": round(avg_rr, 1),
                        "confidence": wr,
                        "optimal_risk_pct_min": optimal_risk[0],
                        "optimal_risk_pct_max": optimal_risk[1],
                        "trade_count": data["total"],
                    },
                }
            )
        elif ev < -1:
            patterns.append(
                {
                    "text": (
                        f"AVOID: {symbol} — EV=${ev}/trade, WR={wr:.0%} "
                        f"over {data['total']} trades. Total PnL: ${data['total_pnl']:.2f}."
                    ),
                    "pattern_id": f"edge_avoid_{symbol}",
                    "metadata": {
                        "type": "edge_avoid",
                        "symbol": symbol,
                        "expected_value": ev,
                        "win_rate": wr,
                        "confidence": 1.0 - wr,
                        "trade_count": data["total"],
                    },
                }
            )

    for emotion, data in stats.get("by_emotion", {}).items():
        if data["total"] < min_threshold:
            continue
        wr = data["win_rate"]
        if emotion != "calm" and wr < 0.4:
            patterns.append(
                {
                    "text": (
                        f"When feeling {emotion}, win rate drops to {wr:.0%} "
                        f"over {data['total']} trades. Step back and wait for calm."
                    ),
                    "pattern_id": f"emotion_warning_{emotion}",
                    "metadata": {
                        "type": "emotion_warning",
                        "emotion": emotion,
                        "confidence": 1.0 - wr,
                        "trade_count": data["total"],
                    },
                }
            )

    return patterns


def _confidence_to_risk_pct(win_rate: float) -> tuple[int, int]:
    if win_rate >= 0.75:
        return (8, 10)
    elif win_rate >= 0.65:
        return (6, 8)
    elif win_rate >= 0.55:
        return (3, 5)
    return (1, 2)


def consolidate(mcp_client=None) -> list[dict[str, Any]]:
    trades = get_recent_trades()
    if not trades:
        logger.info("No trades to consolidate")
        return []

    stats = compute_statistics(trades)
    patterns = extract_patterns(stats)

    if not patterns:
        logger.info("No new patterns extracted from %d trades", len(trades))
        return []

    from jesse.semantic_memory import SemanticMemory

    memory = SemanticMemory()

    stored = []
    for p in patterns:
        doc_id = memory.add_pattern(
            pattern_id=p["pattern_id"],
            text=p["text"],
            metadata=p["metadata"],
        )
        stored.append({"id": doc_id, **p})
        logger.info("Stored pattern: %s", p["text"][:80])

    logger.info(
        "Consolidation complete: %d patterns from %d trades", len(stored), len(trades)
    )
    return stored
