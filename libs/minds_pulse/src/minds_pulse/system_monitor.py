"""Live system activity metrics for the Minds Monitor.

Complements the cost engine (:mod:`minds_pulse.collector`) with the
"Activity Monitor" side: CPU load, memory (and the memory-pressure / OOM-shed
story that matters most in a small container), disk usage and what's growing,
the supervisord service list with per-service CPU/RAM, an agent roster, and a
rolling in-memory history for sparklines.

Sampling is on-demand: each request produces a fresh sample and appends it to a
bounded history deque, so graphs build while the dashboard is open (like macOS
Activity Monitor) without a background thread. Disk usage is cached on a longer
interval because directory sizing is comparatively expensive.
"""

import collections
import json
import re
import subprocess
from datetime import datetime
from datetime import timezone
from pathlib import Path
from time import monotonic
from typing import Any

import psutil

from minds_pulse.collector import host_dir
from minds_pulse.collector import host_identity
from minds_pulse.collector import load_registry

# One process_shed line per earlyoom kill; other record types (notice_delivered)
# are bookkeeping and ignored here.
_SHED_LEDGER = Path("runtime/oom_priority/events/shed.jsonl")
_SHED_RECORD_TYPE = "process_shed"

_STATUS_RE = re.compile(r"^(\S+)\s+(\w+)\s*(.*)$")
_PID_RE = re.compile(r"pid (\d+)")

# Directories worth attributing disk growth to, resolved at call time.
_DISK_TARGETS = (
    ("Agent state & transcripts", "{host}/agents"),
    ("Git history", ".git"),
    ("Runtime state", "runtime"),
    ("Uploads", "uploads"),
)


def _memory_pressure(available_pct: float) -> str:
    if available_pct > 30:
        return "ok"
    if available_pct >= 15:
        return "elevated"
    return "high"


def _du_bytes(path: Path) -> int | None:
    if not path.exists():
        return None
    try:
        out = subprocess.run(
            ["du", "-sb", str(path)], capture_output=True, text=True, timeout=20, check=True
        )
    except (subprocess.SubprocessError, OSError):
        return None
    try:
        return int(out.stdout.split("\t", 1)[0])
    except (ValueError, IndexError):
        return None


