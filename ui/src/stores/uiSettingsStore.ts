import { create } from "zustand";
import type { DuplexMode, RuntimeStateSnapshot } from "../protocol";

export type { DuplexMode } from "../protocol";

/** 主题类型：亮色、暗色、跟随系统。 */
export type Theme = "light" | "dark" | "system";

export interface PersonaTemplate {
  id: string;
  name: string;
  prompt: string;
  isBuiltin?: boolean;
}

export const BUILTIN_PERSONAS: readonly PersonaTemplate[] = [
  {
    id: "builtin-default",
    name: "🎙️ 全能智能助理",
    prompt: "你是一个由 Qwen3 驱动的本地实时语音助手，回答简练、自然、口语化，适合直接语音朗读。",
    isBuiltin: true,
  },
  {
    id: "builtin-tech",
    name: "⚡ 极简技术顾问",
    prompt: "你是一个顶尖的高级软件与系统架构专家，回答务必极度精炼、切中要害，用直接清晰的口语解释技术原理，无冗余套话。",
    isBuiltin: true,
  },
  {
    id: "builtin-witty",
    name: "🎭 幽默风趣伙伴",
    prompt: "你是一个性格开朗、机智幽默的智能搭档，用轻松风趣、地道的中文口语与用户对话交流。",
    isBuiltin: true,
  },
  {
    id: "builtin-tutor",
    name: "🇨🇳 中文口语陪练",
    prompt: "你是一个耐心的普通话与口语交流陪练，专注日常口语表达，语气亲切温和，适时提出对话延伸问题。",
    isBuiltin: true,
  },
  {
    id: "builtin-fintech",
    name: "💼 金融科技专家",
    prompt: "你是一个熟悉银行业务、金融科技架构及大模型工程落地实践的资深专家，用专业且通俗的语言回答。",
    isBuiltin: true,
  },
];

export interface TeleprompterSettings {
  mirror: boolean;
  fontSize: number; // in rem e.g. 2.2
  textAlign?: "left" | "center";
}

/** 读取 localStorage 值，隐私模式抛错时返回默认值。 */
function readStorage<T>(key: string, fallback: T): T {
  try {
    if (
      typeof window === "undefined"
      || typeof document === "undefined"
      || document.location.protocol === "about:"
      || (typeof navigator !== "undefined" && navigator.userAgent.includes("jsdom"))
    ) {
      return fallback;
    }
    const raw = document.defaultView?.localStorage.getItem(key) ?? null;
    if (raw === null) return fallback;
    return JSON.parse(raw) as T;
  } catch {
    return fallback;
  }
}

/** 写入 localStorage，隐私模式抛错时静默忽略。 */
function writeStorage(key: string, value: unknown): void {
  try {
    if (
      typeof window === "undefined"
      || typeof document === "undefined"
      || document.location.protocol === "about:"
      || (typeof navigator !== "undefined" && navigator.userAgent.includes("jsdom"))
    ) {
      return;
    }
    document.defaultView?.localStorage.setItem(key, JSON.stringify(value));
  } catch {
    // 静默降级
  }
}

/** 探测系统当前是否偏好暗色。 */
function systemPrefersDark(): boolean {
  try {
    return window.matchMedia("(prefers-color-scheme: dark)").matches;
  } catch {
    return false;
  }
}

/** 初始主题：已有存储读存储，否则按系统偏好决定。 */
export function initialTheme(): Theme {
  const stored = readStorage<string | null>("voice-studio:theme", null);
  if (stored === "light" || stored === "dark" || stored === "system") return stored;
  return systemPrefersDark() ? "dark" : "light";
}

/** 将主题应用到 documentElement 的 data-theme 属性。 */
export function applyTheme(theme: Theme): void {
  const root = document.documentElement;
  if (theme === "system") {
    root.removeAttribute("data-theme");
  } else {
    root.dataset.theme = theme;
  }
}

interface UISettingsState {
  theme: Theme;
  persona: string;
  voice: string;
  duplexMode: DuplexMode;
  customPersonas: PersonaTemplate[];
  micMuted: boolean;
  pipelineStatus: string;
  subtitleStatus: string;
  sessionStartedAt: string | null;
  serverSynchronized: boolean;
  teleprompterSettings: TeleprompterSettings;

  setTheme: (theme: Theme) => void;
  applyRuntimeState: (state: RuntimeStateSnapshot) => void;
  setTeleprompterSettings: (settings: Partial<TeleprompterSettings>) => void;
  addCustomPersona: (name: string, prompt: string) => void;
  updateCustomPersona: (id: string, name: string, prompt: string) => void;
  removeCustomPersona: (id: string) => void;
}

export const useUISettingsStore = create<UISettingsState>((set, get) => ({
  theme: initialTheme(),
  persona: readStorage<string>(
    "voice-studio:persona",
    BUILTIN_PERSONAS[0]?.prompt || "",
  ),
  voice: readStorage<string>("voice-studio:voice", "default"),
  duplexMode: readStorage<DuplexMode>("voice-studio:duplex-mode", "speaker_focus"),
  customPersonas: readStorage<PersonaTemplate[]>("voice-studio:custom-personas", []),
  micMuted: readStorage<boolean>("voice-studio:mic-muted", false),
  pipelineStatus: "unknown",
  subtitleStatus: "unknown",
  sessionStartedAt: null,
  serverSynchronized: false,
  teleprompterSettings: readStorage<TeleprompterSettings>("voice-studio:teleprompter", {
    mirror: false,
    fontSize: 2.2,
    textAlign: "left",
  }),

  setTheme: (theme) => {
    set({ theme });
    writeStorage("voice-studio:theme", theme);
    applyTheme(theme);
  },
  applyRuntimeState: (state) => {
    const persona = state.persona ?? BUILTIN_PERSONAS[0]?.prompt ?? "";
    set({
      persona,
      voice: state.voice,
      duplexMode: state.duplex_mode,
      micMuted: state.mic_muted,
      pipelineStatus: state.pipeline,
      subtitleStatus: state.subtitle,
      sessionStartedAt: state.session_started_at,
      serverSynchronized: true,
    });
    writeStorage("voice-studio:persona", persona);
    writeStorage("voice-studio:voice", state.voice);
    writeStorage("voice-studio:duplex-mode", state.duplex_mode);
    writeStorage("voice-studio:mic-muted", state.mic_muted);
  },
  setTeleprompterSettings: (partial) => {
    const next = { ...get().teleprompterSettings, ...partial };
    set({ teleprompterSettings: next });
    writeStorage("voice-studio:teleprompter", next);
  },
  addCustomPersona: (name, prompt) => {
    const newItem: PersonaTemplate = {
      id: `custom-${Date.now()}`,
      name: name.trim(),
      prompt: prompt.trim(),
    };
    const updated = [...get().customPersonas, newItem];
    set({ customPersonas: updated });
    writeStorage("voice-studio:custom-personas", updated);
  },
  updateCustomPersona: (id, name, prompt) => {
    const updated = get().customPersonas.map((p) =>
      p.id === id ? { ...p, name: name.trim(), prompt: prompt.trim() } : p,
    );
    set({ customPersonas: updated });
    writeStorage("voice-studio:custom-personas", updated);
  },
  removeCustomPersona: (id) => {
    const updated = get().customPersonas.filter((p) => p.id !== id);
    set({ customPersonas: updated });
    writeStorage("voice-studio:custom-personas", updated);
  },
}));
