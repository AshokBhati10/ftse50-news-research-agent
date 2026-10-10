/**
 * Typed API client — the single place the frontend talks to the backend.
 * Base URL comes from VITE_API_BASE_URL (default: same-origin `/api`,
 * which the Vite dev server proxies to the FastAPI backend).
 */

import type {
  CompaniesResponse,
  HealthResponse,
  ReportDetail,
  ReportsResponse,
  ResearchRun,
  ResearchRunCreate,
  RunsResponse,
  SettingsStatus,
} from "../types";

const BASE: string =
  (import.meta.env.VITE_API_BASE_URL as string | undefined) ?? "/api";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let resp: Response;
  try {
    resp = await fetch(`${BASE}${path}`, {
      headers: { "Content-Type": "application/json" },
      ...init,
    });
  } catch {
    throw new ApiError(0, "Cannot reach the backend API. Is the FastAPI server running?");
  }
  if (!resp.ok) {
    let detail = `Request failed (${resp.status})`;
    try {
      const body = await resp.json();
      if (typeof body?.detail === "string") detail = body.detail;
    } catch {
      /* keep default */
    }
    throw new ApiError(resp.status, detail);
  }
  if (resp.status === 204) return undefined as T;
  return (await resp.json()) as T;
}

export const api = {
  health: () => request<HealthResponse>("/health"),
  companies: (refresh = false) =>
    request<CompaniesResponse>(`/companies${refresh ? "?refresh=true" : ""}`),
  reports: () => request<ReportsResponse>("/reports"),
  latestReport: () => request<ReportDetail>("/reports/latest"),
  report: (id: string) => request<ReportDetail>(`/reports/${encodeURIComponent(id)}`),
  downloadUrl: (id: string, format: "json" | "md") =>
    `${BASE}/reports/${encodeURIComponent(id)}/download?format=${format}`,
  runs: (limit = 20) => request<RunsResponse>(`/research-runs?limit=${limit}`),
  run: (id: string) => request<ResearchRun>(`/research-runs/${encodeURIComponent(id)}`),
  startRun: (body: ResearchRunCreate) =>
    request<ResearchRun>("/research-runs", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  cancelRun: (id: string) =>
    request<ResearchRun>(`/research-runs/${encodeURIComponent(id)}/cancel`, {
      method: "POST",
    }),
  settingsStatus: () => request<SettingsStatus>("/settings/status"),

  /**
   * Subscribe to a run's Server-Sent Events. Resolves with a cleanup
   * function. The caller receives parsed event payloads.
   */
  subscribeToRun: (
    id: string,
    onEvent: (event: { type: string; [k: string]: unknown }) => void,
    onError?: (err: Error) => void
  ): (() => void) => {
    const source = new EventSource(
      `${BASE}/research-runs/${encodeURIComponent(id)}/events`
    );
    source.onmessage = (msg) => {
      try {
        onEvent(JSON.parse(msg.data));
      } catch {
        /* ignore malformed frames */
      }
    };
    source.onerror = () => {
      // EventSource auto-reconnects; only report if the stream is dead.
      if (source.readyState === EventSource.CLOSED) {
        onError?.(new Error("Live update stream closed."));
      }
    };
    return () => source.close();
  },
};
