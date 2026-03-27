#!/usr/bin/env fish

systemctl --user restart rcpilot
systemctl --user status rcpilot --no-pager
