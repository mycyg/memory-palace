export type Scope = {
  project: string;
  persona: string;
  collection: string;
  world: string;
};

export const scopeDefault: Scope = {
  project: "personal",
  persona: "default",
  collection: "default",
  world: "real",
};

let credential = new URLSearchParams(location.hash.slice(1)).get("token") ??
  sessionStorage.getItem("memorypalace-token") ?? "";
if (location.hash) history.replaceState(null, "", location.pathname);

export function currentToken(): string { return credential; }
export function setToken(value: string): void {
  credential = value.trim();
  if (credential) sessionStorage.setItem("memorypalace-token", credential);
  else sessionStorage.removeItem("memorypalace-token");
}

export async function request<T = any>(
  path: string,
  options: { method?: string; query?: Record<string, string | number | undefined>; body?: unknown } = {},
): Promise<T> {
  const url = new URL(path, location.origin);
  for (const [key, value] of Object.entries(options.query ?? {})) {
    if (value !== undefined && value !== "") url.searchParams.set(key, String(value));
  }
  const body = options.body instanceof FormData ? options.body :
    options.body === undefined ? undefined : JSON.stringify(options.body);
  const headers: Record<string, string> = { Authorization: "Bearer " + credential };
  if (body && !(body instanceof FormData)) headers["Content-Type"] = "application/json";
  const response = await fetch(url, {
    method: options.method ?? "GET",
    headers,
    body,
    cache: "no-store",
  });
  if (!response.ok) {
    const detail = await response.json().catch(() => ({})) as { detail?: string };
    throw new Error(detail.detail || "HTTP " + response.status);
  }
  return response.json() as Promise<T>;
}

export const commandId = () => crypto.randomUUID();
export const stamp = (value?: string) => value
  ? new Date(value).toLocaleString("zh-CN", { dateStyle: "medium", timeStyle: "short" })
  : "—";
