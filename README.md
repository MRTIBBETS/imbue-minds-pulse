# Minds Pulse

*A host-wide monitor for a [Minds](https://minds.com) workspace — what your AI is costing you, and what your machine is doing, in one live dashboard.*

Minds Pulse answers a simple question that turns out to be surprisingly hard: **"how much is this actually costing me, and is that number even right?"** It runs as a web tab inside your Minds workspace and, in near real time, shows:

- **Cost** — live AI spend, broken down per agent and per workspace, priced straight from each agent's transcripts. It shows the *formula* and a *confidence score*, not just a dollar figure.
- **Memory** — usage, pressure, top processes, and the OOM shed log (what the kernel killed under pressure — the usual cause of a mysterious crash).
- **Compute** — load per core, CPU over time, top processes.
- **Storage** — disk usage and what's actually growing (transcripts, git history, …).
- **Services & agents** — every background service with live CPU/RAM, plus the agent roster.

A permanent **vitals strip** across the top gives you the one-glance summary; the depth lives behind tabs so it stays uncluttered.

Everything is **local and read-only**: Minds Pulse makes no external or LLM calls and needs no credentials. It just reads files on the host and samples the process table.

---

## The thinking behind it

This started as a plain request — *"track my AI usage across Minds and predict what it's costing me, in real time."* Getting to a number you can trust took a few turns of thought, and those turns are baked into the design:

**1. The data already existed.** Every Minds agent writes a Claude Code transcript that records exact per-message token usage. So this isn't sampling or estimating usage — it reads the real counts and prices them with the same rate table Minds itself bills against. The dashboard surfaces those transcripts as the source, so nothing is a black box.

**2. Accuracy had to be earned, not assumed.** An early version quietly *undercounted* by ~7% because it priced all prompt-cache writes at the cheap 5-minute rate, when 1-hour cache writes cost double. That's fixed — cache writes are now split by TTL and priced correctly. To keep the numbers honest going forward, the dashboard shows a **confidence score** and states plainly what is *measured* (the tokens) versus *estimated* (their dollar value).

**3. "Cost" needed honest framing.** Most Minds usage runs on a subscription, so the dollar figure is **API-equivalent value** — *what you're consuming*, not a bill. Billed pay-per-token spend is tracked separately. The dashboard says which is which, so a big number is never mistaken for an invoice.

**4. The scariest number was the most misleading.** A naive "30-day projection" extrapolates today's spend across a month — but building something in Minds is **front-loaded**: the expensive part is the agent work of *creating* a tool, while *running* what you built costs ~$0 (the apps make no model calls). So the projection is framed as a *provisional pace* with a **"build vs. run"** explanation, not a fixed monthly bill.

**5. Cost alone wasn't the whole picture.** A Minds workspace is a small container where **memory is the real constraint** — and Minds will shed (kill) processes under memory pressure. So the tool grew from a cost tracker into a full monitor (hence *Pulse*), modeled on macOS Activity Monitor but kept deliberately uncluttered: one always-on vitals strip, everything else one tab away.

**6. It should feel like part of Minds.** The design uses the native Minds palette and type, a terminal-but-friendly texture, and a single quiet "pulse" mark as its identity — comprehensive views without the clutter.

The durable principles, if you adapt this: **show the math, not just the number; separate measured from estimated; frame cost as a pace, not a bill; and stay comprehensive without becoming cluttered.**

---

## How it works

- `libs/minds_pulse/collector.py` reads agent transcripts under the host dir, attributes usage by session id (sub-agents included), and prices it with `pricing.py` (rates mirrored from Minds' billing table).
- `libs/minds_pulse/system_monitor.py` samples CPU/memory/disk via `psutil`, services via `supervisorctl`, and the OOM shed ledger — fresh on each request, with a short rolling history for the graphs.
- `libs/minds_pulse/runner.py` is a small Flask app serving the dashboard and its JSON endpoints; the whole UI is a single `assets/index.html`.

---

## Use it

- **Create a new mind from it:** point a new Minds workspace at this repo's URL. On first boot the mind reads the inspiration and helps you adapt it.
- **Bring it into an existing mind:** run `/use-inspiration <this repo's URL>`.

## What's inside

- **Minds Pulse** — [`inspiration-minds-pulse.md`](inspiration-minds-pulse.md) — the full manifest: what it is, how it works, prerequisites (none — it needs no credentials), and notes for adapting it.

This repository is a published **minds inspiration**: a clean, bootable snapshot of a feature a mind built, ready to adapt into your own. It is not the generic workspace template — it is this specific project.

---

## Honest limitations

- **Rates can drift.** Pricing is mirrored from Minds' billing table as of publish; re-sync if prices change.
- **Direct-API spend isn't captured yet.** Anything that calls a metered pay-per-token API (e.g. browser automation) isn't yet folded into "billed API spend."
- **The projection is provisional early on.** With only a day or two of history it can't tell a build sprint from steady-state use; it says so.
- **Per-process → agent attribution is heuristic.** Agents share a process tree, so the system panels attribute best-effort.
