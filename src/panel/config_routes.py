"""
Configuration route module - Handles HTTP requests related to /config/*
"""

import re

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

import config
from log import log
from src.models import ConfigSaveRequest
from src.storage.database import get_database
from src.utils import verify_panel_token
from .utils import get_env_locked_keys


# Create router
router = APIRouter(prefix="/config", tags=["config"])


@router.get("/get")
async def get_config(token: str = Depends(verify_panel_token)):
    """Get current configuration."""
    try:


        # Read current configuration (including environment variables and TOML file configurations)
        current_config = {}

        # Basic configuration
        current_config["credentials_dir"] = await config.get_credentials_dir()
        current_config["proxy"] = await config.get_proxy_config() or ""

        # Proxy endpoint configuration
        current_config["oauth_proxy_url"] = await config.get_oauth_proxy_url()
        current_config["googleapis_proxy_url"] = await config.get_googleapis_proxy_url()
        current_config["antigravity_api_url"] = await config.get_antigravity_api_url()

        # Auto-ban configuration
        current_config["auto_ban_enabled"] = await config.get_auto_ban_enabled()
        current_config["auto_ban_error_codes"] = await config.get_auto_ban_error_codes()

        # 429 retry configuration
        current_config["retry_429_max_retries"] = await config.get_retry_429_max_retries()
        current_config["retry_429_enabled"] = await config.get_retry_429_enabled()
        current_config["retry_429_interval"] = await config.get_retry_429_interval()
        # Anti-truncation configuration
        current_config["anti_truncation_max_attempts"] = await config.get_anti_truncation_max_attempts()

        # Compatibility configuration

        # Chain of thought return configuration
        current_config["return_thoughts_to_frontend"] = await config.get_return_thoughts_to_frontend()

        # Sensitive words filter configuration
        current_config["sensitive_words_enabled"] = await config.get_sensitive_words_enabled()
        current_config["sensitive_words"] = await config.get_sensitive_words()

        # IP whitelist configuration
        current_config["ip_whitelist_enabled"] = await config.get_ip_whitelist_enabled()
        current_config["ip_whitelist"] = await config.get_ip_whitelist()

        # Model rewrite and visible models configuration
        current_config["model_rewrite_enabled"] = await config.get_model_rewrite_enabled()
        current_config["model_rewrite_rules"] = await config.get_model_rewrite_rules_config()
        current_config["visible_models"] = await config.get_visible_models()

        # Request log configuration
        current_config["usage_log_enabled"] = await config.get_usage_log_enabled()

        # Request body detail log configuration
        current_config["request_log_enabled"] = await config.get_request_log_enabled()
        current_config["request_log_error_codes"] = await config.get_request_log_error_codes()

        # Credential selection mode
        current_config["credential_select_mode"] = await config.get_credential_select_mode()

        # Server configuration
        current_config["host"] = await config.get_server_host()
        current_config["port"] = await config.get_server_port()
        current_config["api_password"] = await config.get_api_password()
        current_config["panel_password"] = await config.get_panel_password()

        # Read configuration from database
        database = await get_database()
        storage_config = await database.get_all_config()

        # Get configuration keys locked by environment variables
        env_locked_keys = get_env_locked_keys()

        # Merge storage system configuration (do not overwrite environment variables)
        for key, value in storage_config.items():
            if key not in env_locked_keys:
                current_config[key] = value

        return JSONResponse(content={"config": current_config, "env_locked": list(env_locked_keys)})

    except Exception as e:
        log.error(f"Failed to get configuration: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/save")
