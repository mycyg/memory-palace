import { Client } from "../../sdk/typescript/src/index";
export const api = new Client(location.origin, "");
export function setToken(token: string) {
  Object.assign(api, { token });
  sessionStorage.setItem("memorypalace-token", token);
}
export const initialToken =
  new URLSearchParams(location.hash.slice(1)).get("token") ??
  sessionStorage.getItem("memorypalace-token") ??
  "";
if (initialToken) setToken(initialToken);
if (location.hash) history.replaceState(null, "", location.pathname);
export const scopeDefault = {
  project: "personal",
  persona: "default",
  collection: "default",
  world: "real",
};
export type Scope = typeof scopeDefault;
export const commandId = () => crypto.randomUUID();
export const stamp = (value?: string) =>
  value
    ? new Date(value).toLocaleString("zh-CN", {
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      })
    : "—";
