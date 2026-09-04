/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_API_BASE_URL?: string;
  readonly VITE_MEETING_WS_URL?: string;
  readonly VITE_CONTROL_WS_URL?: string;
  readonly VITE_SUBTITLES_WS_URL?: string;
  readonly VITE_ASSISTANT_WS_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
