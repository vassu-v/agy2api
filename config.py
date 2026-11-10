"""
Configuration constants for the Geminicli2api proxy server.
Centralizes all configuration to avoid duplication across modules.

- Loads configuration into memory once on startup
- Calls reload_config() to reload from database when configuration is updated
"""

import ipaddress
import json
import os
import re
from typing import Any, Optional

from log import log

# Global configuration cache
_config_cache: dict[str, Any] = {}
_config_initialized = False

# Client Configuration

# Error codes requiring auto-ban (default values, can be overridden by env var or config)
AUTO_BAN_ERROR_CODES = [403]

# ====================== Environment Variable Mapping ======================
# Uniformly maintain mappings between environment variable names and config key names
# Format: "ENV_VAR_NAME": "config_key_name"
ENV_MAPPINGS = {
    "CREDENTIALS_DIR": "credentials_dir",
    "PROXY": "proxy",
    "OAUTH_PROXY_URL": "oauth_proxy_url",
    "GOOGLEAPIS_PROXY_URL": "googleapis_proxy_url",
    "ANTIGRAVITY_API_URL": "antigravity_api_url",
    "AUTO_BAN": "auto_ban_enabled",
    "AUTO_BAN_ERROR_CODES": "auto_ban_error_codes",
    "RETRY_429_MAX_RETRIES": "retry_429_max_retries",
    "RETRY_429_ENABLED": "retry_429_enabled",
    "RETRY_429_INTERVAL": "retry_429_interval",
    "ANTI_TRUNCATION_MAX_ATTEMPTS": "anti_truncation_max_attempts",
    "RETURN_THOUGHTS_TO_FRONTEND": "return_thoughts_to_frontend",
    "SENSITIVE_WORDS": "sensitive_words",
    "SENSITIVE_WORDS_ENABLED": "sensitive_words_enabled",
    "IP_WHITELIST": "ip_whitelist",
    "IP_WHITELIST_ENABLED": "ip_whitelist_enabled",
    "MODEL_REWRITE_ENABLED": "model_rewrite_enabled",
    "MODEL_REWRITE_RULES": "model_rewrite_rules",
    "VISIBLE_MODELS": "visible_models",
    "USAGE_LOG_ENABLED": "usage_log_enabled",
    "REQUEST_LOG_ENABLED": "request_log_enabled",
    "REQUEST_LOG_ERROR_CODES": "request_log_error_codes",
    "CREDENTIAL_SELECT_MODE": "credential_select_mode",
    "HOST": "host",
    "PORT": "port",
    "API_PASSWORD": "api_password",
    "PANEL_PASSWORD": "panel_password",
}


# ====================== Configuration System ======================

async def init_config():
    """Initialize configuration cache (called once on startup)."""
    global _config_cache, _config_initialized

    if _config_initialized:
        return

    try:
        from src.storage.database import get_database
        database = await get_database()
        _config_cache = await database.get_all_config()
        _config_initialized = True
    except Exception:
        # Use empty cache on initialization failure
        _config_cache = {}
        _config_initialized = True


async def reload_config():
    """Reload configuration (called after modifying configuration)."""
    global _config_cache, _config_initialized

    try:
        from src.storage.database import get_database
        database = await get_database()

        # Reload database configuration cache
        await database.reload_config_cache()

        # Reload configuration cache
        _config_cache = await database.get_all_config()
        _config_initialized = True
    except Exception:
        pass


def _get_cached_config(key: str, default: Any = None) -> Any:
    """Get configuration from in-memory cache (synchronous)."""
    return _config_cache.get(key, default)


async def get_config_value(key: str, default: Any = None, env_var: Optional[str] = None) -> Any:
    """Get configuration value with priority: ENV > Storage > default."""
    # Ensure configuration is initialized
    if not _config_initialized:
        await init_config()

    # Priority 1: Environment variable
    if env_var and os.getenv(env_var):
        return os.getenv(env_var)

    # Priority 2: Memory cache
    value = _get_cached_config(key)
    if value is not None:
        return value

    return default


