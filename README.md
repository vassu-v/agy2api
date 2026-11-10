# AGY2API

**Convert Google Antigravity to OpenAI, Anthropic Claude, and Gemini API Compatible Interfaces**

[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![License: CNC-1.0](https://img.shields.io/badge/License-CNC--1.0-red.svg)](LICENSE)
[![Docker](https://img.shields.io/badge/docker-available-blue.svg)](https://github.com/vassu-v/agy2api/pkgs/container/agy2api)

> **⚠️ Important Notice**: The legacy GeminiCLI endpoint has been deprecated by Google. All GeminiCLI-specific code has been completely removed; only the Antigravity proxy engine is active and maintained.

---

## 🌟 Fork Enhancements & Modifications

This repository is an enhanced, security-audited, and 100% localized fork of `agy2api`. The following key features, architectural improvements, and security enhancements have been added over upstream:

1. **A-to-Z English Localization & Codebase Cleanup**:
   - Every file across backend Python modules (`src/`, `config.py`, `web.py`, `log.py`), React frontend, CLI scripts (`.sh`, `.bat`, `.ps1`), GitHub issue templates, and documentation has been translated into clean, idiomatic English.
   - Cleaned up Chinese locale hardcodings (e.g., `zh-CN` timestamp parsing) and fullwidth punctuation artifacts.
2. **Network Listen Interface Control (`HOST`)**:
   - Easily toggle whether the server listens on `0.0.0.0` (all network interfaces: LAN, Wi-Fi, Tailscale, remote access) or `127.0.0.1` (local machine loopback only for maximum privacy).
   - Dynamically configurable from the **Web Dashboard** under **Settings** without requiring manual config file edits, with fallback to the `HOST` environment variable.
3. **Model Serving & Visibility Controls (`VISIBLE_MODELS`)**:
   - Filter and whitelist specific models exposed to clients via the `/v1/models` catalog endpoint.
   - Protects AI coding agents (such as Kimi CLI, Claude Code, Cursor, OpenCode) from getting overwhelmed by unneeded internal models.
4. **Dynamic Model Name Rewriting & Serving Aliases (`MODEL_REWRITE_RULES`)**:
   - Define custom regex rewrite rules or use 1-click presets in the Web Dashboard to map external model identifiers directly to upstream models (e.g., `gpt-4o` ➔ `gemini-2.5-pro`, `gpt-4o-mini` ➔ `gemini-2.5-flash`, `claude-3-7-sonnet` ➔ `claude-sonnet-4-6`).
   - Declared alias models automatically register in `/v1/models` so client auto-discovery tools can discover and select them instantly.
5. **Security Hardening & Privacy Verification**:
   - **Zero Data Leaks:** Audited to guarantee zero tracking, user analytics, external CDNs, or third-party pixels.
   - **Third-Party Mirror Presets Removed:** Purged upstream presets pointing to external Chinese reverse proxies (`sukaka.top`) from the dashboard, enforcing that 100% of OAuth credential exchanges and LLM prompt payloads travel strictly and directly to official Google endpoints (`*.googleapis.com`).
6. **Cross-Platform Script Support**:
   - Fully translated and verified Windows PowerShell installer (`install.ps1`) and batch runner (`start.bat`), alongside macOS (`darwin-install.sh`), Linux (`install.sh`), and Android (`termux-install.sh`).

---

## 🏗️ Architecture & Core Request Flow

```
[Client Applications: Kimi / Claude Code / Cursor / OpenCode / OpenAI SDK]
                                  │
                                  ▼
      ┌────────────────────────────────────────────────────────┐
      │                 AGY2API PROXY GATEWAY                  │
      ├────────────────────────────────────────────────────────┤
      │ 1. Network Interface Binding (127.0.0.1 vs 0.0.0.0)    │
      │ 2. ASGI IP Whitelist Middleware (Optional CIDR/IPs)    │
      │ 3. Model Rewrite & Alias Engine (e.g. gpt-4o ➔ pro)    │
      │ 4. Model Catalog Filter (Exposes whitelisted models)   │
      │ 5. Session Cache Affinity (Reuses token for cache hits)│
      │ 6. Multi-Account Pool & Per-Family 429 Cooldown Tracker│
      │ 7. Anti-Truncation Continuation Engine ('autostitch-') │
      └────────────────────────────────────────────────────────┘
                                  │
                                  ▼ (HTTPS strictly to official Google APIs)
      ┌────────────────────────────────────────────────────────┐
      │        Google Cloud Code Backend (googleapis.com)      │
      └────────────────────────────────────────────────────────┘
```

### Impact on Core Flow:
* **Zero Breaking Changes**: Core proxying logic, SSE real-time streaming throughput, token estimation, and Google Antigravity communication protocols remain 100% intact with zero added latency.
* **Strict HTTPS Privacy**: All payload transmissions bypass third parties and communicate directly with Google's Cloud Code APIs.

---

## ⚡ Quick Start & Installation

### Termux (Android)

**Initial Installation:**
```bash
curl -o termux-install.sh "https://raw.githubusercontent.com/vassu-v/agy2api/refs/heads/master/termux-install.sh" && chmod +x termux-install.sh && ./termux-install.sh
```

**Restart Service:**
```bash
cd agy2api
bash termux-start.sh
```

---

### Windows

**Initial Installation:**
```powershell
iex (iwr "https://raw.githubusercontent.com/vassu-v/agy2api/refs/heads/master/install.ps1" -UseBasicParsing).Content
```

**Restart Service:**
Double-click `start.bat` or run:
```powershell
.\start.bat
```

---

### Linux

**Initial Installation:**
```bash
curl -o install.sh "https://raw.githubusercontent.com/vassu-v/agy2api/refs/heads/master/install.sh" && chmod +x install.sh && ./install.sh
```

**Restart Service:**
```bash
cd agy2api
bash start.sh
```

---

### macOS

**Initial Installation:**
```bash
curl -o darwin-install.sh "https://raw.githubusercontent.com/vassu-v/agy2api/refs/heads/master/darwin-install.sh" && chmod +x darwin-install.sh && ./darwin-install.sh
```

**Restart Service:**
```bash
cd agy2api
bash start.sh
```

---

### Docker & Docker Compose

**Direct Docker Run:**
```bash
docker run -d \
  --name agy2api \
  --network host \
  -e API_PASSWORD=your_api_password \
  -e PANEL_PASSWORD=your_panel_password \
  -e PORT=7861 \
  -v $(pwd)/data/creds:/app/creds \
  -v $(pwd)/data/request_logs:/app/data/request_logs \
  ghcr.io/vassu-v/agy2api:latest
```

**Docker (macOS / Windows without host networking):**
```bash
docker run -d \
  --name agy2api \
  -p 7861:7861 \
  -p 8080:8080 \
  -e API_PASSWORD=your_api_password \
  -e PANEL_PASSWORD=your_panel_password \
  -e PORT=7861 \
  -v "$(pwd)/data/creds":/app/creds \
  -v "$(pwd)/data/request_logs":/app/data/request_logs \
  ghcr.io/vassu-v/agy2api:latest
```

**Docker Compose:**
Save the following as `docker-compose.yml`:
```yaml
version: '3.8'

services:
  agy2api:
    image: ghcr.io/vassu-v/agy2api:latest
    container_name: agy2api
    restart: unless-stopped
    network_mode: host
    environment:
      - API_PASSWORD=your_api_password
      - PANEL_PASSWORD=your_panel_password
      - PORT=7861
      - HOST=0.0.0.0
    volumes:
      - ./data/creds:/app/creds
      - ./data/request_logs:/app/data/request_logs
    healthcheck:
      test: ["CMD-SHELL", "python -c \"import sys, urllib.request, os; port = os.environ.get('PORT', '7861'); req = urllib.request.Request(f'http://localhost:{port}/v1/models', headers={'Authorization': 'Bearer ' + os.environ.get('API_PASSWORD', 'pwd')}); sys.exit(0 if urllib.request.urlopen(req, timeout=5).getcode() == 200 else 1)\""]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 40s
```

Launch with:
```bash
docker-compose up -d
```

---

## 🚀 Core Features

### 🔄 Multi-Format API Translation
* **OpenAI Endpoints**: `/v1/chat/completions` and `/v1/models`
  * Standard OpenAI messages structure (`system`, `user`, `assistant`, `tool`).
  * Multimodal input support (text, base64 images, and URL images).
  * Thinking process separation via `reasoning_content`.
* **Anthropic Claude Endpoints**: `/v1/messages`
  * Native Claude format specification (`anthropic-version: 2023-06-01`).
  * Supports `system` instructions, tool calling, and streaming events (`content_block_delta`).
* **Google Gemini Native Endpoints**: `/v1/models/{model}:generateContent` and `streamGenerateContent`
  * Fully compatible with official Google GenAI SDKs and REST formats.
  * Supports `contents`, `generationConfig`, and image generation parameters.

### 🌐 Network Listen Interface Control
* Choose where the proxy binds:
  * `0.0.0.0` — Listen on all network interfaces (LAN, Wi-Fi, Tailscale, VPN, remote devices).
  * `127.0.0.1` — Listen on local loopback only (secure, accessible only on the current machine).
* Configurable via the **Web Dashboard** under **Settings** or via the `HOST` environment variable.

### 🎯 Model Serving Whitelist & Visibility
* Control exactly which models are advertised to clients via `/v1/models`.
* In the Web Dashboard or via `VISIBLE_MODELS`, provide a whitelist of allowed model identifiers.
* Protect your client tools from being overwhelmed by models you do not use.

### 🔀 Dynamic Model Rewriting & Serving Aliases
* Map incoming model names from external tools to target upstream models:
  * Example: Map `gpt-4o` ➔ `gemini-2.5-pro`
  * Example: Map `gpt-4o-mini` ➔ `gemini-2.5-flash`
  * Example: Map `claude-3-7-sonnet` ➔ `claude-sonnet-4-6`
  * Example: Map `claude-3-5-sonnet` ➔ `claude-sonnet-4-5`
* Configurable via the **Web Dashboard** with a single click ("+ Add Common Aliases") or via custom regex rules (`MODEL_REWRITE_RULES`).
* Aliased models are automatically appended to `/v1/models` so client auto-discovery tools (e.g. Cursor, Kimi, OpenCode) can discover them immediately.

### 📊 Intelligent Multi-Account Pool & Cooldowns
* Load-balances across multiple Google OAuth accounts automatically.
* Unique email indexing ensures importing the same account refreshes credentials without duplication.
* Per-family 429 rate-limit tracking: puts only the throttled model family (argon / gemini / claude) into cooldown while keeping other accounts and models active.
* Prompt-cache affinity (`CREDENTIAL_SELECT_MODE=cache`): Hashes conversation keys so repeated prompts hit Google's prompt cache on the same credential.

### 🛡️ Streaming Anti-Truncation (`autostitch-`)
* Google Antigravity tokens occasionally truncate output length.
* Prefix any model name with `autostitch-` (e.g. `autostitch-gemini-2.5-pro`) to automatically continue generating until completion across tool calls and streaming responses.

### 🎛️ Modern Web Management Dashboard
* Built with React, Vite, and Tailwind CSS.
* One-click Google OAuth login flow.
* Credential management (upload JSON/ZIP, view health status, toggle active state, export).
* Real-time request inspection and debugging log viewer.
* Live hot-updating settings without restarting the service.

---

## 🤖 Supported Models

Models are dynamically retrieved from the upstream Antigravity engine and include:

| Model Family | Popular Model Identifiers | Description |
| :--- | :--- | :--- |
| **Claude** | `claude-sonnet-4-5`, `claude-opus-4-5`, `claude-sonnet-4-6` | Anthropic flagship models routed through Antigravity |
| **Gemini Pro** | `gemini-2.5-pro`, `gemini-3-pro-preview`, `gemini-3.1-pro-preview` | High-capability reasoning and long-context processing (up to 1M tokens) |
| **Gemini Flash**| `gemini-2.5-flash`, `gemini-3-flash-preview` | Ultra-fast responses with deep multimodal reasoning |
| **Image Gen** | `gemini-3.1-flash-image` | Supports aspect ratios (`-16x9`, `-9x16`, `-1x1`, `-21x9`) & resolutions (`-2k`, `-4k`) |
| **Anti-Truncation** | `autostitch-<model-name>` | Automatically stitches truncated responses together |

---

## 🛠️ Client Integration Guides

### 1. OpenAI SDK (Python)

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:7861/v1",
    api_key="your_api_password"  # Configured via API_PASSWORD (default: pwd)
)

response = client.chat.completions.create(
    model="gemini-2.5-pro",
    messages=[
        {"role": "system", "content": "You are a helpful coding assistant."},
        {"role": "user", "content": "Explain async/await in Python with an example."}
    ],
    stream=True
)

for chunk in response:
    content = chunk.choices[0].delta.content or ""
    print(content, end="", flush=True)
```

---

### 2. Anthropic Claude SDK / Claude Code (CLI)

**Using `curl`:**
```bash
curl -X POST "http://127.0.0.1:7861/v1/messages" \
  -H "x-api-key: your_api_password" \
  -H "anthropic-version: 2023-06-01" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "claude-sonnet-4-5",
    "max_tokens": 4096,
    "messages": [
      {"role": "user", "content": "Hello Claude!"}
    ]
  }'
```

**Using Claude Code / DSH (`cordis.patch.yml`):**
```yaml
env:
  ANTHROPIC_BASE_URL: "http://127.0.0.1:7861"
  ANTHROPIC_API_KEY: "pwd"
```

---

### 3. Google GenAI SDK (Gemini Native)

```python
from google.genai import Client
from google.genai.types import HttpOptions

client = Client(
    api_key="your_api_password",
    http_options=HttpOptions(base_url="http://127.0.0.1:7861")
)

response = client.models.generate_content(
    model="gemini-2.5-flash",
    contents="Hello Gemini!"
)
print(response.text)
```

**Image Generation (`gemini-3.1-flash-image`):**
```python
from io import BytesIO
from PIL import Image
from google.genai import Client
from google.genai.types import HttpOptions, GenerateContentConfig, ImageConfig

client = Client(
    api_key="your_api_password",
    http_options=HttpOptions(base_url="http://127.0.0.1:7861")
)

response = client.models.generate_content(
    model="gemini-3.1-flash-image-4k-16x9",
    contents=["A futuristic cyberpunk cityscape at sunset with flying vehicles"],
    config=GenerateContentConfig(
        image_config=ImageConfig(aspect_ratio="16:9")
    )
)

for part in response.candidates[0].content.parts:
    if part.inline_data:
        img = Image.open(BytesIO(part.inline_data.data))
        img.save("cityscape.png")
```

---

### 4. AI Coding Agents Integration

#### **Kimi CLI (`~/.kimi-code/config.toml`)**
```toml
default_model = "antigravity/gemini-2.5-pro"

[models."antigravity/gemini-2.5-pro"]
provider = "antigravity"
model = "gemini-2.5-pro"
max_context_tokens = 1000000

[providers.antigravity]
type = "openai_compatible"
base_url = "http://127.0.0.1:7861/v1"
api_key = "pwd"
```

#### **OpenCode (`opencode.jsonc`)**
```jsonc
{
  "provider": {
    "antigravity": {
      "npm": "@ai-sdk/openai",
      "name": "createOpenAI",
      "options": {
        "baseURL": "http://127.0.0.1:7861/v1",
        "apiKey": "pwd"
      },
      "models": {
        "gemini-2.5-pro": { "name": "Gemini 2.5 Pro" },
        "claude-sonnet-4-5": { "name": "Claude 3.5 Sonnet" }
      }
    }
  }
}
```

---

## ⚙️ Configuration & Environment Variables

| Variable | Default | Description |
| :--- | :--- | :--- |
| `PORT` | `7861` | Server listening port |
| `HOST` | `0.0.0.0` | Server listen address (`0.0.0.0` for all interfaces, `127.0.0.1` for local only) |
| `API_PASSWORD` | `pwd` | Bearer token / API key required for chat completions |
| `PANEL_PASSWORD` | `pwd` | Password required to access the Web Dashboard |
| `VISIBLE_MODELS` | *(empty)* | Whitelist of models exposed in `/v1/models` (comma/newline separated; empty = all) |
| `MODEL_REWRITE_ENABLED`| `false` | Enable/disable incoming model alias rewriting |
| `MODEL_REWRITE_RULES` | `[]` | JSON list of `[regex_pattern, target_model]` replacement rules |
| `CREDENTIAL_SELECT_MODE`| `cache` | Account balancing strategy: `cache` (affinity), `random`, or `sticky` |
| `CREDENTIALS_DIR` | `./creds` | Local directory for storing SQLite credentials database |
| `POSTGRESQL_URI` | *(empty)* | Optional PostgreSQL connection string for multi-instance cloud deployments |
| `REDIS_URL` | *(empty)* | Optional Redis URL for shared multi-instance prompt cache affinity |
| `PROXY` | *(empty)* | HTTP/HTTPS/SOCKS5 outbound proxy (`socks5://user:pass@host:port`) |
| `RETRY_429_ENABLED` | `true` | Automatically retry rate-limited upstream requests |
| `RETRY_429_MAX_RETRIES`| `5` | Maximum retry attempts on 429 rate limit |
| `RETRY_429_INTERVAL` | `1.0` | Initial backoff retry delay in seconds |
| `ANTI_TRUNCATION_MAX_ATTEMPTS` | `3` | Maximum stitching passes for `autostitch-*` models |
| `IP_WHITELIST_ENABLED` | `false` | Restrict panel and API access to specified IP ranges |
| `IP_WHITELIST` | *(empty)* | Comma-separated list of IPs, CIDRs (`192.168.1.0/24`), or IP ranges |
| `SENSITIVE_WORDS_ENABLED` | `false`| Redact sensitive keywords from prompt inputs |
| `USAGE_LOG_ENABLED` | `true` | Record request metadata, token counts, and latency to database |
| `REQUEST_LOG_ENABLED` | `false` | Save full sanitized request headers & bodies for debug status codes |
| `LOG_LEVEL` | `INFO` | Console log verbosity (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |
| `ENABLE_LOG` | `1` | Set to `0` for completely silent console output |

---

## 💾 Data Storage

* **SQLite (Default)**: Zero configuration required. Credentials, statistics, and settings are stored locally in `./creds/credentials.db`.
* **PostgreSQL (Cloud / Distributed)**: Set the `POSTGRESQL_URI` environment variable to connect multiple proxy instances to a shared cloud database.

---

## 🛠️ Frontend Development

The web console source is located in `frontend/` (React + Vite + Tailwind CSS):

```bash
cd frontend
npm install
npm run dev     # Starts development dev server with hot reload
npm run build   # Compiles production assets into dist/
```

The Python FastAPI backend serves the compiled static files directly from `dist/`; no Node.js runtime is required in production.

---

## 📄 License & Disclaimer

* **Disclaimer**: This project is developed strictly for educational, testing, and research purposes. Users are responsible for complying with applicable terms of service.
* **License**: This project is licensed under the **Cooperative Non-Commercial License (CNC-1.0)**. Commercial resale and paid exploitation are strictly prohibited. See [LICENSE](LICENSE) for details.
