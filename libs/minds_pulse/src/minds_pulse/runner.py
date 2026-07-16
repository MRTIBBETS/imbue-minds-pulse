"""Host-wide AI/LLM usage and cost tracker with per-agent and per-workspace line items.

Services run from /mngr/code (the repo root). Conventions:

- Persistent state (anything written and read across runs -- cursors,
  caches, snapshots, user records): read and write it under ``DATA_DIR``
  (defined below), never a hardcoded ``runtime/minds-pulse/`` at the call
  site. ``DATA_DIR`` defaults to ``runtime/minds-pulse/`` but honors the
  ``MINDS_PULSE_DATA_DIR`` env var, so an editing agent can point a throwaway
  instance at a *copy* of the data instead of the live store (see the
  update-service skill). Do NOT use ``Path(__file__)``-based paths for
  state -- the bug to avoid is one process writing to
  ``/mngr/code/runtime/...`` while another reads from
  ``/mngr/code/libs/<pkg>/runtime/...``.
- Static assets shipped alongside this file (templates, default
  configs, bundled JSON): ``Path(__file__).parent / "assets/..."`` is
  fine and is the right pattern.
- Listen port: bind ``PORT`` (defined below), which defaults to this
  service's assigned port but honors the ``MINDS_PULSE_PORT`` env var, so
  an editing agent can boot a throwaway instance on a *spare* port
  alongside the live one (see the update-service skill). Never hardcode
  the port at the ``run_simple`` call.

This is a synchronous Flask app served by the threaded Werkzeug server.
The system_interface proxy at ``/service/minds-pulse/`` rewrites absolute
paths in served HTML and installs a scoped service worker that prepends
the prefix to the page's own fetches, so the app can serve at ``/`` and
still work behind the proxy. Use ``flask_sock`` if you need WebSockets.
"""

import json
import os
import threading
from time import monotonic
from pathlib import Path
from typing import Any

from flask import Flask
from flask import Response
from flask import jsonify
from flask import request
from werkzeug.serving import run_simple

from minds_pulse.collector import Collector
from minds_pulse.system_monitor import SystemMonitor

# Persistent state for this service lives under DATA_DIR. It defaults to
# ``runtime/minds-pulse/`` but is overridable via the ``MINDS_PULSE_DATA_DIR`` env var so a
# throwaway instance can run against a *copy* of the data while editing --
# see the update-service skill. Always read/write state through DATA_DIR;
# never hardcode ``runtime/minds-pulse/`` at a call site, or the override is
# bypassed. A writing call site should ``DATA_DIR.mkdir(parents=True,
# exist_ok=True)`` before writing.
DATA_DIR = Path(os.environ.get("MINDS_PULSE_DATA_DIR", "runtime/minds-pulse"))

# Listen port. Defaults to this service's assigned port but is overridable via
# the ``MINDS_PULSE_PORT`` env var so an editing agent can boot a throwaway
# instance on a spare port next to the live one (see the update-service skill).
# Never hardcode the port at the ``run_simple`` call, or the override is bypassed.
PORT = int(os.environ.get("MINDS_PULSE_PORT", "8080"))

app = Flask("minds_pulse", static_folder=None)

ASSETS_DIR = Path(__file__).parent / "assets"

# The snapshot is rebuilt on demand with a short time-to-live: a request older
# than this recomputes, fresher requests reuse the cached result. The collector
# only re-reads transcript files whose mtime changed, so a rebuild is cheap.
# The dashboard polls every few seconds, so this gives "close to real-time"
# freshness (a completed turn's cost appears within one TTL) without a background
# thread. The cached snapshot is mirrored to DATA_DIR so the derived record
# persists on disk.
SNAPSHOT_TTL_SECONDS = 4.0

_collector = Collector()
_system_monitor = SystemMonitor()
_cache_lock = threading.Lock()
_cached_snapshot: dict[str, Any] = {}
_cached_at: float = 0.0


def _get_snapshot() -> dict[str, Any]:
    global _cached_snapshot, _cached_at
    with _cache_lock:
        now = monotonic()
        if _cached_snapshot and (now - _cached_at) < SNAPSHOT_TTL_SECONDS:
            return _cached_snapshot
        snapshot = _collector.build_snapshot()
        _cached_snapshot = snapshot
        _cached_at = now
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = DATA_DIR / "snapshot.json.tmp"
    tmp.write_text(json.dumps(snapshot), encoding="utf-8")
    tmp.replace(DATA_DIR / "snapshot.json")
    return snapshot


@app.route("/")
def index() -> Response:
    return Response((ASSETS_DIR / "index.html").read_text(encoding="utf-8"), mimetype="text/html")


@app.route("/api/snapshot")
def api_snapshot() -> Response:
    return jsonify(_get_snapshot())


@app.route("/api/system")
def api_system() -> Response:
    # Sampled fresh on every request (the monitor keeps its own rolling history),
    # so the activity graphs stay live without a background thread.
    with _cache_lock:
        return jsonify(_system_monitor.snapshot())


@app.route("/api/raw")
def api_raw() -> Response:
    """Return the raw transcript file for a line item, so the user can see the source."""
    path = request.args.get("path", "")
    resolved = Path(path).resolve()
    # Only serve transcript JSONL files under the host dir -- never arbitrary paths.
    host_root = Path(os.environ.get("MNGR_HOST_DIR", "/mngr")).resolve()
    if resolved.suffix != ".jsonl" or host_root not in resolved.parents:
        return Response('{"error": "Not a permitted transcript path."}', status=403, mimetype="application/json")
    if not resolved.exists():
        return Response('{"error": "Transcript not found."}', status=404, mimetype="application/json")
    return Response(resolved.read_text(encoding="utf-8"), mimetype="text/plain; charset=utf-8")


@app.route("/health")
def health() -> Response:
    return Response('{"status": "ok"}', mimetype="application/json")


def main() -> None:
    run_simple(
        "127.0.0.1", PORT, app, threaded=True, use_reloader=False, use_debugger=False
    )


if __name__ == "__main__":
    main()
