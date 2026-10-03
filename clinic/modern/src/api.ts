export interface User { username: string; display_name: string; role: "frontdesk" | "supervisor" }
export interface Patient {
  id: number; mrn: string; first_name: string; last_name: string; dob: string; phone: string; email: string;
  address: string; insurer: string | null; insurance_member_id: string | null;
}
export interface Appointment {
  id: number; number: string; provider: string; starts_at: string; duration_min: number; reason: string;
  status: string; can_modify: boolean;
}
export interface Invoice {
  id: number; number: string; description: string; amount_cents: number; paid_cents: number; refunded_cents: number;
  writeoff_cents: number; claimed: number; balance_cents: number; refundable_cents: number; created_on: string;
}
export interface Paged<T> { items: T[]; total: number; page: number; pages: number }
export interface Claim { number: string; invoice_number: string; payer: string; amount_cents: number; status: string }
export interface RefundRow { number: string; invoice_number: string; amount_cents: number; status: string }
export interface PatientDetail {
  patient: Patient; appointments: Appointment[]; balance_cents: number; claims: Claim[]; refunds: RefundRow[];
  invoices: Paged<Invoice>;
}
export interface Line { label: string; value: string }
export interface Review {
  kind: string; subject: string; title: string; lines: Line[]; warnings: string[]; irreversible?: boolean;
  requires_approval?: boolean;
}
export interface Receipt { kind: string; status: string; receipt_number: string; message: string; lines: Line[] }
export interface Meta {
  today: string; providers: string[]; refund_cap_cents: number;
  cancel_reasons: Record<string, string>; refund_reasons: Record<string, string>; writeoff_reasons: Record<string, string>;
}
export interface Approval {
  number: string; invoice_number: string; amount_cents: number; requested_by: string; mrn: string;
  first_name: string; last_name: string; reason_code: string; notes: string | null;
}
export interface ScheduleRow {
  number: string; starts_at: string; provider: string; mrn: string; first_name: string; last_name: string;
  reason: string; status: string;
}

export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string, public problems: string[] = [],
              public result?: Receipt) {
    super(message);
  }
}

let onSessionLost: (() => void) | null = null;
export const setSessionLostHandler = (fn: (() => void) | null) => { onSessionLost = fn; };

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await fetch(`/api${path}`, {
    credentials: "same-origin",
    ...init,
    headers: { "Content-Type": "application/json", "X-Clinic-Client": "modern-ui", ...init.headers },
  });
  if (res.ok) return (await res.json()) as T;
  let body: { error?: string; message?: string; problems?: string[]; result?: Receipt } = {};
  try { body = await res.json(); } catch { /* non-JSON error body */ }
  const err = new ApiError(res.status, body.error ?? "error", body.message ?? `Request failed (${res.status})`, body.problems, body.result);
  if (res.status === 401 && !path.startsWith("/auth/login")) onSessionLost?.();
  throw err;
}

const post = <T>(path: string, body?: unknown) =>
  request<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });

const qs = (params: Record<string, string | number | undefined>) => {
  const p = new URLSearchParams();
  Object.entries(params).forEach(([k, v]) => { if (v !== undefined && v !== "") p.set(k, String(v)); });
  const s = p.toString();
  return s ? `?${s}` : "";
};

export const api = {
  login: (username: string, password: string) => post<{ user: User }>("/auth/login", { username, password }),
  logout: () => post<{ ok: boolean }>("/auth/logout"),
  me: () => request<{ user: User }>("/auth/me"),
  meta: () => request<Meta>("/meta"),
  uiText: () => request<{ level: number; text: Record<string, string> }>("/ui"),
  searchPatients: (q: { mrn?: string; last_name?: string; dob?: string; page?: number }) =>
    request<Paged<Patient>>(`/patients${qs(q)}`),
  patient: (id: number, invoicePage = 1) => request<PatientDetail>(`/patients/${id}${qs({ invoice_page: invoicePage })}`),
  updateContact: (id: number, body: { phone: string; email: string; address: string }) =>
    request<{ patient: Patient; changed: string[] }>(`/patients/${id}/contact`, { method: "PATCH", body: JSON.stringify(body) }),
  slots: (number: string, date: string) => request<{ slots: string[] }>(`/appointments/${number}/slots${qs({ date })}`),
  reschedule: (number: string, date: string, time: string) =>
    post<{ appointment: Appointment }>(`/appointments/${number}/reschedule`, { date, time }),
  review: (kind: string, body: Record<string, unknown>) =>
    post<{ token: string; review: Review }>(`/transactions/${kind}/review`, body),
  confirm: (token: string) => post<Receipt>(`/transactions/${token}/confirm`),
  discard: (token: string) => post<{ ok: boolean }>(`/transactions/${token}/discard`),
  approvals: () => request<{ items: Approval[] }>("/approvals"),
  decide: (number: string, decision: "approve" | "deny", note: string) =>
    post<{ approval: string; status: string }>(`/approvals/${number}/decision`, { decision, note }),
  schedule: (date: string, provider: string) => request<{ items: ScheduleRow[] }>(`/schedule${qs({ date, provider })}`),
  scheduleCsv: async (date: string, provider: string): Promise<Blob> => {
    const res = await fetch(`/api/schedule.csv${qs({ date, provider })}`, {
      credentials: "same-origin", headers: { "X-Clinic-Client": "modern-ui" },
    });
    if (!res.ok) {
      if (res.status === 401) onSessionLost?.();
      throw new ApiError(res.status, "download_failed", "The download failed.");
    }
    return res.blob();
  },
};

export const money = (cents: number) =>
  `${cents < 0 ? "-" : ""}$${(Math.abs(cents) / 100).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

export const fmtDateTime = (iso: string) =>
  new Date(iso).toLocaleString("en-US", { weekday: "short", day: "2-digit", month: "short", year: "numeric", hour: "numeric", minute: "2-digit" });

export const toCents = (text: string): number => Math.round(parseFloat(text.replace(/[$,]/g, "")) * 100);
