import { Loader2, Plus, RefreshCw, Save, Trash2 } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import Modal from "../components/Modal";
import Select from "../components/Select";
import api from "../api";




function CsvNumbersField({ value, onChange, placeholder }) {
  const toText = (v) => (Array.isArray(v) ? v.join(", ") : (v ?? ""));
  const [text, setText] = useState(() => toText(value));
  const focusedRef = useRef(false);

  
  useEffect(() => {
    if (!focusedRef.current) setText(toText(value));
  }, [value]);

  const commitText = (raw) => {
    const nums = raw
      .split(/[,\s]+/)
      .map((x) => parseInt(x, 10))
      .filter((n) => Number.isInteger(n) && n >= 100 && n <= 599);
    onChange([...new Set(nums)].sort((a, b) => a - b));
  };

  return (
    <input
      type="text"
      value={text}
      onFocus={() => {
        focusedRef.current = true;
      }}
      onChange={(e) => {
        const raw = e.target.value;
        setText(raw);
        commitText(raw);
      }}
      onBlur={() => {
        focusedRef.current = false;
        commitText(text);
      }}
      placeholder={placeholder}
      className="w-full sm:w-64 px-3 py-2 bg-dark-800 border border-dark-600 rounded-lg text-sm text-white placeholder-gray-500 font-mono"
    />
  );
}





function LinesField({ value, onChange, placeholder }) {
  const toText = (v) => (Array.isArray(v) ? v.join("\n") : v ?? "");
  const [text, setText] = useState(() => toText(value));
  const focusedRef = useRef(false);

  
  useEffect(() => {
    if (!focusedRef.current) setText(toText(value));
  }, [value]);

  const commitText = (raw) => {
    const lines = raw
      .split(/\r?\n/)
      .map((w) => w.replace(/\s+$/, ""))
      .filter((w) => w.trim() !== "");
    onChange(lines);
  };

  return (
    <textarea
      value={text}
      onFocus={() => {
        focusedRef.current = true;
      }}
      onChange={(e) => {
        const raw = e.target.value;
        setText(raw);
        commitText(raw);
      }}
      onBlur={() => {
        focusedRef.current = false;
        commitText(text);
      }}
      placeholder={placeholder}
      rows={6}
      className="w-full px-3 py-2 bg-dark-800 border border-dark-600 rounded-lg text-sm text-white placeholder-gray-500 font-mono"
    />
  );
}

