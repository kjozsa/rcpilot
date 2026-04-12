#!/usr/bin/env fish

set HOST "pi@rpi5"
set PROJECT_DIR "$HOME/pilot/projects/rcpilot"

# Detect if we're running on the server or remotely
set HOSTNAME (hostname)
if test "$HOSTNAME" = "rpi5"
    # Running locally on the server - deploy development version
    echo "Running locally on $HOSTNAME - deploying development version..."
    
    echo "Stashing any local changes..."
    git stash
    
    echo "Pulling latest code..."
    git pull
    
    echo "Syncing dependencies..."
    uv sync --frozen
    
    echo "Restarting service..."
    systemctl --user restart rcpilot
    systemctl --user status rcpilot --no-pager
else
    # Running remotely - deploy via SSH
    echo "Running remotely - deploying to $HOST..."
    
    echo "Stashing any local changes on $HOST..."
    ssh $HOST "cd $PROJECT_DIR && git stash"
    
    echo "Pulling latest code on $HOST..."
    ssh $HOST "cd $PROJECT_DIR && git pull"
    
    echo "Syncing dependencies on $HOST..."
    ssh $HOST "cd $PROJECT_DIR && uv sync --frozen"
    
    echo "Restarting service on $HOST..."
    ssh $HOST "systemctl --user restart rcpilot && systemctl --user status rcpilot --no-pager"
end

echo "Done."
