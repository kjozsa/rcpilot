#!/usr/bin/env fish

set pid (ps aux | grep -E "uv run rcpilot|\.venv/bin/rcpilot" | grep -v grep | awk '{print $2}')
if test -n "$pid"
    echo "Killing PID(s): $pid"
    kill $pid
    sleep 1
end

echo "Starting rcpilot..."
uv run rcpilot > /dev/null 2>&1 &
disown
