import { create } from "zustand";

/** 主题类型：亮色、暗色、跟随系统。 */
export type Theme = "light" | "dark" | "system";

/** 读取 localStorage 值，隐私模式抛错时返回默认值。 */
function readStorage<T>(key: string, fallback: T): T {
	try {
		const raw = localStorage.getItem(key);
		if (raw === null) return fallback;
		return JSON.parse(raw) as T;
	} catch {
		return fallback;
	}
}

/** 写入 localStorage，隐私模式抛错时静默忽略。 */
function writeStorage(key: string, value: unknown): void {
	try {
		localStorage.setItem(key, JSON.stringify(value));
	} catch {
		// 隐私模式或无存储空间时静默降级。
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
		// 跟随系统时移除 data-theme，让 CSS @media 查询生效。
		root.removeAttribute("data-theme");
	} else {
		root.dataset.theme = theme;
	}
}

interface UISettingsState {
	/** 当前主题。 */
	theme: Theme;
	/** 人格编辑器中的 system prompt 文本。 */
	persona: string;
	/** 当前选中的音色 ID。 */
	voice: string;
	setTheme: (theme: Theme) => void;
	setPersona: (persona: string) => void;
	setVoice: (voice: string) => void;
}

export const useUISettingsStore = create<UISettingsState>((set) => ({
	theme: initialTheme(),
	persona: readStorage<string>("voice-studio:persona", ""),
	voice: readStorage<string>("voice-studio:voice", "default"),
	setTheme: (theme) => {
		set({ theme });
		writeStorage("voice-studio:theme", theme);
		applyTheme(theme);
	},
	setPersona: (persona) => {
		set({ persona });
		writeStorage("voice-studio:persona", persona);
	},
	setVoice: (voice) => {
		set({ voice });
		writeStorage("voice-studio:voice", voice);
	},
}));