# Configuration getters - all async
async def get_proxy_config():
    """Get proxy configuration, validating the URL format.

    Supports http://, https://, socks5://, socks5h:// schemes, all with
    optional embedded credentials (user:pass@host:port).
    """
    proxy_url = await get_config_value("proxy", env_var="PROXY")
    if not proxy_url:
        return None

    proxy_url = proxy_url.strip()
    scheme = proxy_url.split("://", 1)[0].lower()
    if scheme not in ("http", "https", "socks5", "socks5h"):
        log.warning(
            f"Unsupported proxy protocol: {scheme}:// (supports http/https/socks5/socks5h), proxy ignored"
        )
        return None
    return proxy_url


async def get_auto_ban_enabled() -> bool:
    """Get auto ban enabled setting."""
    env_value = os.getenv("AUTO_BAN")
    if env_value:
        return env_value.lower() in ("true", "1", "yes", "on")

    return bool(await get_config_value("auto_ban_enabled", False))


async def get_auto_ban_error_codes() -> list:
    """
    Get auto ban error codes.

    Environment variable: AUTO_BAN_ERROR_CODES (comma-separated, e.g., "400,403")
    Database config key: auto_ban_error_codes
    Default: [400, 403]
    """
    env_value = os.getenv("AUTO_BAN_ERROR_CODES")
    if env_value:
        try:
            return [int(code.strip()) for code in env_value.split(",") if code.strip()]
        except ValueError:
            pass

    codes = await get_config_value("auto_ban_error_codes")
    if codes and isinstance(codes, list):
        return codes
    return AUTO_BAN_ERROR_CODES


async def get_retry_429_max_retries() -> int:
    """Get max retries for 429 errors."""
    env_value = os.getenv("RETRY_429_MAX_RETRIES")
    if env_value:
        try:
            return int(env_value)
        except ValueError:
            pass

    return int(await get_config_value("retry_429_max_retries", 5))


async def get_retry_429_enabled() -> bool:
    """Get 429 retry enabled setting."""
    env_value = os.getenv("RETRY_429_ENABLED")
    if env_value:
        return env_value.lower() in ("true", "1", "yes", "on")

    return bool(await get_config_value("retry_429_enabled", True))


async def get_retry_429_interval() -> float:
    """Get 429 retry interval in seconds."""
    env_value = os.getenv("RETRY_429_INTERVAL")
    if env_value:
        try:
            return float(env_value)
        except ValueError:
            pass

    return float(await get_config_value("retry_429_interval", 1))


async def get_anti_truncation_max_attempts() -> int:
    """
    Get maximum attempts for anti-truncation continuation.

    Environment variable: ANTI_TRUNCATION_MAX_ATTEMPTS
    Database config key: anti_truncation_max_attempts
    Default: 3
    """
    env_value = os.getenv("ANTI_TRUNCATION_MAX_ATTEMPTS")
    if env_value:
        try:
            return int(env_value)
        except ValueError:
            pass

    return int(await get_config_value("anti_truncation_max_attempts", 3))


# Server Configuration
async def get_server_host() -> str:
    """
    Get server host setting.

    Environment variable: HOST
    Database config key: host
    Default: 0.0.0.0
    """
    return str(await get_config_value("host", "0.0.0.0", "HOST"))


async def get_server_port() -> int:
    """
    Get server port setting.

    Environment variable: PORT
    Database config key: port
    Default: 7861
    """
    env_value = os.getenv("PORT")
    if env_value:
        try:
            return int(env_value)
        except ValueError:
            pass

    return int(await get_config_value("port", 7861))


async def get_api_password() -> str:
    """
    Get API password setting for chat endpoints.

    Environment variable: API_PASSWORD
    Database config key: api_password
    Default: pwd
    """
    return str(await get_config_value("api_password", "pwd", "API_PASSWORD"))


async def get_panel_password() -> str:
    """
    Get panel password setting for web interface.

    Environment variable: PANEL_PASSWORD
    Database config key: panel_password
    Default: pwd
    """
    return str(await get_config_value("panel_password", "pwd", "PANEL_PASSWORD"))


async def get_credentials_dir() -> str:
    """
    Get credentials directory setting.

    Environment variable: CREDENTIALS_DIR
    Database config key: credentials_dir
    Default: ./creds
    """
    return str(await get_config_value("credentials_dir", "./creds", "CREDENTIALS_DIR"))