async def save_config(request: ConfigSaveRequest, token: str = Depends(verify_panel_token)):
    """Save configuration."""
    try:

        new_config = request.config

        log.debug(f"Received configuration data: {list(new_config.keys())}")

        # Validate configuration items
        if "retry_429_max_retries" in new_config:
            if (
                not isinstance(new_config["retry_429_max_retries"], int)
                or new_config["retry_429_max_retries"] < 0
            ):
                raise HTTPException(status_code=400, detail="Max 429 retries must be an integer greater than or equal to 0")

        if "retry_429_enabled" in new_config:
            if not isinstance(new_config["retry_429_enabled"], bool):
                raise HTTPException(status_code=400, detail="429 retry enabled switch must be a boolean")

        # Validate new configuration items
        if "retry_429_interval" in new_config:
            try:
                interval = float(new_config["retry_429_interval"])
                if interval < 0 or interval > 3600:
                    raise HTTPException(status_code=400, detail="429 retry interval must be between 0 and 3600 seconds")
            except (ValueError, TypeError):
                raise HTTPException(status_code=400, detail="429 retry interval must be a valid number")

        if "anti_truncation_max_attempts" in new_config:
            if (
                not isinstance(new_config["anti_truncation_max_attempts"], int)
                or new_config["anti_truncation_max_attempts"] < 1
                or new_config["anti_truncation_max_attempts"] > 10
            ):
                raise HTTPException(
                    status_code=400, detail="Anti-truncation max attempts must be an integer between 1 and 10"
                )

        if "return_thoughts_to_frontend" in new_config:
            if not isinstance(new_config["return_thoughts_to_frontend"], bool):
                raise HTTPException(status_code=400, detail="Return thoughts to frontend switch must be a boolean")

        # Validate sensitive words filter configuration
        if "sensitive_words_enabled" in new_config:
            if not isinstance(new_config["sensitive_words_enabled"], bool):
                raise HTTPException(status_code=400, detail="Sensitive words filter switch must be a boolean")

        if "sensitive_words" in new_config:
            words = new_config["sensitive_words"]
            if not isinstance(words, list):
                raise HTTPException(status_code=400, detail="Sensitive words list must be an array (one word per line)")

            cleaned_words = []
            for word in words:
                if not isinstance(word, str):
                    raise HTTPException(status_code=400, detail=f"Sensitive word must be a string: {word!r}")
                word = word.strip()
                if not word:
                    continue
                try:
                    re.compile(re.escape(word), re.IGNORECASE | re.UNICODE)
                except re.error:
                    raise HTTPException(status_code=400, detail=f"Invalid sensitive word: {word!r}")
                cleaned_words.append(word)
            new_config["sensitive_words"] = cleaned_words

        # Validate IP whitelist configuration (one per line, supports single IP / CIDR / IP range, IPv4 / IPv6)
        if "ip_whitelist_enabled" in new_config:
            if not isinstance(new_config["ip_whitelist_enabled"], bool):
                raise HTTPException(status_code=400, detail="IP whitelist switch must be a boolean")

        if "ip_whitelist" in new_config:
            entries = new_config["ip_whitelist"]
            if not isinstance(entries, list):
                raise HTTPException(status_code=400, detail="IP whitelist must be an array (one entry per line)")
            cleaned_entries = []
            for entry in entries:
                if not isinstance(entry, str):
                    raise HTTPException(status_code=400, detail=f"IP whitelist entry must be a string: {entry!r}")
                entry = entry.strip()
                if not entry:
                    continue
                try:
                    config.parse_ip_whitelist_line(entry)
                except ValueError:
                    raise HTTPException(status_code=400, detail=f"Invalid IP whitelist entry: {entry!r}")
                cleaned_entries.append(entry)
            new_config["ip_whitelist"] = cleaned_entries

        # Validate model rewrite configuration (each rule is [pattern, replacement], pattern supports regex)
        if "model_rewrite_enabled" in new_config:
            if not isinstance(new_config["model_rewrite_enabled"], bool):
                raise HTTPException(status_code=400, detail="Model rewrite switch must be a boolean")

        if "model_rewrite_rules" in new_config:
            rules = new_config["model_rewrite_rules"]
            if not isinstance(rules, list):
                raise HTTPException(status_code=400, detail="Model rewrite rules must be an array (each item is [match regex, replacement model name])")
            cleaned_rules = []
            for rule in rules:
                if not (isinstance(rule, (list, tuple)) and len(rule) == 2 and all(isinstance(x, str) for x in rule)):
                    raise HTTPException(
                        status_code=400,
                        detail=f"Each rule must be a string tuple of [match regex, replacement model name]: {rule!r}",
                    )
                pattern, replacement = rule[0].strip(), rule[1].strip()
                if not pattern or not replacement:
                    raise HTTPException(status_code=400, detail=f"Rule regex and replacement model name cannot be empty: {rule!r}")
                try:
                    re.compile(pattern)
                except re.error:
                    raise HTTPException(status_code=400, detail=f"Invalid regular expression: {pattern!r}")
                cleaned_rules.append([pattern, replacement])
            new_config["model_rewrite_rules"] = cleaned_rules

        # Validate visible models configuration (one model per line, empty = show all)
        if "visible_models" in new_config:
            models = new_config["visible_models"]
            if not isinstance(models, list):
                raise HTTPException(status_code=400, detail="Visible models must be an array (one model per line)")
            cleaned_models = []
            for m in models:
                if not isinstance(m, str):
                    raise HTTPException(status_code=400, detail=f"Visible model name must be a string: {m!r}")
                m = m.strip()
                if m and m not in cleaned_models:
                    cleaned_models.append(m)
            new_config["visible_models"] = cleaned_models

        # Validate request log configuration
        if "usage_log_enabled" in new_config:
            if not isinstance(new_config["usage_log_enabled"], bool):
                raise HTTPException(status_code=400, detail="Request log switch must be a boolean")

        # Validate request body detail log configuration
        if "request_log_enabled" in new_config:
            if not isinstance(new_config["request_log_enabled"], bool):
                raise HTTPException(status_code=400, detail="Request body detail log switch must be a boolean")

        if "request_log_error_codes" in new_config:
            codes = new_config["request_log_error_codes"]
            if not isinstance(codes, list):
                raise HTTPException(status_code=400, detail="Detail log error codes must be an array, e.g. [400, 429]")
            cleaned_codes = []
            for code in codes:
                if isinstance(code, bool) or not isinstance(code, int) or code < 100 or code > 599:
                    raise HTTPException(status_code=400, detail=f"Error code must be an integer between 100 and 599: {code!r}")
                if code not in cleaned_codes:
                    cleaned_codes.append(code)
            new_config["request_log_error_codes"] = sorted(cleaned_codes)

        # Validate credential selection mode
        if "credential_select_mode" in new_config:
            mode = new_config["credential_select_mode"]
            if mode not in ("random", "cache", "sticky"):
                raise HTTPException(status_code=400, detail="Credential selection mode must be random / cache / sticky")

        # Validate server configuration
        if "host" in new_config:
            if not isinstance(new_config["host"], str) or not new_config["host"].strip():
                raise HTTPException(status_code=400, detail="Server host address cannot be empty")

        if "port" in new_config:
            if (
                not isinstance(new_config["port"], int)
                or new_config["port"] < 1
                or new_config["port"] > 65535
            ):
                raise HTTPException(status_code=400, detail="Port number must be an integer between 1 and 65535")

        if "api_password" in new_config:
            if not isinstance(new_config["api_password"], str):
                raise HTTPException(status_code=400, detail="API access password must be a string")

        if "panel_password" in new_config:
            if not isinstance(new_config["panel_password"], str):
                raise HTTPException(status_code=400, detail="Control panel password must be a string")

        # Get configuration keys locked by environment variables
        env_locked_keys = get_env_locked_keys()

        # Directly write to database
        database = await get_database()
        for key, value in new_config.items():
            if key not in env_locked_keys:
                await database.set_config(key, value)
                if key in ("api_password", "panel_password"):
                    log.debug(f"Set {key} field to: {value}")

        # Reload configuration cache (critical!)
        await config.reload_config()

        # Verify saved result
        test_api_password = await config.get_api_password()
        test_panel_password = await config.get_panel_password()
        log.debug(f"API password read immediately after save: {test_api_password}")
        log.debug(f"Panel password read immediately after save: {test_panel_password}")

        # Build response message
        response_data = {
            "message": "Configuration saved successfully",
            "saved_config": {k: v for k, v in new_config.items() if k not in env_locked_keys},
        }

        return JSONResponse(content=response_data)

    except HTTPException:
        raise
    except Exception as e:
        log.error(f"Failed to save configuration: {e}")
        raise HTTPException(status_code=500, detail=str(e))
