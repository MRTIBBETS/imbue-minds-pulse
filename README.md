# Minds Pulse

A host-wide monitor for Minds -- live AI cost per agent and workspace priced from transcripts, plus system activity (CPU, memory and OOM, disk, services), with cost formulas and a confidence score.

Minds Pulse is a host-wide monitor that runs as a web dashboard in your Minds
workspace. It reads every agent's Claude Code transcripts off the host to price
live AI cost per agent and per workspace -- showing the cost formulas and a
confidence score, not just a number -- and samples the machine's live activity:
CPU, memory (with the OOM shed log), disk, supervisord services, and the agent
roster. Everything is local: it makes no external or LLM calls, needs no
credentials, and surfaces it all in one tabbed dashboard (Cost, Memory,
Compute, Storage, Services).

This repository is a published **minds inspiration**: a clean, bootable
snapshot of the apps and features a mind built, ready to adapt into your own.
It is NOT the generic workspace template -- it is this specific project.

## Use it

- **Create a new mind from it:** point a new minds workspace at this repo's
  URL. On first boot the mind reads the inspiration and helps you connect your
  own accounts and adapt it.
- **Bring it into an existing mind:** run `/use-inspiration <this repo's URL>`.

## What's inside

- **Minds Pulse** -- [`inspiration-minds-pulse.md`](inspiration-minds-pulse.md) (published now)

Each `inspiration-<slug>.md` is the full manifest for that inspiration: what
it is, how it works, the prerequisites it needs, and how to adapt it.
