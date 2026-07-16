---
title: Minds Pulse
description: A host-wide monitor for Minds -- live AI cost per agent and workspace priced from transcripts, plus system activity (CPU, memory and OOM, disk, services), with cost formulas and a confidence score.
thumbnail: inspiration-minds-pulse.svg
format: v1
---

# Minds Pulse

This file is the manifest for the **Minds Pulse** inspiration (slug:
`minds-pulse`). It is the one document a future agent reads to understand,
present, and adapt this inspiration. If you are an agent in a mind that was
created from this inspiration, this file is your script: read all of it, then
follow "How to adapt it" below.

## What it is

A host-wide monitor for Minds -- live AI cost per agent and workspace priced from transcripts, plus system activity (CPU, memory and OOM, disk, services), with cost formulas and a confidence score.

Minds Pulse is a host-wide monitor that runs as a web service in your
workspace (it opens as the `minds-pulse` tab). It answers two questions you
otherwise cannot see at a glance: "how much is the AI on this host actually
costing me, and who is spending it?" and "is this machine healthy right now?"
For cost, it reads every agent's Claude Code transcripts directly off the host,
attributes token usage to the agent and workspace that produced it, and prices
it from a mirrored copy of the mngr billing rates -- so you get live spend
broken down per agent and per workspace, with the exact cost formulas and a
confidence score shown rather than hidden. For system activity, it samples the
live machine -- CPU, memory (including the OOM shed log of processes the host
killed under memory pressure), disk usage, the supervisord service roster, and
the running agents -- refreshed on each request with a short rolling history.
The user sees a single dashboard: a vitals strip across the top and tabbed
panels (Cost, Memory, Compute, Storage, Services), styled in native Minds
colors with a "pulse" identity and a provisional build-vs-run spend projection.
It makes no external or LLM calls of its own; everything comes from reading
local host files and sampling the process table.

## How it works

The snapshot includes these paths (each is a repo-root-relative path copied
from the original mind onto a clean default-workspace-template base):

- `libs/minds_pulse`
- `supervisord.conf`
- `pyproject.toml`

- `libs/minds_pulse` is the whole application -- a Python library with a Flask
  service. Its pieces:
  - `collector.py` is the cost engine. It walks every agent's transcripts under
    `$MNGR_HOST_DIR/agents/*/plugin/claude/anthropic/projects/*.jsonl` (plus
    sub-agent and preserved transcripts), attributes each record to the agent
    and workspace by session id, and prices the token usage.
  - `pricing.py` holds the per-model rates, mirrored from
    `vendor/mngr/libs/mngr_usage/pricing.py` (the mngr billing table), and the
    formulas that turn token counts into dollar costs plus a confidence score.
  - `system_monitor.py` is the system engine. It samples CPU, memory, disk, the
    supervisord service roster, and the agent roster using `psutil`,
    `supervisorctl`, and `du`, and reads the OOM shed ledger at
    `runtime/oom_priority/events/shed.jsonl` for processes the host killed under
    memory pressure. It samples fresh per request and keeps a short rolling
    history.
  - `runner.py` is the Flask entrypoint. It serves `/` (the dashboard),
    `/api/snapshot` (the TTL-cached cost + system snapshot), `/api/system`
    (live system sample), `/api/raw` (the underlying raw records), and
    `/health`.
  - `assets/index.html` is the single-page frontend: a vitals strip plus tabbed
    panels (Cost, Memory, Compute, Storage, Services).
- `supervisord.conf` wires the service into the workspace. The
  `[program:minds-pulse]` block runs `uv run minds-pulse` behind
  `scripts/forward_port.py`, which registers it on port 8080 under the name
  `minds-pulse` so it appears as a workspace tab.
- `pyproject.toml` declares `minds-pulse` as a uv workspace member, source, and
  top-level dependency so `uv sync --all-packages` installs it and the
  `minds-pulse` console script resolves.

At runtime the flow is: supervisord starts the `minds-pulse` program ->
`forward_port.py` registers port 8080 -> Flask serves the dashboard, which
polls the `/api/*` endpoints -> `collector.py` and `system_monitor.py` read
local host files and the process table on demand.

## Prerequisites

Activation requirements: what the adopting agent must SET UP -- and must
INITIATE ITSELF during setup, before asking how to adapt -- for this
inspiration to run against the new user's own accounts/data. One line per
requirement, in this machine-readable form (greppable by `requires_`):

No prerequisites -- runs with no external permissions or secrets.

Minds Pulse makes no external or LLM calls: it only reads local host files
(agent transcripts, the OOM shed ledger, disk paths) and runs `psutil`,
`supervisorctl`, and `du` locally. There is nothing to authorize with latchkey
and no secret to configure. It does need the `psutil` package (declared in the
lib's own `pyproject.toml`, installed automatically by `uv sync --all-packages`)
and a Minds host layout -- a `/mngr` host directory (or `$MNGR_HOST_DIR`) with
agent transcripts present -- for the cost panels to have data to read.

## How to adapt it

Instructions for the NEXT agent -- the one adapting this inspiration into a
new mind. This is the `use-inspiration` skill's template path; in short:

1. Read this entire file first, especially "Prerequisites" and "Holes"
   below -- Prerequisites are your SETUP agenda, Holes are your ADAPTATION
   agenda.
2. Present the inspiration to the user in plain, non-technical language: what
   it is, what it does, and what it needs from them (name the Prerequisites).
3. Ask whether they want to use the same connectors (e.g. their own Slack).
   If YES: ACTIVATE FIRST -- initiate every `requires_permission` line NOW
   via a latchkey permission request (see the `latchkey` skill; the request
   opens the approval/login flow in the minds app), wire up any
   `requires_secret` values, start the services, and get the app showing
   THE USER'S OWN DATA. Done for a data-backed app means the user can open it
   and see their own data -- NOT that a service starts or an endpoint returns
   200. Then tell them it is live and to take a look.
4. Only AFTER that (or immediately, if they chose different connectors -- the
   swap is then the first adaptation) ask: "How do you want to adapt it?"
5. Work through each hole interactively, one at a time. Translate each into
   plain language, ask for a decision only when you genuinely need one, and
   resolve the obvious ones yourself.
6. When done, append a dated entry to "Adaptation history" below (never
   rewrite earlier entries) and commit.

## Holes

- **Pricing rates can drift.** `pricing.py` is a mirror of the mngr billing
  table (`vendor/mngr/libs/mngr_usage/pricing.py`). When mngr's rates change or
  new models appear, the mirror goes stale and costs will be off. A working
  replacement re-syncs the rate table from the current
  `vendor/mngr/libs/mngr_usage/pricing.py` (and ideally adds a check that flags
  when the two diverge).
- **Browser-automation API spend is not captured.** Real API calls made by the
  browser-automation lib are not yet counted in "billed API spend," so hosts
  that drive that lib will under-report. Closing this hole means teaching the
  collector where those calls are recorded and folding their cost into the
  totals.
- **Daily spend history and projection are provisional.** The build-vs-run
  spend projection is extrapolated from very little history until several days
  of data accrue, so early numbers are rough. No rewiring is needed -- it
  firms up on its own as history accumulates -- but the adapter should set
  expectations rather than present early projections as settled.
- **Per-process -> agent attribution is heuristic.** In the system panels,
  mapping OS processes back to specific agents is a best-effort heuristic and
  can misattribute. A working improvement ties processes to agents through a
  more reliable signal (e.g. the OOM-priority tagging the host already applies)
  rather than name/pid matching.

## Adaptation history

Each mind that adapts this inspiration appends one dated entry below. Earlier
entries are never rewritten.