const CONFIG_GROUPS = [
  {
    title: "Server Configuration",
    fields: [
      {
        key: "host",
        label: "Server Host (Listen Address)",
        type: "select",
        note: "Choose whether the proxy listens locally only or on all network interfaces (restart required).",
        options: [
          { value: "0.0.0.0", label: "0.0.0.0 (All Network Interfaces - LAN & Tailscale)" },
          { value: "127.0.0.1", label: "127.0.0.1 (Local Machine Only)" },
        ],
      },
      { key: "port", label: "Server Port", type: "number", note: "Port number for the server to listen on (restart required)" },
      { key: "api_password", label: "API Access Password", type: "text", note: "API key password used for OpenAI, Gemini, and Claude endpoint auth" },
      { key: "panel_password", label: "Control Panel Password", type: "text", note: "Password required to log into the web control panel" },
    ],
  },
  {
    title: "IP Whitelist",
    fields: [
      { key: "ip_whitelist_enabled", label: "Enable IP Whitelist", type: "bool", note: "When enabled, only whitelisted IPs can access the panel and API (others get 403). Hot-reloaded upon saving." },
      { key: "ip_whitelist", label: "IP Whitelist List", type: "textarea", showIf: (cfg) => !!cfg.ip_whitelist_enabled, note: "One entry per line. Supports single IPs, CIDR ranges, or start-end ranges (IPv4/IPv6).", placeholder: "Example:\n127.0.0.1\n192.168.1.0/24\n10.0.0.1-10.0.0.100\n::1\n2001:db8::/32" },
    ],
  },
  {
    title: "Basic Configuration",
    fields: [
      { key: "credentials_dir", label: "Credentials Directory Path", type: "text", note: "Directory path storing credential database and JSON files" },
      { key: "proxy", label: "Proxy Settings", type: "text", note: "Supports http/https/socks5/socks5h with optional credentials. Leave empty to disable.", placeholder: "http://user:pass@proxy:port or socks5h://proxy:port" },
    ],
  },
  {
    title: "Endpoint Configuration",
    fields: [
      { key: "oauth_proxy_url", label: "OAuth Endpoint", type: "text", note: "Google OAuth2 endpoint for token acquisition and refresh" },
      { key: "googleapis_proxy_url", label: "Google APIs Endpoint", type: "text", note: "Google APIs endpoint" },
      { key: "antigravity_api_url", label: "Antigravity API Endpoint", type: "text", note: "Antigravity API endpoint" },
    ],
  },
  {
    title: "Auto-Ban Configuration",
    fields: [
      { key: "auto_ban_enabled", label: "Enable Auto-Ban", type: "bool", note: "Automatically disable credentials upon receiving specified error codes" },
      { key: "auto_ban_error_codes", label: "Auto-Ban Error Codes", type: "text", note: "JSON array format, e.g. [403, 400]" },
    ],
  },
  {
    title: "Retry Configuration",
    fields: [
      { key: "retry_429_enabled", label: "Enable Error Retries", type: "bool", note: "Automatically retry requests when errors occur" },
      { key: "retry_429_max_retries", label: "Max Retry Attempts", type: "number", note: "Maximum retry attempts before returning error" },
      { key: "retry_429_interval", label: "Retry Interval (Seconds)", type: "number", note: "Wait time in seconds between retries (0 = immediate)" },
    ],
  },
  {
    title: "Credential Selection Mode",
    fields: [
      {
        key: "credential_select_mode",
        label: "Selection Strategy",
        type: "select",
        note: "Strategy used to select which Google credential serves each incoming request",
        options: [
          { value: "random", label: "Completely Random (Round-Robin)" },
          { value: "cache", label: "Prompt Cache Match (Recommended)" },
          { value: "sticky", label: "Sticky Until Cooldown (CD)" },
        ],
        descriptions: {
          random: "Random Load Balancing: Randomly picks an active credential for each request. Evenly distributes traffic but does not maintain session affinity for prompt caching.",
          cache: "Prompt Cache Matching (Recommended): Hashes model and system prompt into a session key so consecutive turns reuse the same credential, maximizing Google's server-side prompt cache.",
          sticky: "Sticky Credential: All requests lock to a single credential until it hits cooldown (429) or is disabled. Maximizes cache hits for that account but does not distribute load.",
        },
      },
    ],
  },
  {
    title: "Response Configuration",
    fields: [
      { key: "return_thoughts_to_frontend", label: "Return Thinking Tokens to Client", type: "bool", note: "Include reasoning / thought tokens in responses (disabled filters them out)" },
    ],
  },
  {
    title: "Anti-Truncation Configuration",
    fields: [
      { key: "anti_truncation_max_attempts", label: "Anti-Truncation Max Attempts", type: "number", note: "Maximum resume attempts (1-10) when output truncation is detected" },
    ],
  },
  {
    title: "Sensitive Word Filtering",
    fields: [
      { key: "sensitive_words_enabled", label: "Enable Sensitive Word Filter", type: "bool", note: "Filters sensitive words from prompts to prevent upstream blocking" },
      { key: "sensitive_words", label: "Sensitive Words List", type: "textarea", showIf: (cfg) => !!cfg.sensitive_words_enabled, note: "One word or phrase per line, case-insensitive. Replaces matches with empty strings.", placeholder: "Example:\nsensitive word\nanother phrase" },
    ],
  },
  {
    title: "Model Serving & Visibility",
    fields: [
      {
        key: "visible_models",
        label: "Models to Show (/v1/models)",
        type: "textarea",
        note: "List of model IDs to show in /v1/models and /v1beta/models. Leave empty to automatically show all upstream models. One model ID per line.",
        placeholder: "Example:\nclaude-sonnet-4-6\nclaude-opus-4-6\ngemini-3.8-flash-high\ngpt-oss-120b-medium",
      },
      {
        key: "model_rewrite_enabled",
        label: "Enable Model Name Rewriting & Aliasing",
        type: "bool",
        note: "Map incoming requested model names to target upstream models for serving. Useful for clients expecting gpt-4o, claude-3-7-sonnet, etc.",
      },
      {
        key: "model_rewrite_rules",
        label: "Rewrite & Serving Alias Rules",
        type: "rules",
        showIf: (cfg) => !!cfg.model_rewrite_enabled,
        note: "Incoming regex pattern → Target model name. Matches are replaced in order from top to bottom.",
      },
    ],
  },
  {
    title: "Request Logs",
    fields: [
      { key: "usage_log_enabled", label: "Enable Request Logs", type: "bool", note: "Records each API request (model, credential, token usage, latency, status) into the local database." },
      { key: "request_log_enabled", label: "Enable Detailed Body Logs", type: "bool", note: "Save full request body to disk when specific error codes occur for debugging" },
      { key: "request_log_error_codes", label: "Detailed Log Error Codes", type: "csv_numbers", showIf: (cfg) => !!cfg.request_log_enabled, note: "Comma-separated status codes to trigger full body logging (e.g. 400, 429)", placeholder: "400, 429" },
    ],
  },
];

