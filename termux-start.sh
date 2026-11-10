echo "Force syncing project code, discarding local changes..."
git fetch --all
git reset --hard origin/$(git rev-parse --abbrev-ref HEAD)
echo "Creating virtual environment..."
PYTHON_VERSION=$(python -V 2>&1 | grep -oP '\d+\.\d+' | head -1)
if [ -n "$PYTHON_VERSION" ]; then
    echo "Detected Python $PYTHON_VERSION, pinning version..."
    uv python pin "$PYTHON_VERSION"
fi
uv add -r requirements.txt
source .venv/bin/activate
pm2 start .venv/bin/python --name agy2api -- web.py
