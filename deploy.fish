#!/usr/bin/env fish

set HOST "pi@rpi5"
set REMOTE_DIR "/srv/pilot/claude-pilot"

echo "Deploying to $HOST:$REMOTE_DIR ..."

rsync -av \
    --exclude=".venv" \
    --exclude="__pycache__" \
    --exclude="*.pyc" \
    --exclude=".git" \
    --exclude="*.db" \
    --exclude="*.log" \
    (pwd)/ $HOST:$REMOTE_DIR/

echo "Reinstalling package on Pi ..."
ssh $HOST "cd $REMOTE_DIR && uv pip install -e ."

echo "Done. Run 'pilot' on the Pi to start."
