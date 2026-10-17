/**
 * Wo das Backend liegt und wie man sich anmeldet.
 *
 * - Entwicklung (`make dev`): ws://127.0.0.1:8765/ws
 * - Server (Docker auf der VM): dieselbe Adresse wie die Seite, Pfad /ws – Caddy leitet
 *   intern ans Backend weiter. https-Seite -> wss (Browser verbieten ws von https aus).
 * - Override zur Build-Zeit: VITE_COCKBOOK_WS
 *
 * Token: einmal über den Link `https://…/#token=…` (der Teil nach # wird nie an einen
 * Server geschickt) oder per Eingabe; danach im localStorage dieses Browsers.
 */

const TOKEN_KEY = "cockbook.token.v1";

export function backendUrl(): string {
  const env = import.meta.env.VITE_COCKBOOK_WS as string | undefined;
  if (env) return env;
  if (import.meta.env.DEV) return "ws://127.0.0.1:8765/ws";
  return `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`;
}

export function getToken(): string | null {
  try {
    return localStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

export function setToken(token: string | null): void {
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token);
    else localStorage.removeItem(TOKEN_KEY);
  } catch {
    /* privates Fenster: dann nur für diese Sitzung */
  }
}

/** Token aus `#token=…` übernehmen und aus der Adresszeile entfernen (true = gefunden). */
export function takeTokenFromHash(): boolean {
  const m = /(?:^#|&)token=([^&]+)/.exec(location.hash);
  if (!m) return false;
  setToken(decodeURIComponent(m[1]!));
  history.replaceState(null, "", location.pathname + location.search);
  document.getElementById("token-prompt")?.remove();
  return true;
}

export function wsUrlWithToken(): string {
  const t = getToken();
  const url = backendUrl();
  return t ? `${url}${url.includes("?") ? "&" : "?"}token=${encodeURIComponent(t)}` : url;
}

/** Kleine Eingabemaske für das Token; ruft `onSubmit` mit dem neuen Token. */
export function showTokenPrompt(onSubmit: () => void): void {
  if (document.getElementById("token-prompt")) return;
  const box = document.createElement("div");
  box.id = "token-prompt";
  box.innerHTML = `
    <form>
      <h2>Zugang zu CockBook</h2>
      <p>Dieses Backend ist geschützt. Gib das Token ein
      (steht in <code>deploy/.env</code> auf dem Server, <code>COCKBOOK_TOKEN</code>).</p>
      <input type="password" autocomplete="current-password" placeholder="Token" required />
      <button type="submit">Verbinden</button>
      <p class="hint">${getToken() ? "Das gespeicherte Token wurde abgelehnt." : ""}</p>
    </form>`;
  const form = box.querySelector("form")!;
  const input = box.querySelector("input")!;
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    setToken(input.value.trim());
    box.remove();
    onSubmit();
  });
  document.body.append(box);
  input.focus();
}
