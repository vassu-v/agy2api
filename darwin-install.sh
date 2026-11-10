#!/bin/bash
# macOS installation script (Supports Intel and Apple Silicon)

# Ensure Homebrew is installed
if ! command -v brew &> /dev/null; then
    echo "Homebrew not detected, starting installation..."
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    
    # Detect Homebrew install path and set environment variables
    if [[ -f "/opt/homebrew/bin/brew" ]]; then
        # Apple Silicon Mac
        eval "$(/opt/homebrew/bin/brew shellenv)"
    elif [[ -f "/usr/local/bin/brew" ]]; then
        # Intel Mac
        eval "$(/usr/local/bin/brew shellenv)"
    fi
fi

# Update brew and install git
brew update
brew install git

# Install uv (Python environment management tool)
curl -Ls https://astral.sh/uv/install.sh | sh

# Ensure uv is in PATH
export PATH="$HOME/.local/bin:$PATH"

# Clone or enter project directory
if [ -f "./web.py" ]; then
    # Already in target directory
    :
elif [ -f "./agy2api/web.py" ]; then
    cd ./agy2api
else
    git clone https://github.com/su-kaka/agy2api.git
    cd ./agy2api
fi

# Pull latest code
git pull

# Create and sync virtual environment
uv sync

# Activate virtual environment
if [ -f ".venv/bin/activate" ]; then
    source .venv/bin/activate
else
    echo "❌ Virtual environment not found, please check if uv was installed successfully"
    exit 1
fi

# Start project
python3 web.py
