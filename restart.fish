#!/usr/bin/env fish

uv tool install . --reinstall --quiet
systemctl --user restart rcpilot
systemctl --user status rcpilot --no-pager
