#!/usr/bin/env bash
set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 1. Read PORT from .env or default to 7861
PORT=7861
if [ -f "$DIR/.env" ]; then
    ENV_PORT=$(grep -E "^PORT=" "$DIR/.env" | cut -d= -f2 | tr -d ' "')
    if [ -n "$ENV_PORT" ]; then
        PORT="$ENV_PORT"
    fi
fi

# 2. Get the permanent Tailscale domain name
DOMAIN=$(tailscale status --json 2>/dev/null | grep -m 1 "DNSName" | cut -d'"' -f4 | sed 's/\.$//')
if [ -z "$DOMAIN" ]; then
    echo "Error: Tailscale does not appear to be running or connected."
    exit 1
fi

TARGET_URL="https://${DOMAIN}/hostedapi"

echo "============================================================"
echo " Starting Tailscale Expose for AGY2API"
echo " Local Target : http://127.0.0.1:${PORT}"
echo " Remote Mount : /hostedapi"
echo " Tailscale URL: ${TARGET_URL}"
echo "============================================================"

# 3. Configure Tailscale Serve (with sudo fallback if operator is not set)
echo "[1/3] Binding local port ${PORT} to ${TARGET_URL}..."
if ! tailscale serve --bg --set-path /hostedapi "http://127.0.0.1:${PORT}" 2>/dev/null; then
    echo "Note: Running with sudo to update Tailscale serve config..."
    sudo tailscale serve --bg --set-path /hostedapi "http://127.0.0.1:${PORT}"
fi

# 4. Wait briefly for local proxy binding
sleep 1

# 5. Test / Ping the endpoint
echo "[2/3] Pinging endpoint to verify connectivity..."
HTTP_CODE=$(curl -k -s -o /dev/null -w "%{http_code}" "${TARGET_URL}/" 2>/dev/null || echo "000")

echo "[3/3] Verification result:"
if [ "$HTTP_CODE" = "200" ] || [ "$HTTP_CODE" = "301" ] || [ "$HTTP_CODE" = "302" ] || [ "$HTTP_CODE" = "307" ] || [ "$HTTP_CODE" = "405" ]; then
    echo "  Status : ONLINE (HTTP Response Code: ${HTTP_CODE})"
    echo ""
    echo "SUCCESS: agy2api is live and accessible at:"
    echo "  --> ${TARGET_URL}"
else
    echo "  Status : Pending/Warning (HTTP Response Code: ${HTTP_CODE})"
    echo "  URL configured: ${TARGET_URL}"
fi

echo "============================================================"
echo "Useful Commands:"
echo "  - Make public to everyone on the internet : sudo tailscale funnel 443 on"
echo "  - Check active mappings                   : tailscale serve status"
echo "  - Stop exposing                           : sudo tailscale serve reset"
echo "============================================================"
