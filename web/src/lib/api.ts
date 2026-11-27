import type {
  AuditEntry,
  Investigation,
  InvestigationView,
  Proposal,
  SessionResponse,
} from "./types";

export const API_URL = (process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000").replace(
  /\/$/,
  "",
);

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

async function request<T>(path: string, init: RequestInit = {}, token?: string): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body) headers.set("Content-Type", "application/json");
  if (token) headers.set("Authorization", `Bearer ${token}`);
  const response = await fetch(`${API_URL}${path}`, { ...init, headers, cache: "no-store" });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = await response.json();
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch {
      // keep statusText
    }
    throw new ApiError(response.status, detail);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export const api = {
  health: () => request<{ status: string; mock: boolean }>("/api/health"),
  investigations: () => request<Investigation[]>("/api/investigations"),
  investigation: (id: string) => request<InvestigationView>(`/api/investigations/${id}`),
  audit: () => request<AuditEntry[]>("/api/audit"),
  login: (password: string) =>
    request<SessionResponse>("/api/session", {
      method: "POST",
      body: JSON.stringify({ password }),
    }),
  approve: (proposalId: string, token: string) =>
    request<Proposal>(`/api/proposals/${proposalId}/approve`, { method: "POST" }, token),
  reject: (proposalId: string, token: string) =>
    request<Proposal>(`/api/proposals/${proposalId}/reject`, { method: "POST" }, token),
  activity: (investigationId: string, activeMs: number, visible: boolean) =>
    request<void>("/api/activity", {
      method: "POST",
      body: JSON.stringify({
        investigation_id: investigationId,
        active_ms: activeMs,
        visible,
        ts: new Date().toISOString(),
      }),
    }),
};
