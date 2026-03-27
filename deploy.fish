#!/usr/bin/env fish

set HOST "pi@rpi5"

echo "Upgrading rcpilot on $HOST ..."
ssh $HOST "uv tool upgrade rcpilot"

echo "Restarting service on $HOST ..."
ssh $HOST "systemctl --user restart rcpilot && systemctl --user status rcpilot --no-pager"

echo "Done."
