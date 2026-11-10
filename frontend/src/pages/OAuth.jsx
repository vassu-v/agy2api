import { Check, ChevronDown, Copy, ExternalLink, Loader2, Rocket } from "lucide-react";
import { useState } from "react";
import Modal from "../components/Modal";
import api from "../api";

export default function OAuth() {
  const [loading, setLoading] = useState(false);
  const [fetching, setFetching] = useState(false);
  const [authUrl, setAuthUrl] = useState("");
  const [callbackUrl, setCallbackUrl] = useState("");
  const [showCallbackInput, setShowCallbackInput] = useState(false);
  const [credsContent, setCredsContent] = useState("");
  const [message, setMessage] = useState(null);
  const [copied, setCopied] = useState(false);

  const startAuth = async () => {
    setLoading(true);
    setMessage(null);
    setAuthUrl("");
    setCredsContent("");

    try {
      const res = await api.post("/auth/start", {});
      setAuthUrl(res.data.auth_url);
      setMessage({ type: "info", text: "Auth URL generated! Please complete Google sign-in in the newly opened window." });
    } catch (err) {
      setMessage({
        type: "error",
        text: err.response?.data?.detail || "Failed to generate authorization URL",
      });
    } finally {
      setLoading(false);
    }
  };

  const fetchCredentials = async () => {
    setFetching(true);
    try {
      const res = await api.post("/auth/callback", {});
      setCredsContent(JSON.stringify(res.data.credentials, null, 2));
      setMessage({
        type: "success",
        text: `Authentication successful! Credentials saved for: ${res.data.email || "Google Account"}`,
      });
    } catch (err) {
      setMessage({
        type: "error",
        text: err.response?.data?.detail || "Failed to retrieve credentials. Please verify authorization was completed.",
      });
    } finally {
      setFetching(false);
    }
  };

  const processCallbackUrl = async () => {
    if (!callbackUrl.trim()) {
      setMessage({ type: "error", text: "Please paste the full callback URL" });
      return;
    }
    setFetching(true);
    try {
      const res = await api.post("/auth/callback-url", {
        callback_url: callbackUrl.trim(),
      });
      setCredsContent(JSON.stringify(res.data.credentials, null, 2));
      setMessage({
        type: "success",
        text: `Authentication successful! Credentials saved for: ${res.data.email || "Google Account"}`,
      });
    } catch (err) {
      setMessage({
        type: "error",
        text: err.response?.data?.detail || "Failed to parse credentials from callback URL",
      });
    } finally {
      setFetching(false);
    }
  };

  const copyUrl = () => {
    navigator.clipboard.writeText(authUrl);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  };

  const downloadCreds = () => {
    const blob = new Blob([credsContent], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "antigravity-credentials.json";
    a.click();
    URL.revokeObjectURL(url);
  };

  return (
    <div className="space-y-6">
      {/* Info Card */}
      <div className="bg-gradient-to-br from-purple-900/30 to-purple-800/10 border border-purple-700/30 rounded-xl p-5">
        <div className="flex items-center gap-2 mb-2">
          <Rocket className="w-5 h-5 text-purple-400" />
          <h3 className="text-lg font-semibold text-purple-400">Antigravity OAuth Login</h3>
        </div>
        <p className="text-sm text-gray-400">
          Authorize your Google account to grant access to your Antigravity subscription limits. 
          Upon authorization, the system retrieves your Project ID and subscription tier, automatically storing credentials.
        </p>
      </div>

      {/* Notification Modal */}
      {message && (
        <Modal
          type={message.type}
          title={message.type === "success" ? "Success" : message.type === "error" ? "Error" : "Notice"}
          onClose={() => setMessage(null)}
        >
          <span className="whitespace-pre-wrap break-all">{message.text}</span>
        </Modal>
      )}

      {/* Start Button */}
      <button
        onClick={startAuth}
        disabled={loading}
        className="btn btn-primary flex items-center gap-2 disabled:opacity-50"
      >
        {loading ? (
          <Loader2 className="animate-spin" size={18} />
        ) : (
          <Rocket size={18} />
        )}
        Generate Antigravity Auth Link
      </button>

      {/* Auth URL Section */}
      {authUrl && (
        <div className="card space-y-4">
          <h3 className="font-semibold">Authorization Link</h3>
          <div className="bg-dark-800 border border-purple-500/30 rounded-lg p-4 flex items-center justify-between gap-3">
            <a
              href={authUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="text-purple-400 hover:text-purple-300 break-all text-sm flex items-center gap-1"
            >
              Click here to sign in with Google <ExternalLink size={14} />
            </a>
            <button
              onClick={copyUrl}
              className="btn btn-secondary text-xs px-3 py-1.5 flex items-center gap-1 flex-shrink-0"
            >
              {copied ? <Check size={14} /> : <Copy size={14} />}
              {copied ? "Copied" : "Copy Link"}
            </button>
          </div>

          <div className="bg-blue-500/10 border border-blue-500/30 rounded-lg p-4 text-sm text-gray-400">
            <p className="font-medium text-blue-400 mb-2">Instructions</p>
            <ol className="list-decimal list-inside space-y-1">
              <li>Click the link above to log in to your Google Account with the Antigravity subscription.</li>
              <li>After consenting, your browser will redirect to a localhost callback page.</li>
              <li>Close the Google OAuth tab and return to this page.</li>
              <li>Click the <strong>"Save Credentials"</strong> button below to complete setup.</li>
            </ol>
          </div>

          {/* Fallback Callback URL */}
          <div className="border border-dark-600 rounded-lg">
            <button
              onClick={() => setShowCallbackInput(!showCallbackInput)}
              className="w-full flex items-center justify-between px-4 py-3 text-sm text-gray-400 hover:text-gray-200"
            >
              <span>🚀 Callback port blocked? Paste callback URL manually</span>
              <ChevronDown
                size={16}
                className={`transition-transform ${showCallbackInput ? "rotate-180" : ""}`}
              />
            </button>
            {showCallbackInput && (
              <div className="px-4 pb-4 space-y-3">
                <p className="text-xs text-gray-500">
                  If the browser fails to reach localhost:11451 after logging in, copy the full URL from the browser's address bar and paste it here:
                </p>
                <textarea
                  value={callbackUrl}
                  onChange={(e) => setCallbackUrl(e.target.value)}
                  placeholder="http://localhost:11451/?state=xxx&code=xxx&scope=xxx..."
                  className="w-full px-3 py-2 bg-dark-800 border border-dark-600 rounded-lg text-sm text-white placeholder-gray-500"
                  rows={3}
                />
                <button
                  onClick={processCallbackUrl}
                  disabled={fetching}
                  className="btn btn-secondary text-sm flex items-center gap-2 disabled:opacity-50"
                >
                  {fetching && <Loader2 className="animate-spin" size={14} />}
                  Parse Credentials from URL
                </button>
              </div>
            )}
          </div>

          {/* Fetch Credentials Button */}
          <button
            onClick={fetchCredentials}
            disabled={fetching}
            className="btn btn-primary flex items-center gap-2 disabled:opacity-50"
          >
            {fetching && <Loader2 className="animate-spin" size={18} />}
            Save & Activate Credentials
          </button>
        </div>
      )}

      {/* Credentials Output */}
      {credsContent && (
        <div className="card space-y-4">
          <h3 className="font-semibold">Active Credential Data</h3>
          <pre className="bg-dark-800 border border-dark-600 rounded-lg p-4 text-xs text-gray-300 overflow-x-auto max-h-96 overflow-y-auto">
            {credsContent}
          </pre>
          <button onClick={downloadCreds} className="btn btn-secondary text-sm">
            Download JSON File
          </button>
        </div>
      )}
    </div>
  );
}
