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
- **YOLO mode** — one toggle to run sessions with `--dangerously-skip-permissions`
- **Usage window scheduler** — cron-based background job fires `claude -p "hi"` to start the 5-hour rolling usage window at configured times
- **Usage widget** — live progress bar in the header showing your 5-hour rate-limit window utilization, sourced from the built-in API proxy
- **Session naming** — auto-named by timestamp, rename anything inline
- **Git integration** — view diffs, pull, commit, and push without leaving the UI
- **Claude-powered commit** — Commit & Push auto-generates a commit message via `claude -p`; you review and confirm before anything is pushed
- **PR review** — trigger a full Claude code review on any open GitHub PR
- **Import project** — clone a GitHub repository directly from the UI into your projects directory
- **Project sort** — toggle between sort-by-last-modified and sort-by-name
- **Watchdog** — background process monitor marks crashed or timed-out sessions automatically
- **Sessions survive restarts** — the server can restart without killing active Claude sessions
- **Claude CLI auto-updater** — background job runs `claude update` on a cron schedule (default: 06:00 and 18:00); current version and last-updated time shown in the header with a manual trigger button
- **Anthropic API proxy** — transparent proxy between Claude Code and `api.anthropic.com` that captures rate-limit utilization, reset timestamps, and per-model call counts (no credentials exposed)

---

![rcpilot on mobile](docs/screenshot.png)

---

## Quick start

### One-shot demo (no install)

Try it immediately with no installation:

```bash
uvx rcpilot
```

Open **http://localhost:8000** in your browser. Config is auto-created at
`~/.config/rcpilot/config.toml` on first run.

### Persistent install

For a machine you want to run rcpilot on permanently:

```bash
uv tool install rcpilot
```

This puts `rcpilot` on your `PATH`. Run it manually:

```bash
rcpilot
```

Or set it up as a systemd service so it starts on boot and restarts on crash:

```bash
# Download the unit file
curl -o ~/.config/systemd/user/rcpilot.service \
  https://raw.githubusercontent.com/kjozsa/rcpilot/main/rcpilot.service

# Enable and start
systemctl --user enable --now rcpilot

# Check status / logs
systemctl --user status rcpilot
journalctl --user -u rcpilot -f
```

On a Pi, replace `localhost` with the Pi's IP or Tailscale hostname.

---

## The UI

The header shows:

- Current Claude CLI version and last-updated time
- Live 5-hour API usage widget (utilization bar + percentage)
- Next scheduled usage-window fire time
- Manual "update Claude now" button

Each project gets a card showing:

- Running sessions with their RC URL (tap to open in Claude Code)
- Start a new session (optionally named, optionally in YOLO mode)
- Kill a running session
- Git branch, diff stat, pull button, diff viewer, Claude-powered commit & push flow
- PR review launcher (posts results as a GitHub PR comment)
- Full session history with snapshot viewer and inline rename

The project list can be sorted by last-modified or by name. New projects can be imported (cloned) from GitHub without leaving the UI.

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
# Example: fire at 07:00, 12:00, and 17:00 every day
window_cron = "0 7,12,17 * * *"

# Claude CLI auto-updater — runs "claude update" on a cron schedule.
# Default: twice daily at 06:00 and 18:00. Set to "" to disable.
claude_update_cron = "0 6,18 * * *"
```

All fields are optional — the defaults above apply when the file is absent.

Both schedulers run embedded in the app (no system cron needed). The next scheduled
usage-window fire time is shown in the UI header. Supported cron syntax: `*`, `*/n`, `a-b`, `a,b,c`.

---

## Raspberry Pi + Tailscale setup

This is the intended deployment: a Pi that's always on, accessible from anywhere
over a private VPN.

### 1. Install uv

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### 2. Install rcpilot

```bash
uv tool install rcpilot
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
| 1.5 — Tooling | ✅ done | Usage proxy, Claude auto-updater, project import, sort, Claude-powered commit |
| 2 — Context memory | 🔜 next | Claude API summarization, resume with injected context |
| 3 — Polish | 💡 planned | Auth, PWA, PyPI publish, Docker, CI |

See [ROADMAP.md](ROADMAP.md) for details.

---

## Development

```bash
uv sync --extra dev
uv run pytest
uv run rcpilot
```

---

## License

MIT
