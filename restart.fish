#!/usr/bin/env fish

set pid (ps aux | grep -E "uv run pilot|\.venv/bin/pilot" | grep -v grep | awk '{print $2}')
if test -n "$pid"
    echo "Killing PID(s): $pid"
    kill $pid
    sleep 1
end

echo "Starting pilot..."
uv run pilot
