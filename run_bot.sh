#!/bin/bash
# Bot runner with enhanced debugging
set -e

echo "============================================================"
echo "Starting bot at $(date)"
echo "Current PID: $$"
echo "Home directory: $HOME"
echo "Current directory: $(pwd)"
echo "============================================================"

# Kill any existing Python bot processes
echo "Checking for existing bot processes..."
EXISTING_PIDS=$(pgrep -f "python3.*main.py" || true)

if [ -n "$EXISTING_PIDS" ]; then
    echo "Found existing bot process(es): $EXISTING_PIDS"
    echo "Killing existing processes..."
    pkill -f "python3.*main.py" || true
    sleep 2
    pkill -9 -f "python3.*main.py" 2>/dev/null || true
    echo "Existing processes terminated"
else
    echo "No existing bot processes found"
fi

# Clean up any leftover lock files
rm -f "$HOME/bot.lock" "$HOME/bot.pid" 2>/dev/null || true

# Check if virtual environment exists
if [ ! -d "$HOME/pwbvenv" ]; then
    echo "ERROR: Virtual environment not found at $HOME/pwbvenv"
    exit 1
fi

echo "Activating virtual environment..."
source $HOME/pwbvenv/bin/activate

# Verify activation
echo "Python version: $(python3 --version)"
echo "Python path: $(which python3)"
echo "Pip list:"
pip list

# Check if main.py exists
if [ ! -f "$HOME/main.py" ]; then
    echo "ERROR: main.py not found at $HOME/main.py"
    exit 1
fi

echo "============================================================"
echo "Starting new bot instance..."
echo "============================================================"

# Run with timeout and explicit error handling
cd $HOME
if timeout --kill-after=10s 3300s python3 -u $HOME/main.py 2>&1; then
    echo "Bot completed successfully at $(date)"
    exit 0
else
    EXIT_CODE=$?
    if [ $EXIT_CODE -eq 124 ]; then
        echo "Bot timed out after 55 minutes at $(date)"
    elif [ $EXIT_CODE -eq 137 ]; then
        echo "Bot was killed (SIGKILL) at $(date)"
    else
        echo "Bot failed with exit code $EXIT_CODE at $(date)"
    fi
    exit $EXIT_CODE
fi