export default function Config() {
  const [config, setConfig] = useState({});
  const [envLocked, setEnvLocked] = useState([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState(null);

  useEffect(() => {
    loadConfig();
  }, []);

  const loadConfig = async ({ silent = false } = {}) => {
    setLoading(true);
    try {
      const res = await api.get("/config/get");
      setConfig(res.data.config || {});
      setEnvLocked(res.data.env_locked || []);
      if (!silent) setMessage(null);
    } catch (err) {
      setMessage({ type: "error", text: err.response?.data?.detail || "Failed to load configuration" });
    } finally {
      setLoading(false);
    }
  };

  const saveConfig = async () => {
    setSaving(true);
    try {
      const res = await api.post("/config/save", { config });
      setMessage({ type: "success", text: res.data.message || "Configuration saved successfully" });
      
      await loadConfig({ silent: true });
    } catch (err) {
      setMessage({ type: "error", text: err.response?.data?.detail || "Failed to save configuration" });
    } finally {
      setSaving(false);
    }
  };

  const setField = (key, value) => {
    setConfig((prev) => ({ ...prev, [key]: value }));
  };

  // Mirror & Official Endpoint Presets
  const MIRROR_BASE = "https://gcli-api.sukaka.top";
  const OFFICIAL_ENDPOINTS = {
    oauth_proxy_url: "https://oauth2.googleapis.com",
    googleapis_proxy_url: "https://www.googleapis.com",
    antigravity_api_url: "https://daily-cloudcode-pa.googleapis.com",
  };
  const MIRROR_PATHS = {
    oauth_proxy_url: "/oauth2",
    googleapis_proxy_url: "/googleapis",
    antigravity_api_url: "/daily-cloudcode-pa",
  };

  const useMirrorUrls = () => {
    setConfig((prev) => {
      const next = { ...prev };
      for (const [key, path] of Object.entries(MIRROR_PATHS)) {
        if (!envLocked.includes(key)) next[key] = MIRROR_BASE + path;
      }
      return next;
    });
  };

  const restoreOfficialUrls = () => {
    setConfig((prev) => {
      const next = { ...prev };
      for (const [key, url] of Object.entries(OFFICIAL_ENDPOINTS)) {
        if (!envLocked.includes(key)) next[key] = url;
      }
      return next;
    });
  };

  const renderField = (field) => {
    const isLocked = envLocked.includes(field.key);
    const value = config[field.key] ?? "";

    if (isLocked) {
      return (
        <div className="text-xs text-amber-400/80 bg-amber-500/10 border border-amber-500/20 rounded-lg px-3 py-2">
          This setting is locked by environment variable. Edit .env and restart to apply.
        </div>
      );
    }

    if (field.type === "bool") {
      return (
        <label className="flex items-center gap-3 cursor-pointer select-none">
          <button
            type="button"
            onClick={() => setField(field.key, !value)}
            className={`relative w-11 h-6 rounded-full transition-colors ${
              value ? "bg-purple-600" : "bg-dark-600"
            }`}
          >
            <span
              className={`absolute top-0.5 left-0.5 w-5 h-5 bg-white rounded-full transition-transform ${
                value ? "translate-x-5" : ""
              }`}
            />
          </button>
          <span className="text-sm text-gray-300">{value ? "Enabled" : "Disabled"}</span>
        </label>
      );
    }

    if (field.type === "select") {
      let options = field.options || [];
      if (value && !options.some((o) => o.value === value)) {
        options = [...options, { value, label: `${value} (Custom)` }];
      }
      const current = options.find((o) => o.value === value) || options[0];
      return (
        <div className="space-y-2">
          <Select
            value={current?.value ?? ""}
            onChange={(v) => setField(field.key, v)}
            options={options}
            className="w-full sm:w-80"
          />
          {field.descriptions && current && (
            <div className="bg-dark-800/60 border border-dark-600 rounded-lg p-3 text-xs sm:text-sm text-gray-300 leading-relaxed">
              {field.descriptions[current.value]}
            </div>
          )}
        </div>
      );
    }

    if (field.type === "csv_numbers") {
      return (
        <CsvNumbersField
          value={value}
          onChange={(v) => setField(field.key, v)}
          placeholder={field.placeholder}
        />
      );
    }

    if (field.type === "rules") {
      const rules = Array.isArray(value)
        ? value.map((r) => (Array.isArray(r) ? [String(r[0] ?? ""), String(r[1] ?? "")] : ["", ""]))
        : [];
      const updateRule = (idx, col, val) => {
        const next = rules.map((r) => [...r]);
        next[idx][col] = val;
        setField(field.key, next);
      };
      const removeRule = (idx) => {
        setField(field.key, rules.filter((_, i) => i !== idx));
      };
      const addRule = () => {
        setField(field.key, [...rules, ["", ""]]);
      };
      const addPresetRules = () => {
        const presets = [
          ["^gpt-4o$", "gemini-2.5-pro"],
          ["^gpt-4o-mini$", "gemini-2.5-flash"],
          ["^claude-3-7-sonnet.*$", "claude-sonnet-4-6"],
          ["^claude-3-5-sonnet.*$", "claude-sonnet-4-5"],
        ];
        const existingPatterns = new Set(rules.map((r) => r[0]));
        const toAdd = presets.filter((p) => !existingPatterns.has(p[0]));
        setField(field.key, [...rules, ...toAdd]);
      };
      return (
        <div className="space-y-2">
          {rules.length === 0 && (
            <div className="text-xs text-gray-500 bg-dark-800/60 border border-dark-700 rounded-lg px-3 py-2">
              No rules configured. Click below to add one or use quick presets.
            </div>
          )}
          {rules.map((rule, idx) => (
            <div key={idx} className="flex items-center gap-2">
              <input
                type="text"
                value={rule[0]}
                onChange={(e) => updateRule(idx, 0, e.target.value)}
                placeholder="Regex match, e.g. ^gpt-4$"
                className="flex-1 min-w-0 px-3 py-2 bg-dark-800 border border-dark-600 rounded-lg text-sm text-white placeholder-gray-500 font-mono"
              />
              <span className="text-gray-500 text-sm shrink-0">→</span>
              <input
                type="text"
                value={rule[1]}
                onChange={(e) => updateRule(idx, 1, e.target.value)}
                placeholder="Target model, e.g. gemini-2.5-pro"
                className="flex-1 min-w-0 px-3 py-2 bg-dark-800 border border-dark-600 rounded-lg text-sm text-white placeholder-gray-500 font-mono"
              />
              <button
                type="button"
                onClick={() => removeRule(idx)}
                className="shrink-0 p-2 text-gray-500 hover:text-red-400 transition-colors"
                title="Delete this rule"
              >
                <Trash2 size={16} />
              </button>
            </div>
          ))}
          <div className="flex items-center gap-2 pt-1">
            <button
              type="button"
              onClick={addRule}
              className="btn btn-secondary text-xs px-3 py-1.5 flex items-center gap-1.5"
            >
              <Plus size={14} />
              Add Rule
            </button>
            <button
              type="button"
              onClick={addPresetRules}
              className="btn btn-secondary text-xs px-3 py-1.5 flex items-center gap-1.5 text-purple-400 border-purple-500/30 hover:border-purple-500/60"
            >
              + Add Common Aliases
            </button>
          </div>
        </div>
      );
    }

    if (field.type === "textarea") {
      return (
        <LinesField
          value={value}
          onChange={(v) => setField(field.key, v)}
          placeholder={field.placeholder}
        />
      );
    }

    return (
      <input
        type={field.type}
        value={value}
        onChange={(e) =>
          setField(
            field.key,
            field.type === "number" ? Number(e.target.value) : e.target.value
          )
        }
        placeholder={field.placeholder}
        className="w-full px-3 py-2 bg-dark-800 border border-dark-600 rounded-lg text-sm text-white placeholder-gray-500"
      />
    );
  };

  return (
    <div className="space-y-6">
      {}
      <div className="sticky top-0 z-30 -mx-1 px-1 py-2 bg-dark-950/95 backdrop-blur border-b border-dark-700 rounded-lg">
        <div className="flex items-center gap-2">
        <button
          onClick={loadConfig}
          disabled={loading}
          className="btn btn-secondary text-sm flex items-center gap-1.5"
        >
          <RefreshCw size={15} className={loading ? "animate-spin" : ""} />
          Reload Configuration
        </button>
        <button
          onClick={saveConfig}
          disabled={saving || loading}
          className="btn btn-primary text-sm flex items-center gap-1.5 disabled:opacity-50"
        >
          {saving ? <Loader2 size={15} className="animate-spin" /> : <Save size={15} />}
          {saving ? "Saving..." : "Save Configuration"}
        </button>
        </div>
      </div>

      {}
      {message && (
        <Modal type={message.type} title={message.type === "success" ? "Success" : "Operation Failed"} onClose={() => setMessage(null)}>
          <span className="whitespace-pre-wrap break-all">{message.text}</span>
        </Modal>
      )}

      {loading ? (
        <div className="flex items-center justify-center py-12">
          <Loader2 className="animate-spin text-purple-500" size={28} />
        </div>
      ) : (
        <div className="space-y-6">
          {CONFIG_GROUPS.map((group) => (
            <div key={group.title} className="card">
              <h3 className="text-base font-semibold text-purple-400 mb-4 border-b border-dark-700 pb-3">
                {group.title}
              </h3>

              {group.title === "Endpoint Configuration" && (
                <div className="flex flex-wrap gap-2 mb-4">
                  <button
                    type="button"
                    onClick={useMirrorUrls}
                    className="btn btn-secondary text-xs px-3 py-1.5"
                  >
                    🚀 Use Mirror Presets
                  </button>
                  <button
                    type="button"
                    onClick={restoreOfficialUrls}
                    className="btn btn-secondary text-xs px-3 py-1.5"
                  >
                    🔄 Reset to Official Endpoints
                  </button>
                </div>
              )}

              <div className="space-y-5">
                {group.fields
                  .filter((field) => !field.showIf || field.showIf(config))
                  .map((field) => (
                    <div key={field.key}>
                      <label className="block text-sm font-medium text-gray-300 mb-1.5">
                        {field.label}
                      </label>
                      {renderField(field)}
                      {field.note && (
                        <p className="text-xs text-gray-500 mt-1.5">{field.note}</p>
                      )}
                    </div>
                  ))}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
