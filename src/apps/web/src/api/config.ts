const LOOPBACK_HOSTS = new Set(["localhost", "127.0.0.1", "[::1]"]);

function parseUrl(value: string | undefined): URL | null {
  const trimmed = value?.trim();
  if (!trimmed) return null;
  try {
    return new URL(trimmed);
  } catch {
    return null;
  }
}

export function apiBaseUrl(): string {
  const raw = import.meta.env.VITE_FORESIGHTNINE_API_BASE_URL;
  const url = parseUrl(raw);
  if (
    url
    && !raw!.includes("#")
    && !url.username
    && !url.password
    && !url.search
    && !url.hash
    && (url.protocol === "https:" || (url.protocol === "http:" && LOOPBACK_HOSTS.has(url.hostname)))
  ) {
    return (raw as string).trim().replace(/\/+$/, "");
  }
  return window.location.origin;
}

export function oauthReturnUri(): string | null {
  const raw = import.meta.env.VITE_FORESIGHTNINE_OAUTH_RETURN_URI;
  const url = parseUrl(raw);
  if (url && url.protocol === "https:" && !raw!.includes("#") && !url.hash && !url.username && !url.password) {
    return (raw as string).trim();
  }
  return null;
}

export function localAuthEnabled(): boolean {
  return import.meta.env.VITE_FORESIGHTNINE_LOCAL_AUTH === "mock";
}