async def get_return_thoughts_to_frontend() -> bool:
    """
    Get return thoughts to frontend setting.

    Controls whether thinking chain is returned to the frontend.
    When enabled, thought reasoning is returned in the response;
    when disabled, thought reasoning is filtered out.

    Environment variable: RETURN_THOUGHTS_TO_FRONTEND
    Database config key: return_thoughts_to_frontend
    Default: True
    """
    env_value = os.getenv("RETURN_THOUGHTS_TO_FRONTEND")
    if env_value:
        return env_value.lower() in ("true", "1", "yes", "on")

    return bool(await get_config_value("return_thoughts_to_frontend", True))


# ====================== Sensitive Words Filter ======================

# Sensitive words regex compilation cache: (original words fingerprint, compiled pattern list)
_sensitive_words_cache: dict = {"fingerprint": None, "patterns": []}


async def get_sensitive_words_enabled() -> bool:
    """
    Sensitive words filter toggle.

    Environment variable: SENSITIVE_WORDS_ENABLED
    Database config key: sensitive_words_enabled
    Default: False
    """
    env_value = os.getenv("SENSITIVE_WORDS_ENABLED")
    if env_value:
        return env_value.lower() in ("true", "1", "yes", "on")

    return bool(await get_config_value("sensitive_words_enabled", False))


async def get_sensitive_words() -> list:
    """
    Sensitive words list (supports regex), one per line; empty list means no filtering.

    Environment variable: SENSITIVE_WORDS (comma or newline separated)
    Database config key: sensitive_words
    Default: []
    """
    env_value = os.getenv("SENSITIVE_WORDS")
    if env_value:
        words = [w.strip() for w in re.split(r"[,\n]", env_value) if w.strip()]
        return words

    value = await get_config_value("sensitive_words")
    if isinstance(value, list):
        return [str(w) for w in value if str(w).strip()]
    if isinstance(value, str) and value.strip():
        return [w.strip() for w in re.split(r"[,\n]", value) if w.strip()]
    return []


def compile_sensitive_words(words: list):
    """
    Compile sensitive words into case-insensitive regex patterns (cached).

    Each word is escaped as a literal before compilation (user inputs words/phrases, not regex syntax),
    matching words are replaced with empty strings. Supports Unicode.

    Returns:
        Compiled Pattern list; invalid entries are skipped with a warning.
    """
    fingerprint = tuple(words)
    if _sensitive_words_cache["fingerprint"] == fingerprint:
        return _sensitive_words_cache["patterns"]

    patterns = []
    for word in words:
        word = str(word).strip()
        if not word:
            continue
        try:
            patterns.append(re.compile(re.escape(word), re.IGNORECASE | re.UNICODE))
        except re.error as e:
            log.warning(f"Failed to compile sensitive word, skipped: {word!r} ({e})")

    _sensitive_words_cache["fingerprint"] = fingerprint
    _sensitive_words_cache["patterns"] = patterns
    return patterns


def apply_sensitive_word_filter(text: str, patterns) -> str:
    """Replace matched sensitive words (case-insensitive) in text with empty string."""
    if not text or not patterns:
        return text
    for pattern in patterns:
        text = pattern.sub("", text)
    return text


async def get_sensitive_word_filter():
    """
    Get active sensitive words filter condition (for request handling pipeline).

    Returns:
        (enabled, patterns); patterns is empty list when disabled.
    """
    if not await get_sensitive_words_enabled():
        return False, []
    words = await get_sensitive_words()
    if not words:
        return False, []
    return True, compile_sensitive_words(words)


# ====================== IP Whitelist ======================

# IP whitelist rule cache: (original entries fingerprint, parsed result (rules, invalid))
_ip_whitelist_cache: dict = {"fingerprint": None, "result": ([], [])}


