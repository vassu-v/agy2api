import {
  AlertTriangle,
  BarChart3,
  CheckCircle,
  Clock,
  Cpu,
  Database,
  FileText,
  Loader2,
  RefreshCw,
  Trash2,
  XCircle,
} from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import Modal from "../components/Modal";
import Select from "../components/Select";
import api from "../api";

const PAGE_SIZES = [20, 50, 100, 200].map((n) => ({ value: n, label: `per page ${n}` }));

function formatTime(ts) {
  const d = new Date(ts * 1000);
  const pad = (x) => String(x).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

function formatTokens(n) {
  if (!n) return "0";
  if (n >= 1000000) return `${(n / 1000000).toFixed(1)}M`;
  if (n >= 1000) return `${(n / 1000).toFixed(1)}K`;
  return String(n);
}

function formatMaybeJson(text) {
  if (typeof text !== "string") return JSON.stringify(text ?? null, null, 2);
  try {
    return JSON.stringify(JSON.parse(text), null, 2);
  } catch {
    return text;
  }
}

export default function UsageLogs() {
  const [items, setItems] = useState([]);
  const [total, setTotal] = useState(0);
  const [stats, setStats] = useState(null);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [statusFilter, setStatusFilter] = useState("all");
  const [formatFilter, setFormatFilter] = useState("");
  const [modelFilter, setModelFilter] = useState("");
  const [statusCodeFilter, setStatusCodeFilter] = useState("");
  const [loading, setLoading] = useState(true);
  const [clearing, setClearing] = useState(false);
  const [message, setMessage] = useState(null);
  const [detail, setDetail] = useState(null);
  const [detailBody, setDetailBody] = useState(null);
  const [detailBodyLoading, setDetailBodyLoading] = useState(false);
  const [copyStatus, setCopyStatus] = useState(null); 

  
  const [rangeHours, setRangeHours] = useState(24);

  const fetchLogs = useCallback(async () => {
    setLoading(true);
    try {
      const params = {
        offset: (page - 1) * pageSize,
        limit: pageSize,
        status: statusFilter,
      };
      if (formatFilter) params.api_format = formatFilter;
      if (modelFilter.trim()) params.model_name = modelFilter.trim();
      const code = parseInt(statusCodeFilter, 10);
      if (statusCodeFilter.trim() !== "" && Number.isInteger(code)) params.status_code = code;
      if (rangeHours > 0) params.start_timestamp = Math.floor(Date.now() / 1000) - rangeHours * 3600;

      const res = await api.get("/usage-logs/list", { params });
      setItems(res.data.items);
      setTotal(res.data.total);
    } catch (err) {
      setMessage({ type: "error", text: err.response?.data?.detail || "Failed to load request logs" });
    } finally {
      setLoading(false);
    }
  }, [page, pageSize, statusFilter, formatFilter, modelFilter, statusCodeFilter, rangeHours]);

  const fetchStats = useCallback(async () => {
    try {
      const params = {};
      if (rangeHours > 0) params.start_timestamp = Math.floor(Date.now() / 1000) - rangeHours * 3600;
      if (formatFilter) params.api_format = formatFilter;
      if (modelFilter.trim()) params.model_name = modelFilter.trim();
      const res = await api.get("/usage-logs/stats", { params });
      setStats(res.data);
    } catch (err) {
      
    }
  }, [rangeHours, formatFilter, modelFilter]);

  useEffect(() => {
    fetchLogs();
  }, [fetchLogs]);

  useEffect(() => {
    fetchStats();
  }, [fetchStats]);

  const refresh = () => {
    fetchLogs();
    fetchStats();
  };

  const clearLogs = async () => {
    if (!window.confirm("Clean logs older than 3 days? This action cannot be undone.")) return;
    setClearing(true);
    try {
      const res = await api.post("/usage-logs/clear", null, { params: { days: 3 } });
      setMessage({ type: "success", text: res.data.message || "Logs cleaned successfully" });
      refresh();
    } catch (err) {
      setMessage({ type: "error", text: err.response?.data?.detail || "Failed to clean logs" });
    } finally {
      setClearing(false);
    }
  };

  const truncateLogs = async () => {
    if (!window.confirm("Clear all request logs? The table will be dropped and recreated cleanly. This action cannot be undone.")) return;
    setClearing(true);
    try {
      const res = await api.post("/usage-logs/truncate");
      setMessage({ type: "success", text: res.data.message || "All logs cleared successfully" });
      refresh();
    } catch (err) {
      setMessage({ type: "error", text: err.response?.data?.detail || "Failed to clear logs" });
    } finally {
      setClearing(false);
    }
  };

  const totalPages = Math.max(1, Math.ceil(total / pageSize));

  
  const openRequestBody = async () => {
    if (!detail?.request_log_file) return;
    setCopyStatus(null);
    setDetailBodyLoading(true);
    try {
      const res = await api.get("/usage-logs/request-body", { params: { path: detail.request_log_file } });
      setDetailBody(res.data);
    } catch (err) {
      
      setDetailBody({ __missing: true, __message: err.response?.data?.detail || "Detailed log file does not exist or has been cleaned" });
    } finally {
      setDetailBodyLoading(false);
    }
  };

  const copyDetailBody = async () => {
    if (!detailBody || detailBody.__missing) return;
    const text = JSON.stringify(detailBody, null, 2);
    let ok = false;
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(text);
        ok = true;
      } else {
        
        const ta = document.createElement("textarea");
        ta.value = text;
        ta.style.position = "fixed";
        ta.style.top = "-9999px";
        ta.style.opacity = "0";
        document.body.appendChild(ta);
        ta.select();
        ok = document.execCommand("copy");
        document.body.removeChild(ta);
      }
    } catch {
      ok = false;
    }
    setCopyStatus(ok ? "ok" : "fail");
  };

  return (
    <div className="space-y-4">
      {}
      {stats && (
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
          <div className="card p-4 flex items-center gap-3">
            <div className="p-2.5 bg-purple-600/20 rounded-lg">
              <Database size={20} className="text-purple-400" />
            </div>
            <div className="min-w-0">
              <div className="text-xl font-bold">{stats.total_requests}</div>
              <div className="text-xs text-gray-500">Total Requests</div>
            </div>
          </div>
          <div className="card p-4 flex items-center gap-3">
            <div className="p-2.5 bg-green-600/20 rounded-lg">
              <CheckCircle size={20} className="text-green-400" />
            </div>
            <div className="min-w-0">
              <div className="text-xl font-bold text-green-400">{stats.success_count}</div>
              <div className="text-xs text-gray-500">Success</div>
            </div>
          </div>
          <div className="card p-4 flex items-center gap-3">
            <div className="p-2.5 bg-red-600/20 rounded-lg">
              <XCircle size={20} className="text-red-400" />
            </div>
            <div className="min-w-0">
              <div className="text-xl font-bold text-red-400">{stats.error_count}</div>
              <div className="text-xs text-gray-500">Failed</div>
            </div>
          </div>
          <div className="card p-4 flex items-center gap-3">
            <div className="p-2.5 bg-blue-600/20 rounded-lg">
              <Cpu size={20} className="text-blue-400" />
            </div>
            <div className="min-w-0">
              <div className="text-xl font-bold">{formatTokens(stats.total_tokens)}</div>
              <div className="text-xs text-gray-500">
                Total Tokens{stats.cached_tokens > 0 ? ` (Cache ${formatTokens(stats.cached_tokens)})` : ""}
              </div>
            </div>
          </div>
        </div>
      )}

      {}
      <div className="flex flex-wrap items-center gap-2">
        <button onClick={refresh} disabled={loading} className="btn btn-secondary text-xs sm:text-sm px-3 py-1.5 sm:px-4 sm:py-2 flex items-center gap-1.5">
          <RefreshCw size={15} className={loading ? "animate-spin" : ""} />
          Refresh
        </button>
        <button onClick={clearLogs} disabled={clearing} className="btn btn-secondary text-xs sm:text-sm px-3 py-1.5 sm:px-4 sm:py-2 flex items-center gap-1.5">
          {clearing ? <Loader2 size={15} className="animate-spin" /> : <Trash2 size={15} />}
          Clean Logs Older Than 3 Days
        </button>
        <button onClick={truncateLogs} disabled={clearing} className="btn btn-danger text-xs sm:text-sm px-3 py-1.5 sm:px-4 sm:py-2 flex items-center gap-1.5">
          <Trash2 size={15} />
          Clear All Logs
        </button>
      </div>

      {}
      {message && (
        <Modal type={message.type} title={message.type === "success" ? "Success" : "Operation Failed"} onClose={() => setMessage(null)}>
          <span className="whitespace-pre-wrap break-all">{message.text}</span>
        </Modal>
      )}

      {}
      <div className="grid grid-cols-2 sm:flex sm:flex-wrap gap-2 sm:gap-3 text-sm">
        <Select
          value={rangeHours}
          onChange={(v) => { setRangeHours(Number(v)); setPage(1); }}
          className="w-full sm:w-32"
          options={[
            { value: 1, label: "Last 1 Hour" },
            { value: 6, label: "Last 6 Hours" },
            { value: 24, label: "Last 24 Hours" },
            { value: 24 * 7, label: "Last 7 Days" },
            { value: 24 * 30, label: "Last 30 Days" },
            { value: 0, label: "All Time" },
          ]}
        />
        <Select
          value={statusFilter}
          onChange={(v) => { setStatusFilter(v); setPage(1); }}
          className="w-full sm:w-32"
          options={[
            { value: "all", label: "Status: All" },
            { value: "success", label: "Success Only" },
            { value: "error", label: "Failed Only" },
          ]}
        />
        <Select
          value={formatFilter}
          onChange={(v) => { setFormatFilter(v); setPage(1); }}
          className="w-full sm:w-32"
          options={[
            { value: "", label: "Format: All" },
            { value: "openai", label: "OpenAI" },
            { value: "gemini", label: "Gemini" },
            { value: "anthropic", label: "Claude" },
          ]}
        />
        <input
          value={modelFilter}
          onChange={(e) => { setModelFilter(e.target.value); setPage(1); }}
          placeholder="Filter by model..."
          className="w-full sm:w-40 bg-dark-800 border border-dark-600 rounded-lg px-3 py-2 text-gray-200 text-xs sm:text-sm placeholder-gray-500"
        />
        <input
          value={statusCodeFilter}
          onChange={(e) => { setStatusCodeFilter(e.target.value); setPage(1); }}
          placeholder="Status code..."
          inputMode="numeric"
          className="w-full sm:w-28 bg-dark-800 border border-dark-600 rounded-lg px-3 py-2 text-gray-200 text-xs sm:text-sm placeholder-gray-500"
        />
        <Select
          value={pageSize}
          onChange={(v) => { setPageSize(Number(v)); setPage(1); }}
          className="w-full sm:w-32"
          options={PAGE_SIZES}
        />
      </div>

      {}
      {loading ? (
        <div className="flex items-center justify-center py-12">
          <Loader2 className="animate-spin text-purple-500" size={28} />
        </div>
      ) : items.length === 0 ? (
        <div className="card p-8 text-center text-gray-500">No request logs found</div>
      ) : (
        <div className="space-y-2">
          {items.map((item) => (
            <button
              key={item.id}
              onClick={() => { setDetail(item); setDetailBody(null); }}
              className="card p-3 sm:p-4 w-full text-left hover:border-purple-500/40 transition-colors cursor-pointer"
            >
              <div className="flex items-start justify-between gap-2">
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className={`text-xs px-1.5 py-0.5 rounded ${item.success ? "bg-green-500/15 text-green-400" : "bg-red-500/15 text-red-400"}`}>
                      {item.success ? "✓ Success" : `✗ ${item.status_code || "Failed"}`}
                    </span>
                    <span className="text-sm font-medium text-gray-200 truncate">{item.model_name || "(unknown)"}</span>
                      {item.upstream_model_name && item.upstream_model_name !== item.model_name && (
                        <span className="text-xs text-gray-500 truncate" title="Real Upstream Model">→ {item.upstream_model_name}</span>
                      )}
                      <span className="text-xs px-1.5 py-0.5 rounded bg-dark-700 text-gray-400">
                        {item.api_format === "openai" ? "OpenAI" : item.api_format === "gemini" ? "Gemini" : item.api_format === "anthropic" ? "Claude" : item.api_format || "?"}
                      </span>
                      {item.is_stream && <span className="text-xs px-1.5 py-0.5 rounded bg-blue-500/15 text-blue-400">Stream</span>}
                      {item.request_log_file && <span className="text-xs px-1.5 py-0.5 rounded bg-amber-500/15 text-amber-400">Detail</span>}
                    </div>
                  <div className="text-xs text-gray-500 mt-1 flex flex-wrap gap-x-3 gap-y-0.5">
                    <span className="flex items-center gap-1"><Clock size={11} />{formatTime(item.created_at)}</span>
                    <span>{(item.use_time || 0).toFixed(1)}s</span>
                    <span>{item.credential_email || "-"}</span>
                  </div>
                </div>
                <div className="text-right shrink-0 text-xs text-gray-400">
                  <div>{formatTokens(item.total_tokens)} tokens</div>
                  <div className="text-gray-600">
                    ↑{formatTokens(item.prompt_tokens)} ↓{formatTokens(item.completion_tokens)}
                  </div>
                  {item.cached_tokens > 0 && (
                    <div className="text-emerald-500/80" title="Tokens in prompt matching upstream cache">
                      ⚡Cache {formatTokens(item.cached_tokens)}
                    </div>
                  )}
                </div>
              </div>
            </button>
          ))}
        </div>
      )}

      {}
      {total > 0 && (
        <div className="flex items-center justify-between text-sm text-gray-400">
          <span>Total {total} items</span>
          <div className="flex items-center gap-2">
            <button
              onClick={() => setPage((p) => Math.max(1, p - 1))}
              disabled={page <= 1}
              className="btn btn-secondary text-xs px-3 py-1.5 disabled:opacity-40"
            >
              Previous
            </button>
            <span>{page} / {totalPages}</span>
            <button
              onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
              disabled={page >= totalPages}
              className="btn btn-secondary text-xs px-3 py-1.5 disabled:opacity-40"
            >
              Next
            </button>
          </div>
        </div>
      )}

      {}
      {detail && (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/60" onClick={() => setDetail(null)}>
          <div className="card max-w-lg w-full max-h-[80vh] overflow-y-auto" onClick={(e) => e.stopPropagation()}>
            <div className="flex items-center justify-between mb-4">
              <h3 className="text-base font-semibold text-purple-400">Request Details</h3>
              <button onClick={() => setDetail(null)} className="text-gray-400 hover:text-white">✕</button>
            </div>
            <div className="space-y-2 text-sm">
              {[
                ["Timestamp", formatTime(detail.created_at)],
                ["Request ID", detail.request_id || "-"],
                ["Status", detail.success ? "Success" : `Failed (${detail.status_code})`],
                ["API Format", detail.api_format || "-"],
                ["Client IP", detail.client_ip || "-"],
                ["Requested Model", detail.model_name || "-"],
                ["Upstream Model", detail.upstream_model_name || detail.model_name || "-"],
                ["Credential", detail.credential_email || "-"],
                ["Stream", detail.is_stream ? "Yes" : "No"],
                ["Latency", `${(detail.use_time || 0).toFixed(2)}s`],
                ["Prompt Tokens", String(detail.prompt_tokens)],
                ["Completion Tokens", String(detail.completion_tokens)],
                ["Total Tokens", String(detail.total_tokens)],
                ...(detail.cached_tokens > 0 ? [["Cached Tokens", String(detail.cached_tokens)]] : []),
              ].map(([k, v]) => (
                <div key={k} className="flex justify-between gap-4 border-b border-dark-700 pb-1.5">
                  <span className="text-gray-500 shrink-0">{k}</span>
                  <span className="text-gray-200 text-right break-all">{v}</span>
                </div>
              ))}
              {detail.error_message && (
                <div>
                  <div className="text-gray-500 mb-1">Error Details</div>
                  <div className="bg-red-500/10 border border-red-500/30 rounded-lg p-2.5 text-xs text-red-300 break-all">
                    {detail.error_message}
                  </div>
                </div>
              )}
              {detail.request_log_file && (
                <div className="pt-2">
                  <div className="flex items-center justify-between gap-2">
                    <div className="text-gray-500">Detailed Request Body Log</div>
                    <button
                      onClick={openRequestBody}
                      disabled={detailBodyLoading}
                      className="btn btn-secondary text-xs px-3 py-1.5 flex items-center gap-1.5"
                    >
                      {detailBodyLoading ? <Loader2 size={13} className="animate-spin" /> : <FileText size={13} />}
                      View Details
                    </button>
                  </div>
                  <p className="text-xs text-gray-500 mt-1.5">
                    Full sanitized headers and body sent upstream are saved when specified error codes occur for debugging.
                  </p>
                </div>
              )}
            </div>
          </div>
        </div>
      )}

      {}
      {detailBody && (
        <div className="fixed inset-0 z-[60] flex items-center justify-center p-4 bg-black/70" onClick={() => setDetailBody(null)}>
          <div className="card max-w-4xl w-full max-h-[85vh] overflow-y-auto" onClick={(e) => e.stopPropagation()}>
            {detailBody.__missing ? (
              <div className="py-8 text-center text-sm text-gray-400">
                <AlertTriangle size={28} className="mx-auto mb-3 text-amber-400" />
                <div>{detailBody.__message}</div>
                <div className="text-xs text-gray-500 mt-2">Detailed log file on disk may have been cleaned or deleted.</div>
              </div>
            ) : (
              <>
            <div className="flex items-center justify-between mb-3">
              <h3 className="text-base font-semibold text-purple-400">Detailed Request Body Log</h3>
              <div className="flex items-center gap-2">
                {copyStatus === "ok" && <span className="text-xs text-green-400">Copied</span>}
                {copyStatus === "fail" && <span className="text-xs text-red-400">Copy Failed</span>}
                <button onClick={copyDetailBody} className="btn btn-secondary text-xs px-3 py-1.5">
                  Copy JSON
                </button>
                <button onClick={() => setDetailBody(null)} className="text-gray-400 hover:text-white">✕</button>
              </div>
            </div>
            <p className="text-xs text-gray-500 mb-3">
              Saved when target error codes occur for debugging: complete sanitized headers and request payload.
            </p>
            <div className="space-y-3 text-xs">
              <div>
                <div className="text-gray-400 mb-1">Request URL</div>
                <pre className="bg-dark-800 border border-dark-600 rounded-lg p-2.5 overflow-x-auto text-gray-200 whitespace-pre-wrap break-all">{detailBody.url || "-"}</pre>
              </div>
              <div>
                <div className="text-gray-400 mb-1">Request Headers (Sanitized)</div>
                <pre className="bg-dark-800 border border-dark-600 rounded-lg p-2.5 overflow-x-auto text-gray-200">{JSON.stringify(detailBody.request_headers || {}, null, 2)}</pre>
              </div>
              <div>
                <div className="text-gray-400 mb-1">Request Body</div>
                <pre className="bg-dark-800 border border-dark-600 rounded-lg p-2.5 overflow-x-auto text-gray-200">{JSON.stringify(detailBody.request_body ?? {}, null, 2)}</pre>
              </div>
              {detailBody.response_body && (
                <div>
                  <div className="text-gray-400 mb-1">Upstream Response</div>
                  <pre className="bg-dark-800 border border-dark-600 rounded-lg p-2.5 overflow-x-auto text-gray-200 whitespace-pre-wrap break-all">{formatMaybeJson(detailBody.response_body)}</pre>
                </div>
              )}
              {detailBody.error_message && (
                <div>
                  <div className="text-gray-400 mb-1">Error Summary</div>
                  <pre className="bg-red-500/10 border border-red-500/30 rounded-lg p-2.5 overflow-x-auto text-red-300 whitespace-pre-wrap break-all">{detailBody.error_message}</pre>
                </div>
              )}
            </div>
              </>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
