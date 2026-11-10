import { ExternalLink, Github, Info, RefreshCw, Rocket, Zap } from "lucide-react";
import { getBasePath } from "../basePath.js";

export default function About({ version, onCheckUpdate, checkingUpdate }) {
  const apiBase = `${window.location.origin}${getBasePath()}`;
  const apiEndpoints = [
    { label: "OpenAI Format", value: `${apiBase}v1/chat/completions` },
    { label: "Gemini Format", value: `${apiBase}v1beta/models` },
    { label: "Claude Format", value: `${apiBase}v1/messages` },
  ];

  const copyText = (text) => {
    navigator.clipboard.writeText(text);
  };

  return (
    <div className="space-y-6">
      {/* API Endpoints */}
      <div className="card p-4 sm:p-6">
        <h3 className="text-sm font-medium text-gray-400 mb-3 flex items-center gap-2">
          <Zap size={16} className="text-amber-400" />
          API Endpoints (Click to Copy)
        </h3>
        <div className="space-y-2">
          {apiEndpoints.map((ep) => (
            <div key={ep.label} className="flex flex-col sm:flex-row sm:items-center gap-1 sm:gap-3 text-sm">
              <span className="text-gray-500 sm:w-32 flex-shrink-0">{ep.label}</span>
              <code
                className="bg-dark-800 border border-dark-600 rounded px-2 py-1 text-xs text-gray-300 cursor-pointer hover:text-white break-all"
                onClick={() => copyText(ep.value)}
              >
                {ep.value}
              </code>
            </div>
          ))}
        </div>
      </div>

      {/* Project Overview */}
      <div className="card">
        <div className="flex items-center gap-3 mb-4">
          <div className="inline-flex items-center justify-center w-12 h-12 rounded-2xl bg-purple-600/20">
            <Rocket className="w-7 h-7 text-purple-400" />
          </div>
          <div>
            <h2 className="text-2xl font-bold text-white">AGY2API</h2>
            <p className="text-gray-400 text-sm">
              {version?.version ? `Version: ${version.version}` : "Antigravity API Proxy"}
            </p>
          </div>
        </div>
        <p className="text-gray-400 text-sm leading-relaxed">
          Proxy gateway converting Google Antigravity into OpenAI, Gemini, and Anthropic Claude compatible API formats.
          Features multi-account pooling, automatic token rotation, per-model 429 cooldowns, and credential management.
        </p>
        <div className="flex flex-wrap items-center gap-4 mt-4">
          <a
            href="https://github.com/su-kaka/agy2api"
            target="_blank"
            rel="noopener noreferrer"
            className="text-purple-400 hover:text-purple-300 text-sm flex items-center gap-1.5"
          >
            <Github size={15} />
            GitHub Repository
            <ExternalLink size={12} />
          </a>
          <button
            onClick={onCheckUpdate}
            disabled={checkingUpdate}
            className="text-blue-400 hover:text-blue-300 text-sm flex items-center gap-1.5 disabled:opacity-50"
          >
            {checkingUpdate ? (
              <Info size={14} className="animate-pulse" />
            ) : (
              <RefreshCw size={14} />
            )}
            {checkingUpdate ? "Checking..." : "Check Updates"}
          </button>
          {version?.check_update && version?.has_update && (
            <span className="text-emerald-400 text-sm flex items-center gap-1.5">
              <Info size={14} />
              New version available: {version.latest_version}
            </span>
          )}
          {version?.check_update && version?.has_update === false && (
            <span className="text-gray-400 text-sm flex items-center gap-1.5">
              <Info size={14} />
              Up to date
            </span>
          )}
        </div>
      </div>

      {/* Core Features */}
      <div className="card">
        <h3 className="text-lg font-semibold text-blue-400 mb-4">✨ Core Features</h3>
        <div className="grid md:grid-cols-2 gap-4 text-sm">
          <div className="space-y-3">
            <div className="text-gray-300">
              <span className="text-amber-400">⚡</span>{" "}
              <strong>Multi-Format Compatible:</strong> OpenAI, Gemini, and Anthropic Claude formats
            </div>
            <div className="text-gray-300">
              <span className="text-cyan-400">🔄</span>{" "}
              <strong>Auto-Rotation:</strong> Load balancing with smart skip for cooled or disabled credentials
            </div>
            <div className="text-gray-300">
              <span className="text-emerald-400">🛡️</span>{" "}
              <strong>Auto-Ban:</strong> Disables credentials automatically on specific error codes
            </div>
            <div className="text-gray-300">
              <span className="text-purple-400">❄️</span>{" "}
              <strong>Model-Level Cooldown:</strong> Automatic backoff cooldown on 429 errors
            </div>
          </div>
          <div className="space-y-3">
            <div className="text-gray-300">
              <span className="text-red-400">✂️</span>{" "}
              <strong>Anti-Truncation:</strong> Auto-resumes responses truncated by upstream limits
            </div>
            <div className="text-gray-300">
              <span className="text-emerald-400">📊</span>{" "}
              <strong>Live Monitoring:</strong> Credential status, error logging, and quota inspection
            </div>
            <div className="text-gray-300">
              <span className="text-gray-400">📱</span>{" "}
              <strong>Responsive UI:</strong> Desktop and mobile friendly
            </div>
          </div>
        </div>
      </div>

      {/* Version Information */}
      {version && (
        <div className="card">
          <h3 className="text-base font-semibold text-gray-300 mb-3">Version Details</h3>
          <div className="grid grid-cols-2 gap-3 text-sm">
            <div className="text-gray-500">
              Current Version: <span className="text-gray-300">{version.version || "-"}</span>
            </div>
            <div className="text-gray-500">
              Build Hash: <span className="text-gray-300">{version.full_hash || "-"}</span>
            </div>
            <div className="text-gray-500">
              Changelog: <span className="text-gray-300">{version.message || "-"}</span>
            </div>
            <div className="text-gray-500">
              Build Date: <span className="text-gray-300">{version.date || "-"}</span>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
