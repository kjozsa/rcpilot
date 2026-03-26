# claude-pilot

> **Claude Code RC is powerful but ephemeral. claude-pilot gives it a home.**

claude-pilot is a lightweight, self-hosted session manager for
[Claude Code Remote Control](https://docs.anthropic.com/en/docs/claude-code).
Run it on a Raspberry Pi (or any always-on machine) and get a mobile-friendly
web UI to launch, reconnect to, and manage your coding sessions from anywhere.

---

## Quick start

```bash
git clone <repo>
cd claude-pilot
uv run pilot
```

The server starts on **http://0.0.0.0:8000** by default.
Open that address in any browser on your local network (or VPN).

---

## Configuration

Create `~/.config/claude-pilot/config.toml` (or point `PILOT_CONFIG` at any path):

```toml
# Directory scanned for projects — one subdirectory = one project
projects_dir = "~/projects"

# Bind address; keep 0.0.0.0 for VPN / LAN access
host = "0.0.0.0"
port = 8000

# SQLite database path
db_path = "~/.config/claude-pilot/pilot.db"
```

All fields are optional — the defaults above apply when the file is absent.

---

## Raspberry Pi setup (headless)

### 1. Install uv

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### 2. Clone and install claude-pilot

```bash
git clone <repo>
cd claude-pilot
uv sync
```

### 3. Create your config

```bash
mkdir -p ~/.config/claude-pilot
cat > ~/.config/claude-pilot/config.toml <<'EOF'
projects_dir = "~/projects"
host = "0.0.0.0"
port = 8000
EOF
```

### 4. Run as a systemd service (optional but recommended)

```ini
# /etc/systemd/system/claude-pilot.service
[Unit]
Description=claude-pilot session manager
After=network.target

[Service]
ExecStart=/home/pi/claude-pilot/.venv/bin/pilot
User=pi
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now claude-pilot
```

---

## VPN access

claude-pilot has no authentication in v0 — it's designed to be accessed over a
trusted private network (e.g. Tailscale or WireGuard).

**Recommended setup:**
1. Install [Tailscale](https://tailscale.com) on your Pi and your phone/laptop.
2. Access claude-pilot at `http://<pi-tailscale-ip>:8000` from anywhere.
3. iOS Safari works fine — the UI is mobile-first with large tap targets.

Do **not** expose port 8000 directly to the public internet without adding
authentication (planned for Phase 3).

---

## Requirements

- Python 3.11+
- `script` (from `util-linux`) — standard on all Linux systems, no install needed
- `claude` — Claude Code CLI, must be on `PATH` and authenticated

---

## How sessions work

Each session runs `claude remote-control --spawn=session` inside the `script`
command, which owns the PTY independently of the pilot server process. This means:

- **Sessions survive pilot restarts** — stopping or restarting the server does
  not kill active Claude Code sessions.
- Session output is logged to `~/.config/claude-pilot/session-*.log` while
  running, then stored in SQLite when the session ends.

---

## Development

```bash
git clone <repo>
cd claude-pilot
uv sync --extra dev
uv run pytest
uv run pilot
```

---

## Roadmap

See [ROADMAP.md](ROADMAP.md) for the full plan.

| Phase | Status | Highlights |
|-------|--------|-----------|
| 0 — Foundation | ✅ done | Project discovery, web UI, session management |
| 1 — Persistence | ✅ done | SQLite history, watchdog, naming, concurrent sessions, restart-safe PTY via `script` |
| 2 — Context memory | 🔜 next | Claude API summarization, resume with context |
| 3 — Polish | 💡 planned | Docker, auth, PWA, PyPI publish |

---

## License

MIT
