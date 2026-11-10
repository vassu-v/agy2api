echo "Force syncing project code, discarding local changes..."
git fetch --all
git reset --hard origin/$(git rev-parse --abbrev-ref HEAD)
uv sync
source .venv/bin/activate
python web.py