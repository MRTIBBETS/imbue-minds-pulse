"""Host-wide AI usage collector.

Scans the native Claude Code transcript JSONL files for every agent on the host
(live under ``<host>/agents/*`` and destroyed under ``<host>/preserved/*``),
sums per-turn token usage, prices it with :mod:`minds_pulse.pricing`, and
aggregates into per-agent line items grouped by workspace.

Design notes:

- **Source of truth is the native transcript**, not the ``mngr usage`` event
  files (which are not written in this workspace) and not the per-agent mirror
  log (which would double-count). Only ``type == "assistant"`` records carry
  usage; ``model == "<synthetic>"`` bookkeeping records are skipped.
- **Attribution is by session id, not by directory.** A transcript file
  ``projects/<slug>/<session>.jsonl`` belongs to ``agent-<session-without-dashes>``
  regardless of which agent's config dir physically holds it. Sub-agent files
  under ``.../<session>/subagents/<subid>.jsonl`` are their own line items,
  attributed to the launching agent's workspace and labelled from the sibling
  ``<subid>.meta.json``.
- **Incremental by mtime.** Per-file parse results are cached and only re-read
  when the file's (mtime, size) changes, so a few-second refresh loop stays
  cheap. Transcripts are append-only.
- **Two dollar framings.** Every token is priced at API rates to give the
  comprehensive *consumption value* (``estimated_equivalent_usd``). The separate
  *billed API spend* (``billed_api_usd``) sums only sources that draw the metered
  pay-per-token API; interactive/sub-agent transcript usage draws the
  subscription pool, so it is estimated-equivalent, not billed.
"""

import json
import os
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any

from minds_pulse.pricing import MODEL_PRICING
from minds_pulse.pricing import compute_cost
from minds_pulse.pricing import resolve_pricing_key

# Display buckets shown in the UI. ``cache_creation`` is the total cache-write
# count; ``cache_creation_1h`` (the long-TTL portion, priced at a higher rate) is
# tracked alongside for costing but is a subset of ``cache_creation``, so it is
# excluded from token totals to avoid double counting.
BUCKETS = ("input", "output", "cache_read", "cache_creation")
TRACK_KEYS = ("input", "output", "cache_read", "cache_creation", "cache_creation_1h")

# Interactive and sub-agent sessions run on the Claude subscription pool, so their
# priced cost is an API-equivalent estimate rather than a metered API charge.
SUBSCRIPTION_KINDS = frozenset({"main", "chat", "subagent", "worker", "session"})


def host_dir() -> Path:
    return Path(os.environ.get("MNGR_HOST_DIR", "/mngr"))


def _empty_buckets() -> dict[str, int]:
    return {b: 0 for b in TRACK_KEYS}


def _add_buckets(dst: dict[str, int], src: dict[str, int]) -> None:
    for b in TRACK_KEYS:
        dst[b] = dst.get(b, 0) + src.get(b, 0)


def _session_to_agent_id(session_id: str) -> str:
    return "agent-" + session_id.replace("-", "")