def parse_ip_whitelist_line(line: str):
    """
    Parse single whitelist entry into (lo, hi, version) network objects; raises ValueError if invalid.

    Supported formats (IPv4 / IPv6):
    - Single IP: 192.168.1.1 / ::1
    - CIDR subnet: 192.168.1.0/24 / 2001:db8::/32
    - IP range (- or ~ separated): 192.168.1.1-192.168.1.100 / ::1-::10
    """
    line = str(line).strip()
    if not line:
        raise ValueError("Empty entry")

    for sep in ("~", "-"):
        if sep in line:
            start_str, _, end_str = line.partition(sep)
            if start_str.strip() and end_str.strip():
                start = ipaddress.ip_address(start_str.strip())
                end = ipaddress.ip_address(end_str.strip())
                if start.version != end.version:
                    raise ValueError(f"Mismatched start/end IP versions: {line}")
                if int(start) > int(end):
                    start, end = end, start
                return start, end, start.version

    if "/" in line:
        net = ipaddress.ip_network(line, strict=False)
        return net.network_address, net.broadcast_address, net.version

    addr = ipaddress.ip_address(line)
    return addr, addr, addr.version


def compile_ip_whitelist(entries: list):
    """
    Parse whitelist entries into rules list (cached).

    Returns:
        (rules, invalid) - rules is [(lo, hi, version), ...], invalid contains failed entries.
    """
    fingerprint = tuple(str(e) for e in entries)
    if _ip_whitelist_cache["fingerprint"] == fingerprint:
        return _ip_whitelist_cache["result"]

    rules = []
    invalid = []
    for entry in entries:
        entry = str(entry).strip()
        if not entry:
            continue
        try:
            rules.append(parse_ip_whitelist_line(entry))
        except ValueError:
            invalid.append(entry)

    result = (rules, invalid)
    _ip_whitelist_cache["fingerprint"] = fingerprint
    _ip_whitelist_cache["result"] = result
    return result


def ip_in_whitelist(client_ip: str, rules: list) -> bool:
    """Check if client IP matches whitelist rules (supports IPv4 and IPv6)."""
    if not client_ip:
        return False
    try:
        addr = ipaddress.ip_address(client_ip)
    except ValueError:
        return False
    return any(lo.version == addr.version and lo <= addr <= hi for lo, hi, _ in rules)


async def get_ip_whitelist_enabled() -> bool:
    """
    IP whitelist toggle.

    Environment variable: IP_WHITELIST_ENABLED
    Database config key: ip_whitelist_enabled
    Default: False
    """
    env_value = os.getenv("IP_WHITELIST_ENABLED")
    if env_value:
        return env_value.lower() in ("true", "1", "yes", "on")

    return bool(await get_config_value("ip_whitelist_enabled", False))


async def get_ip_whitelist() -> list:
    """
    IP whitelist list, one entry per line; empty list means unrestricted.

    Supports single IP, CIDR subnet, start-end ranges (- or ~ separated), IPv4/IPv6.
    Only takes effect when enabled; all requests outside whitelist (panel + API) return 403.

    Environment variable: IP_WHITELIST (comma or newline separated)
    Database config key: ip_whitelist
    Default: []
    """
    env_value = os.getenv("IP_WHITELIST")
    if env_value is not None and env_value.strip():
        return [e.strip() for e in re.split(r"[,\n]", env_value) if e.strip()]

    value = await get_config_value("ip_whitelist")
    if isinstance(value, list):
        return [str(e).strip() for e in value if str(e).strip()]
    if isinstance(value, str) and value.strip():
        return [e.strip() for e in re.split(r"[,\n]", value) if e.strip()]
    return []


async def get_ip_whitelist_rules():
    """
    Get active IP whitelist rules (for request interception middleware).

    Returns:
        (enabled, rules); restriction not enabled when toggle is off or whitelist is empty.
    """
    if not await get_ip_whitelist_enabled():
        return False, []
    entries = await get_ip_whitelist()
    if not entries:
        return False, []
    rules, invalid = compile_ip_whitelist(entries)
    if invalid:
        log.warning(f"Invalid entries found in IP whitelist (skipped): {invalid}")
    return bool(rules), rules


# ====================== Model Name Rewrite ======================

# Model name rewrite rule cache: (fingerprint, (compiled rules list, invalid rules list))
_model_rewrite_cache: dict = {"fingerprint": None, "result": ([], [])}


async def get_model_rewrite_enabled() -> bool:
    """
    Model name rewrite toggle.

    Environment variable: MODEL_REWRITE_ENABLED
    Database config key: model_rewrite_enabled
    Default: False
    """
    env_value = os.getenv("MODEL_REWRITE_ENABLED")
    if env_value:
        return env_value.lower() in ("true", "1", "yes", "on")

    return bool(await get_config_value("model_rewrite_enabled", False))


