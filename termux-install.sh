#!/bin/bash

# Avoid interactive prompts
export DEBIAN_FRONTEND=noninteractive

if [ "$(whoami)" = "root" ]; then
    echo "Root user detected, exiting..."
    exit
fi

echo "Checking Termux mirror configuration..."

# Check if current mirror is already Cloudflare mirror
target_mirror="https://packages-cf.termux.dev/apt/termux-main"
fallback_mirror="https://packages.termux.dev/apt/termux-main"
if [ -f "$PREFIX/etc/apt/sources.list" ] && grep -q "$target_mirror" "$PREFIX/etc/apt/sources.list"; then
    echo "✅ Mirror already configured to Cloudflare mirror, skipping modification"
else
    echo "Setting Termux mirror to Cloudflare mirror..."
    
    # Backup original sources.list file
    if [ -f "$PREFIX/etc/apt/sources.list" ]; then
        echo "Backing up original sources.list file..."
        cp "$PREFIX/etc/apt/sources.list" "$PREFIX/etc/apt/sources.list.backup.$(date +%s)"
    fi
    
    # Write new mirror source
    echo "Writing new mirror configuration..."
    cat > "$PREFIX/etc/apt/sources.list" << 'EOF'
# Cloudflare mirror source
deb https://packages-cf.termux.dev/apt/termux-main stable main
EOF
    
    echo "✅ Mirror updated to: $target_mirror"
fi

ensure_dpkg_ready() {
    echo "Checking and repairing dpkg/apt status..."
    # Wait for any running apt/dpkg processes to finish
    if pgrep -f "apt|dpkg" >/dev/null 2>&1; then
        echo "Detected apt/dpkg running, waiting for completion..."
        while pgrep -f "apt|dpkg" >/dev/null 2>&1; do sleep 1; done
    fi
    # Clean up stale locks (if no processes)
    for f in "$PREFIX/var/lib/dpkg/lock" \
             "$PREFIX/var/lib/apt/lists/lock" \
             "$PREFIX/var/cache/apt/archives/lock"; do
        [ -e "$f" ] && rm -f "$f"
    done
    # Attempt to continue unfinished configuration
    dpkg --configure -a || true
}

# Update package lists and check for errors
echo "Updating package lists..."
ensure_dpkg_ready
apt_output=$(apt update 2>&1)
if [ $? -ne 0 ]; then
    if echo "$apt_output" | grep -qi "is not signed"; then
        echo "⚠️ Repository not signed detected, trying fallback mirror and repairing keyring..."
        # Switch to fallback mirror
        sed -i "s#${target_mirror}#${fallback_mirror}#g" "$PREFIX/etc/apt/sources.list" || true
        # Clean lists and locks
        rm -rf "$PREFIX/var/lib/apt/lists/"* || true
        rm -f "$PREFIX/var/lib/dpkg/lock" "$PREFIX/var/lib/apt/lists/lock" "$PREFIX/var/cache/apt/archives/lock" || true
        # Reinstall termux-keyring
        apt-get install --reinstall -y termux-keyring || true
        # Update again
        ensure_dpkg_ready
        apt update
    else
        echo "apt update failed, error message:"
        echo "$apt_output" | head -20
        exit 1
    fi
else
    echo "$apt_output"
fi

echo "✅ Termux mirror configuration complete!"
echo "📁 Original configuration backed up to: $PREFIX/etc/apt/sources.list.backup.*"
echo "🔄 To restore original mirror, run:"
echo "   cp \$PREFIX/etc/apt/sources.list.backup.* \$PREFIX/etc/apt/sources.list && apt update"

# Check if package manager update and software installation are needed
need_update=false
packages_to_install=""

# Check if uv is installed
if ! command -v uv &> /dev/null; then
    need_update=true
    packages_to_install="$packages_to_install uv"
fi

# Check if python is installed
if ! command -v python &> /dev/null; then
    need_update=true
    packages_to_install="$packages_to_install python"
fi

# Check if nodejs is installed
if ! command -v node &> /dev/null; then
    need_update=true
    packages_to_install="$packages_to_install nodejs"
fi

# Check if git is installed
if ! command -v git &> /dev/null; then
    need_update=true
    packages_to_install="$packages_to_install git"
fi

# If packages need installing, update and install
if [ "$need_update" = true ]; then
    echo "Updating package manager..."
    ensure_dpkg_ready
    echo "Installing missing packages: $packages_to_install"
    pkg install $packages_to_install -y
else
    echo "All required packages are already installed, skipping update and install"
fi

# Check if pm2 is installed
if ! command -v pm2 &> /dev/null; then
    echo "Installing pm2..."
    npm install pm2 -g
else
    echo "pm2 is already installed, skipping installation"
fi

# Project directory handling logic
if [ -f "./web.py" ]; then
    # Already in target directory; skip clone and cd
    echo "Already in target directory, skipping clone operation"
elif [ -f "./agy2api/web.py" ]; then
    echo "Entering existing agy2api directory"
    cd ./agy2api
else
    echo "Cloning project repository..."
    git clone https://github.com/su-kaka/agy2api.git
    cd ./agy2api
fi

echo "Force syncing project code, discarding local changes..."
git fetch --all
git reset --hard origin/$(git rev-parse --abbrev-ref HEAD)

# Only create if not exists
if [ ! -d ".venv" ]; then
    echo "Creating virtual environment..."
    PYTHON_VERSION=$(python -V 2>&1 | grep -oP '\d+\.\d+' | head -1)
    if [ -n "$PYTHON_VERSION" ]; then
        echo "Detected Python $PYTHON_VERSION, pinning version..."
        uv python pin "$PYTHON_VERSION"
    fi
    rm pyproject.toml
    uv init
    uv venv
else
    echo "Virtual environment already exists, skipping creation"
fi

echo "Installing Python dependencies..."
uv add -r requirements.txt

echo "Activating virtual environment and starting service..."
source .venv/bin/activate
pm2 start .venv/bin/python --name web -- web.py
cd ..