# rcpilot

> **Claude Code RC is powerful but ephemeral. rcpilot gives it a home.**

rcpilot is a lightweight, self-hosted session manager for
[Claude Code Remote Control](https://docs.anthropic.com/en/docs/claude-code).
Run it on a Raspberry Pi (or any always-on machine) and get a polished,
mobile-friendly web UI to launch, manage, and reconnect to your coding sessions
from anywhere — phone, tablet, or browser.

---

## What it does

Claude Code's Remote Control mode gives you a shareable session URL. That's great,
but it has no persistence, no history, and no management layer. rcpilot wraps
it with everything that's missing:

- **Launch sessions** from any device via a clean web UI
- **Reconnect** to running sessions — RC URLs are stored and always one tap away
- **Full session history** per project — every session logged with start time, duration, and status
- **Terminal snapshots** — captured on session end and browsable any time
- **Multiple concurrent sessions** per project, all tracked independently
- **YOLO mode** — one toggle to run sessions with `--permission-mode bypassPermissions`
- **Usage window scheduler** — cron-based background job fires `claude -p "hi"` to start the 5-hour rolling usage window at configured times
- **Session naming** — auto-named by timestamp, rename anything inline
- **Git integration** — view diffs, pull, commit, and push without leaving the UI
- **PR review** — trigger a full Claude code review on any open GitHub PR
- **Watchdog** — background process monitor marks crashed or timed-out sessions automatically
- **Sessions survive restarts** — the server can restart without killing active Claude sessions

---

![rcpilot on mobile](docs/screenshot.png)

---

## Quick start

```bash
git clone <repo>
cd rcpilot
uv run pilot
```

Open **http://localhost:8000** in any browser. On a Pi, replace `localhost` with
the Pi's IP or Tailscale hostname.

---

## The UI

Each project gets a card showing:

- Running sessions with their RC URL (tap to open in Claude Code)
- Start a new session (optionally named, optionally in YOLO mode)
- Kill a running session
- Git branch, diff stat, pull button, diff viewer, commit & push flow
- PR review launcher
- Full session history with snapshot viewer and inline rename

The UI is mobile-first with large tap targets — works well on iOS Safari and Android Chrome.
No app install needed.

---

## Sessions survive restarts

Each session runs `claude remote-control --spawn=session` inside `script`, which
holds the PTY independently of the pilot server process. This means:

- Restarting or crashing rcpilot does **not** kill active Claude Code sessions
- Sessions persist across Pi reboots — if the RC process is still running, pilot reconnects to it
- Session log files are written continuously to `~/.config/rcpilot/` and captured as snapshots on end

---

## Configuration

Create `~/.config/rcpilot/config.toml` (or point `PILOT_CONFIG` at any path):

```toml
# Directory scanned for projects — each immediate subdirectory becomes a project
projects_dir = "~/projects"

# Bind address; keep 0.0.0.0 for VPN / LAN access
host = "0.0.0.0"
port = 8000

# SQLite database path
db_path = "~/.config/rcpilot/pilot.db"

# Usage window scheduler — fires "claude -p hi" on a cron schedule to start
# the 5-hour rolling usage window (Pro/Max plans). Standard 5-field cron syntax.
# Example: fire at 07:00 and 12:00 every day
window_cron = "0 7,12 * * *"
```

All fields are optional — the defaults above apply when the file is absent.

The scheduler runs embedded in the app (no system cron needed). The next scheduled
fire time is shown in the UI header. Supported cron syntax: `*`, `*/n`, `a-b`, `a,b,c`.

---

## Raspberry Pi + Tailscale setup

This is the intended deployment: a Pi that's always on, accessible from anywhere
over a private VPN.

### 1. Install uv

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### 2. Clone and install

```bash
git clone <repo>
cd rcpilot
uv sync
```

### 3. Configure

```bash
mkdir -p ~/.config/rcpilot
cat > ~/.config/rcpilot/config.toml <<'EOF'
projects_dir = "~/projects"
host = "0.0.0.0"
port = 8000
EOF
```

### 4. Install Tailscale

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

Then install Tailscale on your phone or laptop. Access rcpilot at
`http://<pi-tailscale-hostname>:8000` from anywhere with no port forwarding needed.

iOS Safari works great — the UI is designed for it.

---

## Security note

rcpilot has no authentication in v0. It is designed to run on a trusted
private network (Tailscale, WireGuard, or local LAN only).

**Do not expose port 8000 to the public internet.**

Simple token-based auth is planned for Phase 3.

---

## Requirements

- Python 3.11+
- `script` (from `util-linux`) — standard on all Linux distros, no install needed
- `claude` — Claude Code CLI, must be on `PATH` and authenticated
- `gh` — GitHub CLI, only needed for PR review feature

---

## Roadmap

| Phase | Status | Focus |
|-------|--------|-------|
| 0 — Foundation | ✅ done | Project discovery, web UI, session spawning |
| 1 — Persistence | ✅ done | Session history, snapshots, watchdog, git & PR integration |
| 2 — Context memory | 🔜 next | Claude API summarization, resume with injected context |
| 3 — Polish | 💡 planned | Auth, PWA, PyPI publish, Docker, CI |

See [ROADMAP.md](ROADMAP.md) for details.

---

## Development

```bash
uv sync --extra dev
uv run pytest
uv run pilot
```

---

## License

MIT