async def get_model_rewrite_rules_config() -> list:
    """
    Model name rewrite rule list, each entry is [pattern, replacement].

    pattern supports regex (searches model name, replaces on match),
    replacement can use regex backreferences (e.g. \\1).

    Environment variable: MODEL_REWRITE_RULES (JSON array, e.g. [["gpt-4","gemini-2.5-pro"]]; also supports "pat->rep" comma-separated format)
    Database config key: model_rewrite_rules
    Default: []
    """
    env_value = os.getenv("MODEL_REWRITE_RULES")
    if env_value is not None and env_value.strip():
        parsed = _parse_model_rewrite_rules_value(env_value)
        if parsed is not None:
            return parsed

    value = await get_config_value("model_rewrite_rules")
    return _parse_model_rewrite_rules_value(value) or []


def _parse_model_rewrite_rules_value(value: Any) -> Optional[list]:
    """Parse config value into [[pattern, replacement], ...]; returns None on failure."""
    if isinstance(value, list):
        rules = []
        for item in value:
            if isinstance(item, (list, tuple)) and len(item) == 2 and all(isinstance(x, str) for x in item):
                rules.append([item[0], item[1]])
            elif isinstance(item, dict) and isinstance(item.get("pattern"), str) and isinstance(item.get("replacement"), str):
                rules.append([item["pattern"], item["replacement"]])
        return rules
    if isinstance(value, str) and value.strip():
        # Prefer JSON array format (recommended env var format)
        if value.strip().startswith("["):
            try:
                parsed = json.loads(value)
                if isinstance(parsed, list):
                    inner = _parse_model_rewrite_rules_value(parsed)
                    if inner is not None:
                        return inner
            except (json.JSONDecodeError, ValueError):
                pass
        # Compatible with "pat->rep,pat->rep" simplified format
        rules = []
        for pair in value.split(","):
            pair = pair.strip()
            if "->" in pair:
                pat, _, rep = pair.partition("->")
                rules.append([pat.strip(), rep.strip()])
        return rules or None
    return None


def compile_model_rewrite_rules(rules: list):
    """
    Compile rewrite rules into (pattern, replacement) tuples (cached).

    Returns:
        (compiled, invalid) - invalid contains rules that failed compilation.
    """
    fingerprint = tuple(tuple(r) for r in rules)
    if _model_rewrite_cache["fingerprint"] == fingerprint:
        return _model_rewrite_cache["result"]

    compiled = []
    invalid = []
    for rule in rules:
        if not (isinstance(rule, (list, tuple)) and len(rule) == 2):
            continue
        pattern, replacement = rule[0], rule[1]
        try:
            compiled.append((re.compile(str(pattern)), str(replacement)))
        except re.error:
            invalid.append(rule)

    result = (compiled, invalid)
    _model_rewrite_cache["fingerprint"] = fingerprint
    _model_rewrite_cache["result"] = result
    return result


def rewrite_model_name(model_name: str, compiled_rules: list) -> str:
    """
    Apply model name rewrites in order (stops at first match).

    Uses re.sub for full string search and replace; replacement supports \\1 backreferences.
    """
    for pattern, replacement in compiled_rules:
        if pattern.search(model_name):
            return pattern.sub(replacement, model_name)
    return model_name


async def get_model_rewrite_filter():
    """
    Get active model rewrite rules (for request handling pipeline).

    Returns:
        (enabled, compiled_rules); compiled_rules is empty list when disabled.
    """
    if not await get_model_rewrite_enabled():
        return False, []
    rules = await get_model_rewrite_rules_config()
    if not rules:
        return False, []
    compiled, invalid = compile_model_rewrite_rules(rules)
    if invalid:
        log.warning(f"Invalid regex found in model rewrite rules (skipped): {invalid}")
    return bool(compiled), compiled


