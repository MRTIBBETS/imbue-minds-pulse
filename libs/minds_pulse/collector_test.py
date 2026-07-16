"""Unit tests for the usage collector over a synthetic host tree."""

import json
from pathlib import Path

from minds_pulse.collector import Collector
from minds_pulse.pricing import compute_cost

_SID = "11111111-1111-1111-1111-111111111111"
_AGENT_ID = "agent-" + _SID.replace("-", "")


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")


def _assistant(model: str, usage: dict) -> dict:
    return {
        "type": "assistant",
        "sessionId": _SID,
        "timestamp": "2026-07-16T20:00:00.000Z",
        "message": {"model": model, "usage": usage},
    }


def _build_host(tmp: Path) -> Path:
    (tmp / "data.json").write_text(json.dumps({"host_id": "h1", "host_name": "test-host"}))
    agent_dir = tmp / "agents" / _AGENT_ID
    (agent_dir).mkdir(parents=True)
    (agent_dir / "data.json").write_text(
        json.dumps(
            {
                "id": _AGENT_ID,
                "name": "test-agent",
                "type": "claude",
                "labels": {"workspace_display_name": "ws1", "project": "proj"},
            }
        )
    )
    projects = agent_dir / "plugin" / "claude" / "anthropic" / "projects" / "slug"
    # Main session: one real turn, one synthetic (must be skipped), one user record.
    _write_jsonl(
        projects / f"{_SID}.jsonl",
        [
            _assistant(
                "claude-opus-4-8",
                {
                    "input_tokens": 10,
                    "output_tokens": 100,
                    "cache_read_input_tokens": 1000,
                    "cache_creation_input_tokens": 200,
                    "cache_creation": {"ephemeral_1h_input_tokens": 50, "ephemeral_5m_input_tokens": 150},
                },
            ),
            _assistant("<synthetic>", {"input_tokens": 999999}),
            {"type": "user", "sessionId": _SID},
        ],
    )
    # A sub-agent transcript with its own meta.
    sub_dir = projects / _SID / "subagents"
    _write_jsonl(
        sub_dir / "agent-sub1.jsonl",
        [_assistant("claude-opus-4-8", {"input_tokens": 5, "output_tokens": 20})],
    )
    (sub_dir / "agent-sub1.meta.json").write_text(
        json.dumps({"agentType": "Explore", "description": "look at X"})
    )
    return tmp


def test_collector_prices_and_attributes(tmp_path: Path) -> None:
    host = _build_host(tmp_path)
    snap = Collector().build_snapshot(host=host)

    # The synthetic record is skipped; one real turn on the main agent + one sub-agent turn.
    assert snap["totals"]["turns"] == 2
    assert snap["totals"]["agent_count"] == 2
    assert snap["totals"]["workspace_count"] == 1
    assert snap["host"]["host_name"] == "test-host"

    ws = snap["workspaces"][0]
    assert ws["workspace"] == "ws1"
    by_name = {a["name"]: a for a in ws["agents"]}
    assert "test-agent" in by_name
    main = by_name["test-agent"]
    assert main["kind"] == "chat"
    assert main["tokens"] == {
        "input": 10,
        "output": 100,
        "cache_read": 1000,
        "cache_creation": 200,
        "cache_creation_1h": 50,
        "total": 1310,
    }
    expected = round(compute_cost("claude-opus-4-8", 10, 100, 1000, 200, 50), 4)
    assert main["cost_usd"] == expected

    # The sub-agent is its own line item, labelled from its meta, same workspace.
    sub = next(a for a in ws["agents"] if a["kind"] == "subagent")
    assert "Explore" in sub["name"]

    # A single day of history is too little to trust as a run-rate.
    assert snap["projection"]["provisional"] is True


def test_no_double_counting_across_files(tmp_path: Path) -> None:
    host = _build_host(tmp_path)
    snap = Collector().build_snapshot(host=host)
    # Total cache-read is exactly the main turn's 1000 -- the sub-agent turn had none,
    # and nothing is counted twice.
    assert snap["totals"]["tokens"]["cache_read"] == 1000