def load_registry(host: Path) -> dict[str, dict[str, Any]]:
    """Map agent_id -> {name, type, workspace, project, color, source, state}."""
    registry: dict[str, dict[str, Any]] = {}
    roots = [(host / "agents", "live"), (host / "preserved", "preserved")]
    for root, source in roots:
        if not root.exists():
            continue
        for data_file in root.glob("*/data.json"):
            try:
                data = json.loads(data_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            agent_id = data.get("id")
            if not agent_id:
                continue
            labels = data.get("labels") or {}
            registry[agent_id] = {
                "name": data.get("name") or agent_id,
                "type": data.get("type") or "unknown",
                "workspace": labels.get("workspace_display_name"),
                "project": labels.get("project"),
                "color": labels.get("color"),
                "source": source,
                "state": data.get("state"),
            }
    return registry


def host_identity(host: Path) -> dict[str, Any]:
    data_file = host / "data.json"
    try:
        data = json.loads(data_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"host_id": None, "host_name": None}
    return {"host_id": data.get("host_id"), "host_name": data.get("host_name")}


def _iter_transcript_files(host: Path) -> list[tuple[Path, str | None]]:
    """Return (path, subagent_id) for every transcript file across the host.

    subagent_id is None for a top-level session file, else the sub-agent's id.
    """
    found: list[tuple[Path, str | None]] = []
    for scope in ("agents", "preserved"):
        base = host / scope
        if not base.exists():
            continue
        projects = base.glob("*/plugin/claude/anthropic/projects")
        for proj in projects:
            # Top-level session transcripts: projects/<slug>/<session>.jsonl
            for f in proj.glob("*/*.jsonl"):
                found.append((f, None))
            # Sub-agent transcripts: projects/<slug>/<session>/subagents/<subid>.jsonl
            for f in proj.glob("*/*/subagents/*.jsonl"):
                found.append((f, f.stem))
    return found


def _parse_transcript(path: Path) -> dict[str, Any]:
    """Sum token usage in one transcript file, grouped by model and by day."""
    model_tokens: dict[str, dict[str, int]] = {}
    model_turns: dict[str, int] = {}
    daily: dict[str, dict[str, dict[str, int]]] = {}
    session_id: str | None = None
    first_ts: str | None = None
    last_ts: str | None = None

    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if session_id is None:
                session_id = record.get("sessionId") or record.get("session_id")
            if record.get("type") != "assistant":
                continue
            message = record.get("message") or {}
            model = message.get("model")
            usage = message.get("usage") or {}
            if not model or model == "<synthetic>":
                continue
            cache_split = usage.get("cache_creation") or {}
            counts = {
                "input": int(usage.get("input_tokens", 0) or 0),
                "output": int(usage.get("output_tokens", 0) or 0),
                "cache_read": int(usage.get("cache_read_input_tokens", 0) or 0),
                "cache_creation": int(usage.get("cache_creation_input_tokens", 0) or 0),
                "cache_creation_1h": int(cache_split.get("ephemeral_1h_input_tokens", 0) or 0),
            }
            if not any(counts.values()):
                continue
            bucket = model_tokens.setdefault(model, _empty_buckets())
            _add_buckets(bucket, counts)
            model_turns[model] = model_turns.get(model, 0) + 1

            ts = record.get("timestamp")
            if ts:
                first_ts = ts if first_ts is None or ts < first_ts else first_ts
                last_ts = ts if last_ts is None or ts > last_ts else last_ts
                day = ts[:10]
                day_model = daily.setdefault(day, {}).setdefault(model, _empty_buckets())
                _add_buckets(day_model, counts)

    return {
        "session_id": session_id or path.stem,
        "model_tokens": model_tokens,
        "model_turns": model_turns,
        "daily": daily,
        "first_ts": first_ts,
        "last_ts": last_ts,
    }


class Collector:
    """Caches per-file parses and builds host-wide snapshots on demand."""

    def __init__(self) -> None:
        # path -> (mtime, size, parsed)
        self._cache: dict[str, tuple[float, int, dict[str, Any]]] = {}

    def _parsed(self, path: Path) -> dict[str, Any] | None:
        try:
            stat = path.stat()
        except OSError:
            return None
        key = str(path)
        cached = self._cache.get(key)
        if cached is not None and cached[0] == stat.st_mtime and cached[1] == stat.st_size:
            return cached[2]
        parsed = _parse_transcript(path)
        self._cache[key] = (stat.st_mtime, stat.st_size, parsed)
        return parsed

    def build_snapshot(self, host: Path | None = None) -> dict[str, Any]:
        host = host or host_dir()
        registry = load_registry(host)
        identity = host_identity(host)
        host_name = identity.get("host_name") or "this host"

        # Accumulate per-agent line items keyed by a stable string.
        agents: dict[str, dict[str, Any]] = {}

        def _agent_acc(key: str, name: str, kind: str, workspace: str, session_id: str, path: Path) -> dict[str, Any]:
            acc = agents.get(key)
            if acc is None:
                acc = {
                    "key": key,
                    "name": name,
                    "kind": kind,
                    "workspace": workspace,
                    "session_id": session_id,
                    "transcript_path": str(path),
                    "model_tokens": {},
                    "model_turns": {},
                    "first_ts": None,
                    "last_ts": None,
                }
                agents[key] = acc
            return acc

        global_daily: dict[str, dict[str, dict[str, int]]] = {}

        for path, subagent_id in _iter_transcript_files(host):
            parsed = self._parsed(path)
            if parsed is None or not parsed["model_tokens"]:
                continue
            session_id = parsed["session_id"]

            if subagent_id is not None:
                key = "sub:" + subagent_id
                meta = _read_subagent_meta(path)
                agent_type = meta.get("agentType") or "sub-agent"
                desc = (meta.get("description") or "").strip()
                name = f"{agent_type}" + (f" - {desc}" if desc else "")
                kind = "subagent"
                # Parent session owns the workspace; parent dir name is the parent session.
                parent_session = path.parent.parent.name
                parent_agent = registry.get(_session_to_agent_id(parent_session), {})
                workspace = parent_agent.get("workspace") or host_name
            else:
                agent_id = _session_to_agent_id(session_id)
                info = registry.get(agent_id)
                key = agent_id
                if info is not None:
                    name = info["name"]
                    kind = {"main": "main", "claude": "chat"}.get(info["type"], info["type"])
                    workspace = info.get("workspace") or host_name
                else:
                    name = "session " + session_id[:8]
                    kind = "session"
                    workspace = host_name

            acc = _agent_acc(key, name, kind, workspace, session_id, path)
            for model, counts in parsed["model_tokens"].items():
                dst = acc["model_tokens"].setdefault(model, _empty_buckets())
                _add_buckets(dst, counts)
            for model, turns in parsed["model_turns"].items():
                acc["model_turns"][model] = acc["model_turns"].get(model, 0) + turns
            for ts_field in ("first_ts", "last_ts"):
                val = parsed[ts_field]
                if val is None:
                    continue
                cur = acc[ts_field]
                if cur is None or (val < cur if ts_field == "first_ts" else val > cur):
                    acc[ts_field] = val
            for day, per_model in parsed["daily"].items():
                for model, counts in per_model.items():
                    dst = global_daily.setdefault(day, {}).setdefault(model, _empty_buckets())
                    _add_buckets(dst, counts)

        return _assemble(agents, global_daily, identity, host_name, registry)


def _read_subagent_meta(transcript_path: Path) -> dict[str, Any]:
    meta_path = transcript_path.with_suffix(".meta.json")
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _cost_for_model_tokens(model_tokens: dict[str, dict[str, int]]) -> tuple[float, list[str]]:
    """Total priced cost across models; also return the list of unpriced models."""
    total = 0.0
    unpriced: list[str] = []
    for model, counts in model_tokens.items():
        cost = compute_cost(
            model,
            counts["input"],
            counts["output"],
            counts["cache_read"],
            counts["cache_creation"],
            counts.get("cache_creation_1h", 0),
        )
        if cost is None:
            unpriced.append(model)
        else:
            total += cost
    return total, unpriced


def _sum_tokens(model_tokens: dict[str, dict[str, int]]) -> dict[str, int]:
    total = _empty_buckets()
    for counts in model_tokens.values():
        _add_buckets(total, counts)
    total["total"] = sum(total[b] for b in BUCKETS)
    return total


def _assemble(
    agents: dict[str, dict[str, Any]],
    global_daily: dict[str, dict[str, dict[str, int]]],
    identity: dict[str, Any],
    host_name: str,
    registry: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    # Finalize each agent line item with cost + token totals.
    agent_items: list[dict[str, Any]] = []
    global_model_tokens: dict[str, dict[str, int]] = {}
    global_model_turns: dict[str, int] = {}
    for acc in agents.values():
        cost, unpriced = _cost_for_model_tokens(acc["model_tokens"])
        tokens = _sum_tokens(acc["model_tokens"])
        turns = sum(acc["model_turns"].values())
        models = sorted(acc["model_tokens"].keys())
        billed = acc["kind"] not in SUBSCRIPTION_KINDS
        agent_items.append(
            {
                "key": acc["key"],
                "name": acc["name"],
                "kind": acc["kind"],
                "workspace": acc["workspace"],
                "session_id": acc["session_id"],
                "transcript_path": acc["transcript_path"],
                "models": models,
                "tokens": tokens,
                "turns": turns,
                "cost_usd": round(cost, 4),
                "billed_api": billed,
                "unpriced_models": unpriced,
                "first_ts": acc["first_ts"],
                "last_ts": acc["last_ts"],
            }
        )
        for model, counts in acc["model_tokens"].items():
            dst = global_model_tokens.setdefault(model, _empty_buckets())
            _add_buckets(dst, counts)
        for model, t in acc["model_turns"].items():
            global_model_turns[model] = global_model_turns.get(model, 0) + t

    agent_items.sort(key=lambda a: a["cost_usd"], reverse=True)

    # Group into workspaces.
    workspaces: dict[str, dict[str, Any]] = {}
    for item in agent_items:
        ws = workspaces.setdefault(
            item["workspace"], {"workspace": item["workspace"], "agents": [], "cost_usd": 0.0}
        )
        ws["agents"].append(item)
        ws["cost_usd"] += item["cost_usd"]
    workspace_list = []
    for ws in workspaces.values():
        ws_tokens = _empty_buckets()
        ws_tokens["total"] = 0
        for a in ws["agents"]:
            for b in BUCKETS:
                ws_tokens[b] += a["tokens"][b]
            ws_tokens["total"] += a["tokens"]["total"]
        workspace_list.append(
            {
                "workspace": ws["workspace"],
                "cost_usd": round(ws["cost_usd"], 4),
                "tokens": ws_tokens,
                "agent_count": len(ws["agents"]),
                "agents": ws["agents"],
            }
        )
    workspace_list.sort(key=lambda w: w["cost_usd"], reverse=True)

    # Global by-model breakdown.
    by_model = []
    for model, counts in global_model_tokens.items():
        cost = compute_cost(
            model,
            counts["input"],
            counts["output"],
            counts["cache_read"],
            counts["cache_creation"],
            counts.get("cache_creation_1h", 0),
        )
        tokens = dict(counts)
        tokens["total"] = sum(counts[b] for b in BUCKETS)
        by_model.append(
            {
                "model": model,
                "priced": cost is not None,
                "cost_usd": round(cost, 4) if cost is not None else None,
                "tokens": tokens,
                "turns": global_model_turns.get(model, 0),
            }
        )
    by_model.sort(key=lambda m: (m["cost_usd"] or 0), reverse=True)

    # Per-bucket cost across all models. Cache-write mixes 5-minute and 1-hour
    # TTLs at different rates, so it is priced from the split rather than a single
    # rate.
    by_bucket = {}
    for b in BUCKETS:
        cost = 0.0
        toks = 0
        for model, counts in global_model_tokens.items():
            key = resolve_pricing_key(model)
            toks += counts[b]
            if key is None:
                continue
            p = MODEL_PRICING[key]
            if b == "cache_creation":
                cache_1h = min(counts.get("cache_creation_1h", 0), counts["cache_creation"])
                cache_5m = counts["cache_creation"] - cache_1h
                cost += cache_5m * p.cache_creation_input_token_cost + cache_1h * (p.input_cost_per_token * 2.0)
                continue
            rate = {
                "input": p.input_cost_per_token,
                "output": p.output_cost_per_token,
                "cache_read": p.cache_read_input_token_cost,
            }[b]
            cost += counts[b] * rate
        by_bucket[b] = {"tokens": toks, "cost_usd": round(cost, 4)}

    # Time series (daily cost + tokens).
    timeseries = []
    for day in sorted(global_daily.keys()):
        day_cost, _ = _cost_for_model_tokens(global_daily[day])
        day_tokens = _sum_tokens(global_daily[day])
        timeseries.append(
            {"date": day, "cost_usd": round(day_cost, 4), "tokens_total": day_tokens["total"]}
        )

    # Totals.
    total_estimated = round(sum(a["cost_usd"] for a in agent_items), 4)
    total_billed = round(sum(a["cost_usd"] for a in agent_items if a["billed_api"]), 4)
    total_tokens = _empty_buckets()
    total_tokens["total"] = 0
    for a in agent_items:
        for b in BUCKETS:
            total_tokens[b] += a["tokens"][b]
        total_tokens["total"] += a["tokens"]["total"]

    projection = _project(timeseries)
    all_unpriced = sorted({m for a in agent_items for m in a["unpriced_models"]})
    pricing = _pricing_reference(global_model_tokens)
    confidence = _confidence(global_model_tokens, total_tokens)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "host": identity,
        "api_key_present": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "totals": {
            "estimated_equivalent_usd": total_estimated,
            "billed_api_usd": total_billed,
            "tokens": total_tokens,
            "turns": sum(a["turns"] for a in agent_items),
            "agent_count": len(agent_items),
            "workspace_count": len(workspace_list),
        },
        "workspaces": workspace_list,
        "by_model": by_model,
        "by_bucket": by_bucket,
        "timeseries": timeseries,
        "projection": projection,
        "unpriced_models": all_unpriced,
        "pricing": pricing,
        "confidence": confidence,
        "gaps": _known_gaps(total_billed),
    }


def _pricing_reference(global_model_tokens: dict[str, dict[str, int]]) -> dict[str, Any]:
    """The formulas and the live per-million-token rates for the models in use.

    Surfaces exactly how every dollar figure on the dashboard is derived, so the
    numbers are auditable rather than opaque.
    """
    per_million = 1_000_000
    rates = []
    for model in sorted(global_model_tokens):
        key = resolve_pricing_key(model)
        if key is None:
            rates.append({"model": model, "priced": False})
            continue
        p = MODEL_PRICING[key]
        rates.append(
            {
                "model": model,
                "priced": True,
                "input_per_mtok": round(p.input_cost_per_token * per_million, 4),
                "output_per_mtok": round(p.output_cost_per_token * per_million, 4),
                "cache_write_5m_per_mtok": round(p.cache_creation_input_token_cost * per_million, 4),
                "cache_write_1h_per_mtok": round(p.input_cost_per_token * 2.0 * per_million, 4),
                "cache_read_per_mtok": round(p.cache_read_input_token_cost * per_million, 4),
            }
        )
    return {
        "cost_formula": (
            "cost = input_tokens x rate_in + cache_write_tokens x rate_cw "
            "+ cache_read_tokens x rate_cr + output_tokens x rate_out"
        ),
        "projection_formula": (
            "projected_30d = (sum of daily cost over the last N active days / N) x 30"
        ),
        "rate_unit": "USD per 1,000,000 tokens",
        "rates": rates,
        "source": (
            "Rates mirror the mngr billing table (mngr_usage/pricing.py), itself synced to "
            "the litellm proxy. Token counts are read from each agent's Claude Code transcript; "
            "the four buckets are non-overlapping, so there is no double counting."
        ),
    }


def _confidence(
    global_model_tokens: dict[str, dict[str, int]], total_tokens: dict[str, int]
) -> dict[str, Any]:
    """Score how much to trust the dollar figure, and say what drives it.

    The token counts are exact (Anthropic's own per-message usage), so the score
    is driven by pricing coverage: what share of tokens is on a model we have a
    rate for. Unpriced tokens contribute no cost, so heavy unpriced usage means
    the total is understated -- that is what lowers confidence.
    """
    total = total_tokens.get("total", 0)
    priced_tokens = 0
    for model, counts in global_model_tokens.items():
        if resolve_pricing_key(model) is not None:
            priced_tokens += sum(counts[b] for b in BUCKETS)
    coverage = (priced_tokens / total) if total else 1.0
    # Token math is exact and cache TTLs are split, so full coverage tops out at
    # 98 (the residual accounts for a rate table that can lag a price change).
    score = round(coverage * 98)
    if score >= 90:
        level = "High"
    elif score >= 70:
        level = "Medium"
    else:
        level = "Low"
    return {
        "level": level,
        "score": score,
        "priced_token_pct": round(coverage * 100, 2),
        "exact": (
            "Token counts are Anthropic's own per-message usage read from each transcript -- "
            "measured, not sampled or estimated. Cache writes are split by 5-minute vs 1-hour TTL "
            "and priced at the correct rate for each."
        ),
        "estimated": (
            "The dollar value applies published API rates to those tokens. It is API-equivalent "
            "value, not necessarily your bill: subscription-pool usage is covered by your plan fee, "
            "and a rate could lag a price change until the table is updated."
        ),
    }


def _project(timeseries: list[dict[str, Any]]) -> dict[str, Any]:
    """Forecast a 30-day run-rate from the most recent active days.

    This is a *pace* extrapolation, not a bill: nearly all spend is live agent
    work (building and chatting), which is front-loaded, while the apps a mind
    builds make no model calls and cost ~nothing to run. Projecting a handful of
    build-heavy days across a month overstates steady-state cost, so ``provisional``
    is set until there are enough days for the average to mean something.
    """
    if not timeseries:
        return {
            "basis_days": 0,
            "daily_avg_usd": 0.0,
            "projected_30d_usd": 0.0,
            "window": None,
            "provisional": True,
        }
    recent = timeseries[-7:]
    avg = sum(d["cost_usd"] for d in recent) / len(recent)
    return {
        "basis_days": len(recent),
        "daily_avg_usd": round(avg, 4),
        "projected_30d_usd": round(avg * 30, 2),
        "window": {"start": recent[0]["date"], "end": recent[-1]["date"]},
        # Fewer than 3 days can't distinguish a build sprint from steady-state use.
        "provisional": len(recent) < 3,
    }


def _known_gaps(total_billed: float) -> list[str]:
    gaps = []
    if total_billed == 0.0:
        gaps.append(
            "No metered pay-per-token API spend captured yet: interactive and sub-agent usage "
            "draws the Claude subscription pool, so it is shown as API-equivalent value. Direct "
            "API callers (the browser-automation lib) are not yet instrumented."
        )
    return gaps