async def get_visible_models() -> list:
    """
    Get list of models to show/expose in model list endpoints (/v1/models, /v1beta/models).
    Empty list means all available upstream models are shown.

    Environment variable: VISIBLE_MODELS (comma or newline separated)
    Database config key: visible_models
    Default: []
    """
    env_value = os.getenv("VISIBLE_MODELS")
    if env_value is not None and env_value.strip():
        return [m.strip() for m in re.split(r"[,\n]", env_value) if m.strip()]

    value = await get_config_value("visible_models")
    if isinstance(value, list):
        return [str(m).strip() for m in value if str(m).strip()]
    if isinstance(value, str) and value.strip():
        return [m.strip() for m in re.split(r"[,\n]", value) if m.strip()]
    return []


# ====================== Request Logging ======================

async def get_usage_log_enabled() -> bool:
    """
    Request usage log (usage_log) toggle.

    Environment variable: USAGE_LOG_ENABLED
    Database config key: usage_log_enabled
    Default: True
    """
    env_value = os.getenv("USAGE_LOG_ENABLED")
    if env_value:
        return env_value.lower() in ("true", "1", "yes", "on")

    return bool(await get_config_value("usage_log_enabled", True))


async def get_request_log_enabled() -> bool:
    """
    Detailed request body log toggle.

    Environment variable: REQUEST_LOG_ENABLED
    Database config key: request_log_enabled
    Default: False
    """
    env_value = os.getenv("REQUEST_LOG_ENABLED")
    if env_value:
        return env_value.lower() in ("true", "1", "yes", "on")

    return bool(await get_config_value("request_log_enabled", False))


async def get_request_log_error_codes() -> list:
    """
    Error codes list for detailed request body logging (persisted to disk only on match).

    Environment variable: REQUEST_LOG_ERROR_CODES (comma-separated, e.g. "400,429")
    Database config key: request_log_error_codes
    Default: [400]
    """
    env_value = os.getenv("REQUEST_LOG_ERROR_CODES")
    if env_value:
        try:
            return [int(code.strip()) for code in env_value.split(",") if code.strip()]
        except ValueError:
            pass

    codes = await get_config_value("request_log_error_codes")
    if codes and isinstance(codes, list):
        return codes
    return [400]


# ====================== Credential Select Mode ======================

# Valid credential select modes
CREDENTIAL_SELECT_MODES = ("random", "cache", "sticky")


async def get_credential_select_mode() -> str:
    """
    Credential selection mode:
    - random: Pure random load balancing (no affinity)
    - cache:  Session cache match (default) - reuses same credential for identical session to hit upstream prompt cache
    - sticky: Sticky credential - maintains same credential until cooldown or disabled

    Environment variable: CREDENTIAL_SELECT_MODE
    Database config key: credential_select_mode
    Default: cache
    """
    value = await get_config_value("credential_select_mode", "cache", "CREDENTIAL_SELECT_MODE")
    value = str(value).strip().lower()
    if value not in CREDENTIAL_SELECT_MODES:
        log.warning(f"Invalid credential selection mode: {value}, falling back to cache")
        return "cache"
    return value


async def get_oauth_proxy_url() -> str:
    """
    Get OAuth proxy URL setting.

    Proxy URL used for Google OAuth2 authentication.

    Environment variable: OAUTH_PROXY_URL
    Database config key: oauth_proxy_url
    Default: https://oauth2.googleapis.com
    """
    return str(
        await get_config_value(
            "oauth_proxy_url", "https://oauth2.googleapis.com", "OAUTH_PROXY_URL"
        )
    )


async def get_googleapis_proxy_url() -> str:
    """
    Get Google APIs proxy URL setting.

    Proxy URL used for Google APIs invocations.

    Environment variable: GOOGLEAPIS_PROXY_URL
    Database config key: googleapis_proxy_url
    Default: https://www.googleapis.com
    """
    return str(
        await get_config_value(
            "googleapis_proxy_url", "https://www.googleapis.com", "GOOGLEAPIS_PROXY_URL"
        )
    )


async def get_antigravity_api_url() -> str:
    """
    Get Antigravity API URL setting.

    URL used for Google Antigravity API.

    Environment variable: ANTIGRAVITY_API_URL
    Database config key: antigravity_api_url
    Default: https://daily-cloudcode-pa.googleapis.com
    """
    return str(
        await get_config_value(
            "antigravity_api_url",
            "https://daily-cloudcode-pa.googleapis.com",
            "ANTIGRAVITY_API_URL",
        )
    )
