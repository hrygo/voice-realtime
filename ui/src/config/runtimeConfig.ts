const DEFAULT_MEETING_WS_PATH = "/ws/v1/meetings";
const DEFAULT_CONTROL_WS_PATH = "/ws/v1/control";
const DEFAULT_SUBTITLES_WS_PATH = "/ws/subtitles";
const DEFAULT_ASSISTANT_WS_PATH = "/ws/assistant";

export function normalizeBaseUrl(value: string | undefined): string {
  return value?.trim().replace(/\/+$/u, "") ?? "";
}

export function buildApiUrl(baseUrl: string, path: string): string {
  const normalizedBase = normalizeBaseUrl(baseUrl);
  const normalizedPath = path.startsWith("/") ? path : `/${path}`;
  return `${normalizedBase}${normalizedPath}`;
}

export function deriveWebSocketUrl(apiBaseUrl: string, path: string): string {
  const normalizedPath = path.startsWith("/") ? path : `/${path}`;
  const normalizedBase = normalizeBaseUrl(apiBaseUrl);
  if (!normalizedBase) return normalizedPath;

  try {
    const origin = new URL(normalizedBase);
    origin.protocol = origin.protocol === "https:" ? "wss:" : "ws:";
    origin.pathname = "/";
    origin.search = "";
    origin.hash = "";
    return `${origin.toString().replace(/\/$/u, "")}${normalizedPath}`;
  } catch {
    return normalizedPath;
  }
}

const apiBaseUrl = normalizeBaseUrl(import.meta.env.VITE_API_BASE_URL);
const configuredMeetingWsUrl = import.meta.env.VITE_MEETING_WS_URL?.trim();
const configuredControlWsUrl = import.meta.env.VITE_CONTROL_WS_URL?.trim();
const configuredSubtitlesWsUrl = import.meta.env.VITE_SUBTITLES_WS_URL?.trim();
const configuredAssistantWsUrl = import.meta.env.VITE_ASSISTANT_WS_URL?.trim();

export const runtimeConfig = {
  apiBaseUrl,
  meetingWsUrl:
    configuredMeetingWsUrl || deriveWebSocketUrl(apiBaseUrl, DEFAULT_MEETING_WS_PATH),
  controlWsUrl:
    configuredControlWsUrl || deriveWebSocketUrl(apiBaseUrl, DEFAULT_CONTROL_WS_PATH),
  subtitlesWsUrl:
    configuredSubtitlesWsUrl || deriveWebSocketUrl(apiBaseUrl, DEFAULT_SUBTITLES_WS_PATH),
  assistantWsUrl:
    configuredAssistantWsUrl || deriveWebSocketUrl(apiBaseUrl, DEFAULT_ASSISTANT_WS_PATH),
} as const;

export function apiUrl(path: string): string {
  return buildApiUrl(runtimeConfig.apiBaseUrl, path);
}