class SystemMonitor:
    """Samples host activity on demand and keeps a short rolling history."""

    def __init__(self, history_len: int = 180) -> None:
        self._history: collections.deque[dict[str, Any]] = collections.deque(maxlen=history_len)
        self._disk_cache: tuple[float, dict[str, Any]] | None = None
        # Prime the CPU counters so the first real sample reflects a delta.
        psutil.cpu_percent(None)

    def _cpu(self) -> dict[str, Any]:
        try:
            load1, load5, load15 = psutil.getloadavg()
        except (OSError, AttributeError):
            load1 = load5 = load15 = 0.0
        cores = psutil.cpu_count() or 1
        return {
            "cores": cores,
            "percent": psutil.cpu_percent(None),
            "load1": round(load1, 2),
            "load5": round(load5, 2),
            "load15": round(load15, 2),
            "load1_per_core": round(load1 / cores, 2),
        }

    def _memory(self) -> dict[str, Any]:
        vm = psutil.virtual_memory()
        available_pct = vm.available / vm.total * 100 if vm.total else 0.0
        return {
            "total": vm.total,
            "used": vm.total - vm.available,
            "available": vm.available,
            "percent": round(vm.percent, 1),
            "available_pct": round(available_pct, 1),
            "pressure": _memory_pressure(available_pct),
        }

    def _top_processes(self, limit: int = 8) -> dict[str, list[dict[str, Any]]]:
        rows: list[dict[str, Any]] = []
        for proc in psutil.process_iter(["pid", "name", "memory_info", "cpu_percent"]):
            try:
                info = proc.info
                mem = info.get("memory_info")
                rows.append(
                    {
                        "pid": info.get("pid"),
                        "name": (info.get("name") or "?")[:24],
                        "rss": mem.rss if mem else 0,
                        "cpu": round(info.get("cpu_percent") or 0.0, 1),
                    }
                )
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        by_mem = sorted(rows, key=lambda r: r["rss"], reverse=True)[:limit]
        by_cpu = sorted(rows, key=lambda r: r["cpu"], reverse=True)[:limit]
        return {"by_memory": by_mem, "by_cpu": by_cpu}

    def _disk(self) -> dict[str, Any]:
        now = monotonic()
        if self._disk_cache is not None and (now - self._disk_cache[0]) < 60:
            return self._disk_cache[1]
        usage = psutil.disk_usage("/")
        host = str(host_dir())
        breakdown = []
        for label, template in _DISK_TARGETS:
            size = _du_bytes(Path(template.format(host=host)))
            if size is not None:
                breakdown.append({"label": label, "bytes": size})
        breakdown.sort(key=lambda d: d["bytes"], reverse=True)
        data = {
            "total": usage.total,
            "used": usage.used,
            "free": usage.free,
            "percent": round(usage.percent, 1),
            "breakdown": breakdown,
        }
        self._disk_cache = (now, data)
        return data

    def _services(self) -> list[dict[str, Any]]:
        try:
            out = subprocess.run(
                ["supervisorctl", "status"], capture_output=True, text=True, timeout=15
            )
        except (subprocess.SubprocessError, OSError):
            return []
        services = []
        for line in out.stdout.splitlines():
            match = _STATUS_RE.match(line.strip())
            if not match:
                continue
            name, state, detail = match.group(1), match.group(2), match.group(3)
            entry: dict[str, Any] = {"name": name, "state": state, "cpu": None, "rss": None, "uptime": None}
            pid_match = _PID_RE.search(detail)
            uptime_match = re.search(r"uptime ([0-9:]+(?: days?, [0-9:]+)?)", detail)
            if uptime_match:
                entry["uptime"] = uptime_match.group(1)
            if pid_match:
                entry.update(self._process_usage(int(pid_match.group(1))))
            services.append(entry)
        return services

    def _process_usage(self, pid: int) -> dict[str, Any]:
        """Total RSS and CPU% for a service's process tree (parent + children)."""
        try:
            parent = psutil.Process(pid)
            procs = [parent, *parent.children(recursive=True)]
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return {"cpu": None, "rss": None}
        rss = 0
        cpu = 0.0
        for proc in procs:
            try:
                mem = proc.memory_info()
                rss += mem.rss
                cpu += proc.cpu_percent(None)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return {"cpu": round(cpu, 1), "rss": rss}

    def _agents(self) -> dict[str, Any]:
        registry = load_registry(host_dir())
        live = [a for a in registry.values() if a.get("source") == "live"]
        roster = [
            {
                "name": a["name"],
                "type": a["type"],
                "project": a.get("project"),
                "state": a.get("state"),
            }
            for a in live
        ]
        roster.sort(key=lambda a: a["name"])
        return {"count": len(live), "agents": roster}

    def _oom(self) -> dict[str, Any]:
        events: list[dict[str, Any]] = []
        if _SHED_LEDGER.exists():
            for line in _SHED_LEDGER.read_text(encoding="utf-8").splitlines():
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("type") == _SHED_RECORD_TYPE:
                    events.append(
                        {
                            "timestamp": record.get("timestamp"),
                            "pid": record.get("pid"),
                            "comm": record.get("comm"),
                            "agent_name": record.get("agent_name"),
                            "is_worker": record.get("is_worker"),
                        }
                    )
        events.reverse()  # newest first
        return {"count": len(events), "recent": events[:25]}

    def snapshot(self) -> dict[str, Any]:
        cpu = self._cpu()
        memory = self._memory()
        sampled_at = datetime.now(timezone.utc).isoformat()
        self._history.append(
            {"t": sampled_at, "cpu": cpu["percent"], "mem": memory["percent"]}
        )
        return {
            "sampled_at": sampled_at,
            "host": host_identity(host_dir()),
            "cpu": cpu,
            "memory": memory,
            "disk": self._disk(),
            "processes": self._top_processes(),
            "services": self._services(),
            "agents": self._agents(),
            "oom": self._oom(),
            "history": list(self._history),
        }
