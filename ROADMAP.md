# claude-pilot Roadmap

> **Claude Code RC is powerful but ephemeral. claude-pilot gives it a home.**

claude-pilot is a lightweight, self-hosted session manager for Claude Code Remote Control. It runs on a Raspberry Pi (or any always-on machine), gives you a mobile-friendly web UI to launch and reconnect to coding sessions, and adds the persistence and context continuity that RC currently lacks.

**Design principles:**
- Local-first, privacy-respecting — your code never leaves your machine
- Pi-friendly — low resource footprint, runs headlessly
- Zero-friction deploy — one command via `uv`
- Open, hackable — FastAPI backend, plain Python, no magic

---

## Phase 0 — Foundation ✦ *current focus*

The skeleton. Enough to be useful on day one.

- [ ] FastAPI app scaffold, deployable via `uvx claude-pilot` or `uv tool install claude-pilot`
- [ ] Configurable projects directory (via `config.toml` or env vars)
- [ ] Project discovery — scans directory, lists projects in web UI
- [ ] Spawn `claude remote-control` for a selected project into a `tmux` session
- [ ] Capture RC session URL from process stdout, surface it in the UI
- [ ] Basic session status per project — running / stopped / timed out
- [ ] Kill and restart a session from the UI
- [ ] Mobile-friendly web UI (accessed via VPN, no auth needed for v0)
- [ ] README with Pi setup guide and VPN access notes

---

## Phase 1 — Persistence & Continuity

The thing RC fundamentally lacks: memory across sessions.

- [ ] SQLite store for session history per project (start time, duration, status)
- [ ] Session log capture — store user prompts and Claude responses per session
- [ ] Session list view per project — see all past sessions with timestamps
- [ ] "Resume" button — relaunch RC with last session metadata pre-loaded
- [ ] Auto-restart watchdog — detect RC timeout/crash, relaunch automatically
- [ ] Session naming — auto-name sessions by date, allow manual rename

---

## Phase 2 — Context Memory via Claude API

Where it gets smart. Uses the Claude API to make resuming sessions actually meaningful.

- [ ] On session end, auto-summarize the session log via Claude API
- [ ] Store summary as project-scoped memory (`project/.pilot/memory.md`)
- [ ] On session resume, inject summary as context pre-prompt to Claude Code
- [ ] Manual project notes per project — persistent instructions Claude always sees
  - e.g. *"don't touch the legacy parser"*, *"always use async patterns"*
- [ ] "What was I doing?" endpoint — ask Claude to summarize across recent sessions
- [ ] Configurable: auto-summarize on session end vs. manual trigger (token cost tradeoff)

---

## Phase 3 — Polish & Shareability

Makes this something others can actually adopt beyond early adopters.

- [ ] Docker / docker-compose for easy self-hosting beyond Pi
- [ ] Optional simple auth — single shared token in config (for users not on VPN)
- [ ] PWA manifest — installable on mobile home screen, feels native
- [ ] Dark mode (default) / light mode toggle
- [ ] Setup script for Raspberry Pi OS (headless install guide)
- [ ] Published to PyPI for `uvx` one-liner install
- [ ] GitHub Actions CI — lint, test, publish
- [ ] Contributing guide and good-first-issue labels

---

## Backlog / Ideas (unscheduled)

Things worth considering but not yet committed to a phase:

- Multi-user support (separate session namespaces)
- Git integration — show current branch and last commit per project
- Webhook on session end (e.g. ping a Slack or ntfy notification)
- `--spawn` server mode support for parallel sessions per project
- Session diff view — what files changed during a session
- MCP server config per project, auto-loaded when session starts
- REST API for claude-pilot itself (enable other tooling to integrate)
- Desktop companion app (stretch goal)

---

## Tech Stack

| Layer | Choice | Rationale |
|---|---|---|
| Backend | Python + FastAPI | Async, lightweight, great DX |
| Deploy | `uv` / `uvx` | Zero-friction install, modern Python packaging |
| Process mgmt | `tmux` + `asyncio.subprocess` | Inspectable via SSH, Pi-friendly |
| Persistence | SQLite via `aiosqlite` | No server, file-based, Pi-appropriate |
| AI features | Anthropic Python SDK | Claude API for summarization + context injection |
| Frontend | Vanilla HTML/CSS/JS (served by FastAPI) | No build step, minimal footprint |

---

## Non-Goals

To keep claude-pilot focused:

- **Not a cloud product** — claude-pilot assumes your machine is always on; cloud hosting is out of scope
- **Not a full IDE** — the coding happens in RC; claude-pilot is the launcher and memory layer
- **Not multi-tenant** — designed for a single developer's personal setup
- **Not a RC replacement** — claude-pilot wraps RC, it doesn't reimplement it

---

## Contributing

Issues and PRs welcome. Check the issue tracker for items tagged `good-first-issue`.

If you're running this on something other than a Pi (NAS, old laptop, VPS), notes on your setup are very welcome in Discussions.

