import {
  AlertCircle,
  Check,
  CheckCircle,
  Copy,
  Edit2,
  Eye,
  EyeOff,
  Key,
  Plus,
  RefreshCw,
  Search,
  Shield,
  Sliders,
  Trash2,
  X,
  Zap,
} from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import api from "../api";

const PRESET_MODELS = [
  "gemini-2.5-pro",
  "gemini-2.5-flash",
  "claude-sonnet-4-6",
  "claude-opus-4-6-thinking",
  "gpt-oss-120b-medium",
];

export default function ApiKeys() {
  const [keys, setKeys] = useState([]);
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState("");
  const [copiedKey, setCopiedKey] = useState(null);
  const [revealedKeys, setRevealedKeys] = useState(new Set());
  const [message, setMessage] = useState(null);

  // Modals state
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [editingKey, setEditingKey] = useState(null);
  const [justCreatedKey, setJustCreatedKey] = useState(null);

  // Form state for Create / Edit
  const [formData, setFormData] = useState({
    name: "",
    description: "",
    key: "",
    rpm_limit: 0,
    daily_limit: 0,
    token_limit: 0,
    expiryOption: "never", // never, 7d, 30d, 90d, custom
    expires_at: null,
    restrictModels: false,
    models_allowed: [],
    is_active: true,
  });

  const showToast = (type, text) => {
    setMessage({ type, text });
    setTimeout(() => setMessage(null), 4000);
  };

  const fetchKeys = useCallback(async () => {
    setLoading(true);
    try {
      const res = await api.get("/keys");
      setKeys(res.data || []);
    } catch (err) {
      showToast("error", err.response?.data?.detail || "Failed to fetch API keys");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchKeys();
  }, [fetchKeys]);

  const copyToClipboard = (text, id) => {
    navigator.clipboard.writeText(text);
    setCopiedKey(id);
    setTimeout(() => setCopiedKey(null), 2000);
  };

  const toggleReveal = (id) => {
    setRevealedKeys((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const openCreateModal = async () => {
    try {
      const res = await api.get("/keys/generate/candidate");
      setFormData({
        name: "",
        description: "",
        key: res.data?.key || "",
        rpm_limit: 0,
        daily_limit: 0,
        token_limit: 0,
        expiryOption: "never",
        expires_at: null,
        restrictModels: false,
        models_allowed: [],
        is_active: true,
      });
      setShowCreateModal(true);
    } catch {
      const array = new Uint8Array(16);
      window.crypto.getRandomValues(array);
      const hex = Array.from(array, (byte) => byte.toString(16).padStart(2, "0")).join("");
      setFormData({
        name: "",
        description: "",
        key: `sk-agy-${hex}`,
        rpm_limit: 0,
        daily_limit: 0,
        token_limit: 0,
        expiryOption: "never",
        expires_at: null,
        restrictModels: false,
        models_allowed: [],
        is_active: true,
      });
      setShowCreateModal(true);
    }
  };

  const handleRegenerateKey = async () => {
    try {
      const res = await api.get("/keys/generate/candidate");
      setFormData((prev) => ({ ...prev, key: res.data?.key || "" }));
    } catch {
      // fallback
    }
  };

  const openEditModal = (k) => {
    let expiryOpt = "never";
    if (k.expires_at) {
      expiryOpt = "custom";
    }
    setFormData({
      id: k.id,
      name: k.name,
      description: k.description || "",
      key: k.key,
      rpm_limit: k.rpm_limit || 0,
      daily_limit: k.daily_limit || 0,
      token_limit: k.token_limit || 0,
      expiryOption: expiryOpt,
      expires_at: k.expires_at,
      restrictModels: Array.isArray(k.models_allowed) && k.models_allowed.length > 0,
      models_allowed: k.models_allowed || [],
      is_active: k.is_active,
    });
    setEditingKey(k);
  };

  const computeExpiresAt = (option, customVal) => {
    const now = Math.floor(Date.now() / 1000);
    if (option === "7d") return now + 7 * 86400;
    if (option === "30d") return now + 30 * 86400;
    if (option === "90d") return now + 90 * 86400;
    if (option === "custom" && customVal) return customVal;
    return null;
  };

  const submitCreate = async (e) => {
    e.preventDefault();
    if (!formData.name.trim()) {
      showToast("error", "Key name is required");
      return;
    }

    const payload = {
      name: formData.name.trim(),
      description: formData.description.trim(),
      key: formData.key.trim() || undefined,
      rpm_limit: Number(formData.rpm_limit) || 0,
      daily_limit: Number(formData.daily_limit) || 0,
      token_limit: Number(formData.token_limit) || 0,
      expires_at: computeExpiresAt(formData.expiryOption, formData.expires_at),
      models_allowed: formData.restrictModels ? formData.models_allowed : [],
      is_active: formData.is_active,
    };

    try {
      const res = await api.post("/keys", payload);
      setShowCreateModal(false);
      setJustCreatedKey(res.data);
      fetchKeys();
      showToast("success", `API key "${res.data.name}" created successfully`);
    } catch (err) {
      showToast("error", err.response?.data?.detail || "Failed to create API key");
    }
  };

  const submitEdit = async (e) => {
    e.preventDefault();
    if (!formData.name.trim()) {
      showToast("error", "Key name is required");
      return;
    }

    const payload = {
      name: formData.name.trim(),
      description: formData.description.trim(),
      rpm_limit: Number(formData.rpm_limit) || 0,
      daily_limit: Number(formData.daily_limit) || 0,
      token_limit: Number(formData.token_limit) || 0,
      expires_at: computeExpiresAt(formData.expiryOption, formData.expires_at),
      models_allowed: formData.restrictModels ? formData.models_allowed : [],
      is_active: formData.is_active,
    };

    try {
      await api.put(`/keys/${formData.id}`, payload);
      setEditingKey(null);
      fetchKeys();
      showToast("success", "API key updated successfully");
    } catch (err) {
      showToast("error", err.response?.data?.detail || "Failed to update API key");
    }
  };

  const toggleKeyActive = async (k) => {
    try {
      await api.put(`/keys/${k.id}`, { is_active: !k.is_active });
      setKeys((prev) =>
        prev.map((item) =>
          item.id === k.id ? { ...item, is_active: !item.is_active } : item
        )
      );
      showToast("success", `Key "${k.name}" is now ${!k.is_active ? "Active" : "Disabled"}`);
    } catch (err) {
      showToast("error", err.response?.data?.detail || "Failed to toggle status");
    }
  };

  const deleteKey = async (k) => {
    if (!window.confirm(`Are you sure you want to delete API key "${k.name}"? This action cannot be undone.`)) {
      return;
    }
    try {
      await api.delete(`/keys/${k.id}`);
      fetchKeys();
      showToast("success", `API key "${k.name}" deleted`);
    } catch (err) {
      showToast("error", err.response?.data?.detail || "Failed to delete API key");
    }
  };

  const toggleModelSelection = (m) => {
    setFormData((prev) => {
      const current = prev.models_allowed || [];
      if (current.includes(m)) {
        return { ...prev, models_allowed: current.filter((x) => x !== m) };
      } else {
        return { ...prev, models_allowed: [...current, m] };
      }
    });
  };

  // Filtered keys
  const filteredKeys = keys.filter((k) => {
    const q = search.toLowerCase();
    return (
      k.name?.toLowerCase().includes(q) ||
      k.description?.toLowerCase().includes(q) ||
      k.masked_key?.toLowerCase().includes(q) ||
      k.key?.toLowerCase().includes(q)
    );
  });

  // Summary stats
  const totalKeys = keys.length;
  const activeKeys = keys.filter((k) => k.is_active).length;
  const totalRequests = keys.reduce((acc, k) => acc + (k.total_requests || 0), 0);
  const totalTokens = keys.reduce((acc, k) => acc + (k.total_tokens || 0), 0);

  const formatTokens = (num) => {
    if (!num) return "0";
    if (num >= 1_000_000) return `${(num / 1_000_000).toFixed(1)}M`;
    if (num >= 1_000) return `${(num / 1_000).toFixed(1)}k`;
    return num.toLocaleString();
  };

  const formatRelativeTime = (ts) => {
    if (!ts) return "Never";
    const diff = Math.floor(Date.now() / 1000 - ts);
    if (diff < 60) return "Just now";
    if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
    if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
    return new Date(ts * 1000).toLocaleDateString();
  };

  return (
    <div className="space-y-6">
      {/* Toast Notification */}
      {message && (
        <div
          className={`fixed top-4 right-4 z-50 flex items-center gap-2 px-4 py-3 rounded-lg shadow-lg text-sm border ${
            message.type === "success"
              ? "bg-green-950 border-green-600 text-green-200"
              : "bg-red-950 border-red-600 text-red-200"
          }`}
        >
          {message.type === "success" ? <CheckCircle size={18} /> : <AlertCircle size={18} />}
          <span>{message.text}</span>
        </div>
      )}

      {/* Header and Quick Stats */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4">
        <div>
          <h2 className="text-xl font-bold text-white flex items-center gap-2">
            <Key className="text-purple-400" size={24} />
            API Keys & Rate Limiting
          </h2>
          <p className="text-sm text-gray-400 mt-1">
            Generate and manage access keys with fine-grained rate limits, token quotas, and model restrictions.
          </p>
        </div>
        <div className="flex items-center gap-3">
          <button
            onClick={fetchKeys}
            disabled={loading}
            className="btn btn-secondary flex items-center gap-1.5 text-xs sm:text-sm"
            title="Refresh keys"
          >
            <RefreshCw size={15} className={loading ? "animate-spin" : ""} />
            Refresh
          </button>
          <button
            onClick={openCreateModal}
            className="btn btn-primary flex items-center gap-1.5 text-xs sm:text-sm shadow-lg shadow-purple-600/20"
          >
            <Plus size={16} />
            Create API Key
          </button>
        </div>
      </div>

      {/* Stats Cards */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        <div className="card p-4">
          <div className="text-gray-400 text-xs font-medium uppercase tracking-wider">Total Keys</div>
          <div className="text-2xl font-bold text-white mt-1">{totalKeys}</div>
        </div>
        <div className="card p-4">
          <div className="text-gray-400 text-xs font-medium uppercase tracking-wider">Active Keys</div>
          <div className="text-2xl font-bold text-green-400 mt-1">{activeKeys}</div>
        </div>
        <div className="card p-4">
          <div className="text-gray-400 text-xs font-medium uppercase tracking-wider">Requests Served</div>
          <div className="text-2xl font-bold text-purple-400 mt-1">{totalRequests.toLocaleString()}</div>
        </div>
        <div className="card p-4">
          <div className="text-gray-400 text-xs font-medium uppercase tracking-wider">Tokens Consumed</div>
          <div className="text-2xl font-bold text-blue-400 mt-1">{formatTokens(totalTokens)}</div>
        </div>
      </div>

      {/* Search Bar */}
      <div className="flex items-center gap-3">
        <div className="relative flex-1">
          <Search size={16} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-500" />
          <input
            type="text"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search by key name, token, or description..."
            className="w-full pl-9 pr-4 py-2 bg-dark-900 border border-dark-700 rounded-lg text-sm text-white placeholder-gray-500 focus:outline-none focus:border-purple-500"
          />
        </div>
      </div>

      {/* API Keys Table */}
      <div className="table-container bg-dark-900 border border-dark-700 rounded-xl overflow-hidden">
        <table className="w-full text-left text-sm text-gray-300">
          <thead className="bg-dark-800/60 text-xs text-gray-400 uppercase tracking-wider border-b border-dark-700">
            <tr>
              <th className="px-4 py-3">Key & Label</th>
              <th className="px-4 py-3">Status</th>
              <th className="px-4 py-3">Rate Limits</th>
              <th className="px-4 py-3">Usage</th>
              <th className="px-4 py-3">Allowed Models</th>
              <th className="px-4 py-3">Expiration</th>
              <th className="px-4 py-3 text-right">Actions</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-dark-700">
            {loading ? (
              <tr>
                <td colSpan="7" className="text-center py-8 text-gray-500">
                  <RefreshCw className="animate-spin inline-block mr-2" size={16} />
                  Loading API keys...
                </td>
              </tr>
            ) : filteredKeys.length === 0 ? (
              <tr>
                <td colSpan="7" className="text-center py-10 text-gray-500">
                  <Key size={32} className="mx-auto mb-2 opacity-30" />
                  {search ? "No matching API keys found" : "No API keys created yet. Click 'Create API Key' to get started."}
                </td>
              </tr>
            ) : (
              filteredKeys.map((k) => {
                const isRevealed = revealedKeys.has(k.id);
                const isExpired = k.expires_at && Date.now() / 1000 >= k.expires_at;

                return (
                  <tr key={k.id} className="hover:bg-dark-800/40 transition-colors">
                    {/* Key & Label */}
                    <td className="px-4 py-3">
                      <div className="font-semibold text-white flex items-center gap-2">
                        {k.name}
                      </div>
                      {k.description && (
                        <div className="text-xs text-gray-500 truncate max-w-xs">{k.description}</div>
                      )}
                      <div className="flex items-center gap-1.5 mt-1 font-mono text-xs text-purple-300 bg-dark-800/80 px-2 py-0.5 rounded border border-dark-700 w-fit">
                        <span>{isRevealed ? k.key : k.masked_key || k.key}</span>
                        <button
                          onClick={() => toggleReveal(k.id)}
                          className="text-gray-400 hover:text-white p-0.5"
                          title={isRevealed ? "Hide full key" : "Reveal full key"}
                        >
                          {isRevealed ? <EyeOff size={13} /> : <Eye size={13} />}
                        </button>
                        <button
                          onClick={() => copyToClipboard(k.key, k.id)}
                          className="text-gray-400 hover:text-white p-0.5"
                          title="Copy API Key"
                        >
                          {copiedKey === k.id ? (
                            <Check size={13} className="text-green-400" />
                          ) : (
                            <Copy size={13} />
                          )}
                        </button>
                      </div>
                    </td>

                    {/* Status */}
                    <td className="px-4 py-3 whitespace-nowrap">
                      {isExpired ? (
                        <span className="inline-flex items-center gap-1 px-2 py-0.5 text-xs font-medium rounded-full bg-yellow-500/10 text-yellow-400 border border-yellow-500/20">
                          Expired
                        </span>
                      ) : k.is_active ? (
                        <span className="inline-flex items-center gap-1.5 px-2 py-0.5 text-xs font-medium rounded-full bg-green-500/10 text-green-400 border border-green-500/20">
                          <span className="w-1.5 h-1.5 rounded-full bg-green-400 animate-pulse" />
                          Active
                        </span>
                      ) : (
                        <span className="inline-flex items-center gap-1.5 px-2 py-0.5 text-xs font-medium rounded-full bg-red-500/10 text-red-400 border border-red-500/20">
                          Disabled
                        </span>
                      )}
                    </td>

                    {/* Rate Limits */}
                    <td className="px-4 py-3 whitespace-nowrap text-xs space-y-1">
                      <div>
                        <span className="text-gray-500">RPM: </span>
                        <span className={k.rpm_limit > 0 ? "text-purple-300 font-semibold" : "text-gray-400"}>
                          {k.rpm_limit > 0 ? `${k.rpm_limit} req/min` : "Unlimited"}
                        </span>
                      </div>
                      <div>
                        <span className="text-gray-500">Daily: </span>
                        <span className={k.daily_limit > 0 ? "text-blue-300 font-semibold" : "text-gray-400"}>
                          {k.daily_limit > 0 ? `${k.daily_limit.toLocaleString()} / day` : "Unlimited"}
                        </span>
                      </div>
                      <div>
                        <span className="text-gray-500">Quota: </span>
                        <span className={k.token_limit > 0 ? "text-yellow-300 font-semibold" : "text-gray-400"}>
                          {k.token_limit > 0 ? `${formatTokens(k.token_limit)} tokens` : "Unlimited"}
                        </span>
                      </div>
                    </td>

                    {/* Usage */}
                    <td className="px-4 py-3 whitespace-nowrap text-xs space-y-1">
                      <div className="text-white font-medium">
                        {(k.total_requests || 0).toLocaleString()} reqs
                      </div>
                      <div className="text-gray-400 font-mono">
                        {formatTokens(k.total_tokens || 0)} tokens
                      </div>
                      <div className="text-[10px] text-gray-500">
                        Last used: {formatRelativeTime(k.last_used_at)}
                      </div>
                    </td>

                    {/* Allowed Models */}
                    <td className="px-4 py-3 text-xs max-w-xs">
                      {!k.models_allowed || k.models_allowed.length === 0 ? (
                        <span className="px-2 py-0.5 rounded bg-dark-700 text-gray-300 border border-dark-600 text-[11px]">
                          All Models
                        </span>
                      ) : (
                        <div className="flex flex-wrap gap-1">
                          {k.models_allowed.map((m) => (
                            <span
                              key={m}
                              className="px-1.5 py-0.5 rounded bg-purple-900/30 text-purple-300 border border-purple-800 text-[10px] font-mono"
                            >
                              {m}
                            </span>
                          ))}
                        </div>
                      )}
                    </td>

                    {/* Expiration */}
                    <td className="px-4 py-3 whitespace-nowrap text-xs text-gray-400">
                      {k.expires_at ? new Date(k.expires_at * 1000).toLocaleDateString() : "Never"}
                    </td>

                    {/* Actions */}
                    <td className="px-4 py-3 whitespace-nowrap text-right text-xs">
                      <div className="flex items-center justify-end gap-1.5">
                        <button
                          onClick={() => toggleKeyActive(k)}
                          className={`px-2 py-1 rounded text-xs font-medium transition-colors ${
                            k.is_active
                              ? "bg-dark-800 hover:bg-dark-700 text-yellow-400"
                              : "bg-green-900/30 hover:bg-green-900/50 text-green-300"
                          }`}
                          title={k.is_active ? "Disable Key" : "Enable Key"}
                        >
                          {k.is_active ? "Disable" : "Enable"}
                        </button>
                        <button
                          onClick={() => openEditModal(k)}
                          className="p-1.5 hover:bg-dark-700 rounded text-gray-400 hover:text-white"
                          title="Edit limits and settings"
                        >
                          <Edit2 size={14} />
                        </button>
                        <button
                          onClick={() => deleteKey(k)}
                          className="p-1.5 hover:bg-dark-700 rounded text-gray-400 hover:text-red-400"
                          title="Delete API Key"
                        >
                          <Trash2 size={14} />
                        </button>
                      </div>
                    </td>
                  </tr>
                );
              })
            )}
          </tbody>
        </table>
      </div>

      {/* Created Key Success Modal */}
      {justCreatedKey && (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/70">
          <div className="card max-w-lg w-full p-6 border border-green-500/40 space-y-4">
            <div className="flex items-center gap-2 text-green-400 font-bold text-lg">
              <CheckCircle size={22} />
              API Key Created Successfully
            </div>
            <p className="text-sm text-gray-300">
              Your new API key <span className="font-semibold text-white">"{justCreatedKey.name}"</span> is ready. Please copy it now:
            </p>
            <div className="bg-dark-950 p-3 rounded-lg border border-dark-700 flex items-center justify-between font-mono text-sm text-purple-300">
              <span className="truncate pr-2">{justCreatedKey.key}</span>
              <button
                onClick={() => copyToClipboard(justCreatedKey.key, "modal")}
                className="btn btn-primary text-xs flex items-center gap-1.5 py-1.5 px-3 shrink-0"
              >
                {copiedKey === "modal" ? <Check size={14} /> : <Copy size={14} />}
                {copiedKey === "modal" ? "Copied!" : "Copy Key"}
              </button>
            </div>
            <p className="text-xs text-gray-400">
              You can view, edit limits, or revoke this key anytime from this dashboard.
            </p>
            <div className="text-right pt-2">
              <button
                onClick={() => setJustCreatedKey(null)}
                className="btn btn-secondary text-xs px-4 py-2"
              >
                Done
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Create API Key Modal */}
      {showCreateModal && (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/70 overflow-y-auto">
          <div className="card max-w-lg w-full p-6 space-y-4 border border-dark-700 my-8">
            <div className="flex items-center justify-between border-b border-dark-700 pb-3">
              <h3 className="font-bold text-lg text-white flex items-center gap-2">
                <Plus size={18} className="text-purple-400" />
                Create New API Key
              </h3>
              <button onClick={() => setShowCreateModal(false)} className="text-gray-400 hover:text-white">
                <X size={18} />
              </button>
            </div>

            <form onSubmit={submitCreate} className="space-y-4 text-sm">
              <div>
                <label className="block text-xs font-medium text-gray-300 mb-1">
                  Key Name / Label <span className="text-red-400">*</span>
                </label>
                <input
                  type="text"
                  required
                  placeholder="e.g. Cursor IDE, Claude Code, Alice"
                  value={formData.name}
                  onChange={(e) => setFormData({ ...formData, name: e.target.value })}
                  className="w-full px-3 py-2 bg-dark-800 border border-dark-600 rounded-lg text-white"
                />
              </div>

              <div>
                <label className="block text-xs font-medium text-gray-300 mb-1">
                  Description (Optional)
                </label>
                <input
                  type="text"
                  placeholder="e.g. Personal key for automated coding tasks"
                  value={formData.description}
                  onChange={(e) => setFormData({ ...formData, description: e.target.value })}
                  className="w-full px-3 py-2 bg-dark-800 border border-dark-600 rounded-lg text-white"
                />
              </div>

              <div>
                <div className="flex items-center justify-between mb-1">
                  <label className="text-xs font-medium text-gray-300">
                    API Key Token
                  </label>
                  <button
                    type="button"
                    onClick={handleRegenerateKey}
                    className="text-xs text-purple-400 hover:text-purple-300 flex items-center gap-1"
                  >
                    <RefreshCw size={11} /> Generate New
                  </button>
                </div>
                <input
                  type="text"
                  value={formData.key}
                  onChange={(e) => setFormData({ ...formData, key: e.target.value })}
                  className="w-full px-3 py-2 bg-dark-800 border border-dark-600 rounded-lg text-white font-mono text-xs"
                />
              </div>

              {/* Rate Limits Grid */}
              <div className="grid grid-cols-1 sm:grid-cols-3 gap-3 pt-1">
                <div>
                  <label className="block text-[11px] font-medium text-gray-300 mb-1">
                    RPM Limit (0 = Unltd)
                  </label>
                  <input
                    type="number"
                    min="0"
                    placeholder="0"
                    value={formData.rpm_limit}
                    onChange={(e) => setFormData({ ...formData, rpm_limit: e.target.value })}
                    className="w-full px-3 py-1.5 bg-dark-800 border border-dark-600 rounded-lg text-white text-xs"
                  />
                </div>
                <div>
                  <label className="block text-[11px] font-medium text-gray-300 mb-1">
                    Daily Limit (0 = Unltd)
                  </label>
                  <input
                    type="number"
                    min="0"
                    placeholder="0"
                    value={formData.daily_limit}
                    onChange={(e) => setFormData({ ...formData, daily_limit: e.target.value })}
                    className="w-full px-3 py-1.5 bg-dark-800 border border-dark-600 rounded-lg text-white text-xs"
                  />
                </div>
                <div>
                  <label className="block text-[11px] font-medium text-gray-300 mb-1">
                    Token Quota (0 = Unltd)
                  </label>
                  <input
                    type="number"
                    min="0"
                    placeholder="0"
                    value={formData.token_limit}
                    onChange={(e) => setFormData({ ...formData, token_limit: e.target.value })}
                    className="w-full px-3 py-1.5 bg-dark-800 border border-dark-600 rounded-lg text-white text-xs"
                  />
                </div>
              </div>

              {/* Expiration Presets */}
              <div>
                <label className="block text-xs font-medium text-gray-300 mb-1">Expiration</label>
                <div className="grid grid-cols-4 gap-2">
                  {[
                    { id: "never", label: "Never" },
                    { id: "7d", label: "7 Days" },
                    { id: "30d", label: "30 Days" },
                    { id: "90d", label: "90 Days" },
                  ].map((opt) => (
                    <button
                      key={opt.id}
                      type="button"
                      onClick={() => setFormData({ ...formData, expiryOption: opt.id })}
                      className={`px-3 py-1.5 text-xs rounded-lg border font-medium transition-colors ${
                        formData.expiryOption === opt.id
                          ? "bg-purple-600 text-white border-purple-500"
                          : "bg-dark-800 text-gray-400 border-dark-600 hover:text-white"
                      }`}
                    >
                      {opt.label}
                    </button>
                  ))}
                </div>
              </div>

              {/* Model Restrictions */}
              <div>
                <div className="flex items-center justify-between mb-2">
                  <label className="text-xs font-medium text-gray-300">Model Whitelist</label>
                  <label className="text-xs text-purple-400 flex items-center gap-1.5 cursor-pointer">
                    <input
                      type="checkbox"
                      checked={formData.restrictModels}
                      onChange={(e) =>
                        setFormData({ ...formData, restrictModels: e.target.checked })
                      }
                      className="rounded bg-dark-800 border-dark-600 text-purple-600"
                    />
                    Restrict models
                  </label>
                </div>
                {formData.restrictModels && (
                  <div className="space-y-2 p-3 bg-dark-800/60 rounded-lg border border-dark-700">
                    <div className="text-[11px] text-gray-400">Select allowed models:</div>
                    <div className="flex flex-wrap gap-2">
                      {PRESET_MODELS.map((m) => {
                        const checked = formData.models_allowed.includes(m);
                        return (
                          <button
                            key={m}
                            type="button"
                            onClick={() => toggleModelSelection(m)}
                            className={`px-2 py-1 text-xs rounded-md border font-mono transition-colors ${
                              checked
                                ? "bg-purple-600 text-white border-purple-500"
                                : "bg-dark-900 text-gray-400 border-dark-600 hover:text-white"
                            }`}
                          >
                            {m}
                          </button>
                        );
                      })}
                    </div>
                  </div>
                )}
              </div>

              {/* Form Buttons */}
              <div className="flex items-center justify-end gap-3 pt-3 border-t border-dark-700">
                <button
                  type="button"
                  onClick={() => setShowCreateModal(false)}
                  className="btn btn-secondary text-xs px-4 py-2"
                >
                  Cancel
                </button>
                <button type="submit" className="btn btn-primary text-xs px-5 py-2">
                  Create Key
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* Edit API Key Modal */}
      {editingKey && (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/70 overflow-y-auto">
          <div className="card max-w-lg w-full p-6 space-y-4 border border-dark-700 my-8">
            <div className="flex items-center justify-between border-b border-dark-700 pb-3">
              <h3 className="font-bold text-lg text-white flex items-center gap-2">
                <Edit2 size={18} className="text-purple-400" />
                Edit API Key: {editingKey.name}
              </h3>
              <button onClick={() => setEditingKey(null)} className="text-gray-400 hover:text-white">
                <X size={18} />
              </button>
            </div>

            <form onSubmit={submitEdit} className="space-y-4 text-sm">
              <div>
                <label className="block text-xs font-medium text-gray-300 mb-1">
                  Key Name / Label <span className="text-red-400">*</span>
                </label>
                <input
                  type="text"
                  required
                  value={formData.name}
                  onChange={(e) => setFormData({ ...formData, name: e.target.value })}
                  className="w-full px-3 py-2 bg-dark-800 border border-dark-600 rounded-lg text-white"
                />
              </div>

              <div>
                <label className="block text-xs font-medium text-gray-300 mb-1">Description</label>
                <input
                  type="text"
                  value={formData.description}
                  onChange={(e) => setFormData({ ...formData, description: e.target.value })}
                  className="w-full px-3 py-2 bg-dark-800 border border-dark-600 rounded-lg text-white"
                />
              </div>

              {/* Status Switch */}
              <div className="flex items-center justify-between p-3 bg-dark-800/60 rounded-lg border border-dark-700">
                <div>
                  <div className="text-xs font-semibold text-white">Active Status</div>
                  <div className="text-[11px] text-gray-400">Toggle whether this key can make requests</div>
                </div>
                <button
                  type="button"
                  onClick={() => setFormData({ ...formData, is_active: !formData.is_active })}
                  className={`px-3 py-1 rounded text-xs font-semibold ${
                    formData.is_active ? "bg-green-600 text-white" : "bg-dark-700 text-gray-400"
                  }`}
                >
                  {formData.is_active ? "Active" : "Disabled"}
                </button>
              </div>

              {/* Rate Limits Grid */}
              <div className="grid grid-cols-1 sm:grid-cols-3 gap-3 pt-1">
                <div>
                  <label className="block text-[11px] font-medium text-gray-300 mb-1">
                    RPM Limit (0 = Unltd)
                  </label>
                  <input
                    type="number"
                    min="0"
                    value={formData.rpm_limit}
                    onChange={(e) => setFormData({ ...formData, rpm_limit: e.target.value })}
                    className="w-full px-3 py-1.5 bg-dark-800 border border-dark-600 rounded-lg text-white text-xs"
                  />
                </div>
                <div>
                  <label className="block text-[11px] font-medium text-gray-300 mb-1">
                    Daily Limit (0 = Unltd)
                  </label>
                  <input
                    type="number"
                    min="0"
                    value={formData.daily_limit}
                    onChange={(e) => setFormData({ ...formData, daily_limit: e.target.value })}
                    className="w-full px-3 py-1.5 bg-dark-800 border border-dark-600 rounded-lg text-white text-xs"
                  />
                </div>
                <div>
                  <label className="block text-[11px] font-medium text-gray-300 mb-1">
                    Token Quota (0 = Unltd)
                  </label>
                  <input
                    type="number"
                    min="0"
                    value={formData.token_limit}
                    onChange={(e) => setFormData({ ...formData, token_limit: e.target.value })}
                    className="w-full px-3 py-1.5 bg-dark-800 border border-dark-600 rounded-lg text-white text-xs"
                  />
                </div>
              </div>

              {/* Model Restrictions */}
              <div>
                <div className="flex items-center justify-between mb-2">
                  <label className="text-xs font-medium text-gray-300">Model Whitelist</label>
                  <label className="text-xs text-purple-400 flex items-center gap-1.5 cursor-pointer">
                    <input
                      type="checkbox"
                      checked={formData.restrictModels}
                      onChange={(e) =>
                        setFormData({ ...formData, restrictModels: e.target.checked })
                      }
                      className="rounded bg-dark-800 border-dark-600 text-purple-600"
                    />
                    Restrict models
                  </label>
                </div>
                {formData.restrictModels && (
                  <div className="space-y-2 p-3 bg-dark-800/60 rounded-lg border border-dark-700">
                    <div className="text-[11px] text-gray-400">Select allowed models:</div>
                    <div className="flex flex-wrap gap-2">
                      {PRESET_MODELS.map((m) => {
                        const checked = formData.models_allowed.includes(m);
                        return (
                          <button
                            key={m}
                            type="button"
                            onClick={() => toggleModelSelection(m)}
                            className={`px-2 py-1 text-xs rounded-md border font-mono transition-colors ${
                              checked
                                ? "bg-purple-600 text-white border-purple-500"
                                : "bg-dark-900 text-gray-400 border-dark-600 hover:text-white"
                            }`}
                          >
                            {m}
                          </button>
                        );
                      })}
                    </div>
                  </div>
                )}
              </div>

              {/* Form Buttons */}
              <div className="flex items-center justify-end gap-3 pt-3 border-t border-dark-700">
                <button
                  type="button"
                  onClick={() => setEditingKey(null)}
                  className="btn btn-secondary text-xs px-4 py-2"
                >
                  Cancel
                </button>
                <button type="submit" className="btn btn-primary text-xs px-5 py-2">
                  Save Changes
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}
