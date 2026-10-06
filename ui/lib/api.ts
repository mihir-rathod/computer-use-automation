const KEY = "cua.apiKey";

export class ApiError extends Error {
  constructor(public status: number, message: string) { super(message); }
}

export const getKey = () => { try { return localStorage.getItem(KEY); } catch { return null; } };
export const setKey = (k: string) => { try { localStorage.setItem(KEY, k); } catch { /* private mode: the key lives for this tab only */ } };
export const clearKey = () => { try { localStorage.removeItem(KEY); } catch { /* ignore */ } };

function detailOf(body: unknown, fallback: string): string {
  if (body && typeof body === "object" && "detail" in body) {
    const d = (body as { detail: unknown }).detail;
    if (typeof d === "string") return d;
    if (Array.isArray(d)) return d.map((x) => (x && typeof x === "object" && "msg" in x ? String((x as { msg: unknown }).msg) : String(x))).join("; ");
  }
  return fallback;
}

export async function api<T>(path: string, init: RequestInit & { json?: unknown; key?: string } = {}): Promise<T> {
  const headers = new Headers(init.headers);
  const key = init.key ?? getKey();
  if (key) headers.set("Authorization", `Bearer ${key}`);
  let body = init.body;
  if (init.json !== undefined) { headers.set("Content-Type", "application/json"); body = JSON.stringify(init.json); }
  let res: Response;
  try { res = await fetch(path, { ...init, headers, body }); }
  catch { throw new ApiError(0, "Could not reach the platform API. Is the server running?"); }
  if (res.status === 401 && !init.key) window.dispatchEvent(new Event("cua:unauthorized"));
  if (!res.ok) {
    let parsed: unknown = null;
    try { parsed = await res.json(); } catch { /* not JSON */ }
    throw new ApiError(res.status, detailOf(parsed, `${res.status} ${res.statusText}`));
  }
  return res.status === 204 ? (undefined as T) : ((await res.json()) as T);
}

/** An authenticated image/file URL: the browser cannot send an Authorization header from <img>, so fetch it and hand back an object URL. */
export async function fetchBlobUrl(path: string): Promise<string> {
  const res = await fetch(path, { headers: { Authorization: `Bearer ${getKey() ?? ""}` } });
  if (!res.ok) throw new ApiError(res.status, "could not load");
  return URL.createObjectURL(await res.blob());
}

export async function download(path: string, filename: string): Promise<void> {
  const url = await fetchBlobUrl(path);
  const a = document.createElement("a");
  a.href = url; a.download = filename; a.click();
  setTimeout(() => URL.revokeObjectURL(url), 5000);
}

export interface StreamEvent { event: string; data: unknown; id?: string }

/** Server-sent events over fetch, because EventSource cannot send an Authorization header. Resolves when the server ends the stream. */
export async function streamEvents(path: string, onEvent: (e: StreamEvent) => void, signal: AbortSignal): Promise<void> {
  const res = await fetch(path, { headers: { Authorization: `Bearer ${getKey() ?? ""}`, Accept: "text/event-stream" }, signal });
  if (!res.ok || !res.body) throw new ApiError(res.status, "stream failed");
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) return;
    buffer += decoder.decode(value, { stream: true });
    let cut: number;
    while ((cut = buffer.indexOf("\n\n")) >= 0) {
      const block = buffer.slice(0, cut); buffer = buffer.slice(cut + 2);
      let event = "message", data = "", id: string | undefined;
      for (const line of block.split("\n")) {
        if (line.startsWith("event:")) event = line.slice(6).trim();
        else if (line.startsWith("data:")) data += line.slice(5).trim();
        else if (line.startsWith("id:")) id = line.slice(3).trim();
      }
      if (!data) continue;
      try { onEvent({ event, data: JSON.parse(data), id }); } catch { /* skip a malformed block */ }
    }
  }
}
