"""Per-capability metrics, computed from the run store (the system of record), so there is no second counter to drift.

Definitions, stated so the numbers can be read honestly:
  settled          a run that reached a final status (success, business_outcome, hard_failure, needs_review, abandoned)
  success rate     (success + business_outcome) / settled. A business outcome ("no such patient") is a correct answer.
  failure rate     hard_failure / settled;  needs_review is counted separately because it is neither
  escalation rate  settled runs in which a human intervened / settled
  latency          seconds from start to finish of settled runs that actually executed (not deduplicated or refused)
Runs waiting for approval, queued, running, rejected or dry runs are reported as counts but excluded from the rates."""
from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from runs.store import RunStore

SETTLED = ("success", "business_outcome", "hard_failure", "needs_review", "abandoned")
GOOD = ("success", "business_outcome")


def _percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, max(0, math.ceil(p / 100 * len(ordered)) - 1))], 3)


def _seconds(result: dict[str, Any]) -> float | None:
    try:
        started, finished = datetime.fromisoformat(result["started_at"]), datetime.fromisoformat(result["finished_at"])
        return max(0.0, (finished - started).total_seconds())
    except (KeyError, TypeError, ValueError):
        return None


def compute(store: RunStore, since_hours: float | None = None, capability_id: str | None = None) -> dict[str, Any]:
    where, args = [], []
    if since_hours:
        where.append("created_at >= ?")
        args.append((datetime.now(UTC) - timedelta(hours=since_hours)).isoformat(timespec="seconds"))
    if capability_id:
        where.append("capability_id = ?")
        args.append(capability_id)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    rows = store._rows(f"SELECT capability_id, status, committed, error_code, result_json FROM runs {clause}", tuple(args))

    per: dict[str, dict[str, Any]] = defaultdict(lambda: {"statuses": defaultdict(int), "latencies": [], "escalated": 0, "recovered": 0,
                                                          "committed": 0, "error_codes": defaultdict(int)})
    for row in rows:
        m = per[row["capability_id"]]
        m["statuses"][row["status"]] += 1
        m["committed"] += int(bool(row["committed"]))
        if row["error_code"]:
            m["error_codes"][row["error_code"]] += 1
        if row["status"] in SETTLED and row["result_json"]:
            result = json.loads(row["result_json"])
            m["escalated"] += int(bool(result.get("escalated")))
            m["recovered"] += int(bool(result.get("recovered")))
            seconds = _seconds(result)
            if seconds is not None and not result.get("deduplicated") and row["status"] != "abandoned":
                m["latencies"].append(seconds)

    out = []
    for cap, m in sorted(per.items()):
        settled = sum(m["statuses"][s] for s in SETTLED)
        good = sum(m["statuses"][s] for s in GOOD)
        rate = lambda n: round(n / settled, 4) if settled else None  # noqa: E731
        out.append({
            "capability_id": cap, "runs": sum(m["statuses"].values()), "settled": settled, "statuses": dict(m["statuses"]),
            "success_rate": rate(good), "failure_rate": rate(m["statuses"]["hard_failure"]), "needs_review": m["statuses"]["needs_review"],
            "escalation_rate": rate(m["escalated"]), "auto_recovered": m["recovered"], "committed": m["committed"],
            "latency_s": {"p50": _percentile(m["latencies"], 50), "p95": _percentile(m["latencies"], 95), "max": _percentile(m["latencies"], 100),
                          "n": len(m["latencies"])},
            "top_error_codes": dict(sorted(m["error_codes"].items(), key=lambda kv: -kv[1])[:5]),
        })
    pending = store._rows("SELECT COUNT(*) AS n FROM approvals WHERE decision IS NULL")[0]["n"]
    repairs = store._rows("SELECT COUNT(*) AS n FROM repairs WHERE status='pending'")[0]["n"]
    canaries = store._rows("SELECT capability_id, ok FROM canary_runs WHERE id IN (SELECT MAX(id) FROM canary_runs GROUP BY capability_id)")
    return {
        "window_hours": since_hours, "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "capabilities": out,
        "totals": {"runs": sum(c["runs"] for c in out), "pending_approvals": pending, "pending_repairs": repairs,
                   "failing_canaries": [c["capability_id"] for c in canaries if not c["ok"]]},
    }


def prometheus(metrics: dict[str, Any], pool: dict[str, int] | None = None) -> str:
    """The same numbers in Prometheus text exposition format, for anything that scrapes /v1/metrics.prom."""
    lines = ["# HELP cua_runs_total Runs by capability and status.", "# TYPE cua_runs_total gauge"]
    for c in metrics["capabilities"]:
        for status, n in c["statuses"].items():
            lines.append(f'cua_runs_total{{capability="{c["capability_id"]}",status="{status}"}} {n}')
    for name, key, help_ in (("cua_success_rate", "success_rate", "Share of settled runs that succeeded or returned a business outcome."),
                             ("cua_escalation_rate", "escalation_rate", "Share of settled runs in which a human intervened.")):
        lines += [f"# HELP {name} {help_}", f"# TYPE {name} gauge"]
        lines += [f'{name}{{capability="{c["capability_id"]}"}} {c[key]}' for c in metrics["capabilities"] if c[key] is not None]
    lines += ["# HELP cua_latency_seconds Run latency percentiles.", "# TYPE cua_latency_seconds gauge"]
    for c in metrics["capabilities"]:
        for q in ("p50", "p95"):
            if c["latency_s"][q] is not None:
                lines.append(f'cua_latency_seconds{{capability="{c["capability_id"]}",quantile="{q}"}} {c["latency_s"][q]}')
    t = metrics["totals"]
    lines += ["# TYPE cua_pending_approvals gauge", f"cua_pending_approvals {t['pending_approvals']}",
              "# TYPE cua_pending_repairs gauge", f"cua_pending_repairs {t['pending_repairs']}",
              "# TYPE cua_failing_canaries gauge", f"cua_failing_canaries {len(t['failing_canaries'])}"]
    for k, v in (pool or {}).items():
        lines.append(f"cua_browser_pool_{k} {v}")
    return "\n".join(lines) + "\n"
