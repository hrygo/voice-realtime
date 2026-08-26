export type MeetingDataSourceMode = "fixture" | "mock" | "backend";

const DEFAULT_MEETING_WS_PATH = "/ws/v1/meetings";
const DEFAULT_CONTROL_WS_PATH = "/ws/v1/control";

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

function readDataSource(value: string | undefined): MeetingDataSourceMode {
  return value === "fixture" || value === "mock" || value === "backend" ? value : "backend";
}

const apiBaseUrl = normalizeBaseUrl(import.meta.env.VITE_API_BASE_URL);
const configuredMeetingWsUrl = import.meta.env.VITE_MEETING_WS_URL?.trim();
const configuredControlWsUrl = import.meta.env.VITE_CONTROL_WS_URL?.trim();

export const runtimeConfig = {
  apiBaseUrl,
  meetingWsUrl:
    configuredMeetingWsUrl || deriveWebSocketUrl(apiBaseUrl, DEFAULT_MEETING_WS_PATH),
  controlWsUrl:
    configuredControlWsUrl || deriveWebSocketUrl(apiBaseUrl, DEFAULT_CONTROL_WS_PATH),
  dataSource: readDataSource(import.meta.env.VITE_DATA_SOURCE),
} as const;

export function apiUrl(path: string): string {
  return buildApiUrl(runtimeConfig.apiBaseUrl, path);
}
