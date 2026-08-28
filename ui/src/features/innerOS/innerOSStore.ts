import { create } from "zustand";
import type {
  InnerOSAnswer,
  InnerOSExchange,
  InnerOSIntent,
  QuickPromptItem,
} from "./contracts";
import { innerOSApi } from "./api";

export type QueryStatus =
  | "idle"
  | "accepted"
  | "generating"
  | "completed"
  | "failed"
  | "cancelled";

export interface UnsavedExchangeItem {
  readonly queryId: string;
  readonly meetingId: string;
  readonly question: string;
  readonly intent: InnerOSIntent;
  readonly answer: InnerOSAnswer;
  readonly createdAt: string;
  saved: boolean;
  isExpanded?: boolean;
}

export interface InnerOSState {
  isPanelOpen: boolean;
  activeMeetingId: string | null;
  activeQueryId: string | null;
  activeQuestion: string | null;
  activeIntent: InnerOSIntent | null;
  queryStatus: QueryStatus;
  activeAnswer: InnerOSAnswer | null;
  activeAnswerSaved: boolean;
  activeError: { code: string; message: string } | null;
  unsavedExchanges: readonly UnsavedExchangeItem[];
  historyList: readonly InnerOSExchange[];
  isLoadingHistory: boolean;

  // Question navigation history (for terminal-style Up/Down key in textarea)
  questionHistory: readonly string[];

  // User-defined pinned quick prompts
  customPrompts: readonly QuickPromptItem[];

  // Panel actions
  openPanel: () => void;
  closePanel: () => void;
  togglePanel: () => void;

  // Query lifecycle actions
  startQuery: (queryId: string, meetingId: string, question: string, intent: InnerOSIntent) => void;
  setAccepted: (queryId: string) => void;
  setGenerating: (queryId: string) => void;
  setCompleted: (queryId: string, answer: InnerOSAnswer) => void;
  setFailed: (queryId: string, code: string, message: string) => void;
  setCancelled: (queryId: string, reason?: string) => void;
  clearActiveQuery: () => void;

  // Question navigation & Custom Prompts
  addQuestionHistory: (question: string) => void;
  addCustomPrompt: (label: string, question: string, intent: InnerOSIntent) => void;
  removeCustomPrompt: (promptId: string) => void;

  // Persistence & history actions
  saveExchangeAction: (meetingId: string, exchangeId: string) => Promise<InnerOSExchange>;
  saveAllExchangesAction: (meetingId: string) => Promise<number>;
  deleteExchangeAction: (meetingId: string, exchangeId: string) => Promise<void>;
  fetchHistory: (meetingId: string) => Promise<void>;
  dismissUnsavedItem: (queryId: string) => void;
  toggleItemExpanded: (queryId: string) => void;
  exportNotesAsMarkdown: (meetingTitle?: string) => string;
  reset: () => void;
}

const CUSTOM_PROMPTS_KEY = "vr_inner_os_custom_prompts";

function loadStoredCustomPrompts(): QuickPromptItem[] {
  try {
    const raw = localStorage.getItem(CUSTOM_PROMPTS_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

function saveStoredCustomPrompts(prompts: readonly QuickPromptItem[]): void {
  try {
    localStorage.setItem(CUSTOM_PROMPTS_KEY, JSON.stringify(prompts));
  } catch {
    // ignore storage quotas
  }
}

export const useInnerOSStore = create<InnerOSState>((set, get) => ({
  isPanelOpen: false,
  activeMeetingId: null,
  activeQueryId: null,
  activeQuestion: null,
  activeIntent: null,
  queryStatus: "idle",
  activeAnswer: null,
  activeAnswerSaved: false,
  activeError: null,
  unsavedExchanges: [],
  historyList: [],
  isLoadingHistory: false,
  questionHistory: [],
  customPrompts: loadStoredCustomPrompts(),

  openPanel: () => set({ isPanelOpen: true }),
  closePanel: () => set({ isPanelOpen: false }),
  togglePanel: () => set((state) => ({ isPanelOpen: !state.isPanelOpen })),

  startQuery: (queryId, meetingId, question, intent) => {
    get().addQuestionHistory(question);
    set({
      activeMeetingId: meetingId,
      activeQueryId: queryId,
      activeQuestion: question,
      activeIntent: intent,
      queryStatus: "accepted",
      activeAnswer: null,
      activeAnswerSaved: false,
      activeError: null,
    });
  },

  setAccepted: (queryId) => {
    if (get().activeQueryId === queryId) {
      set({ queryStatus: "accepted" });
    }
  },

  setGenerating: (queryId) => {
    if (get().activeQueryId === queryId) {
      set({ queryStatus: "generating" });
    }
  },

  setCompleted: (queryId, answer) => {
    const { activeMeetingId, activeQuestion, activeIntent, unsavedExchanges } = get();
    if (get().activeQueryId === queryId) {
      const newItem: UnsavedExchangeItem = {
        queryId,
        meetingId: activeMeetingId || "",
        question: activeQuestion || "",
        intent: activeIntent || "mixed",
        answer,
        createdAt: new Date().toISOString(),
        saved: false,
        isExpanded: true,
      };
      set({
        queryStatus: "completed",
        activeAnswer: answer,
        activeAnswerSaved: false,
        unsavedExchanges: [newItem, ...unsavedExchanges.filter((item) => item.queryId !== queryId)],
      });
    }
  },

  setFailed: (queryId, code, message) => {
    if (get().activeQueryId === queryId) {
      set({
        queryStatus: "failed",
        activeError: { code, message },
      });
    }
  },

  setCancelled: (queryId) => {
    if (get().activeQueryId === queryId) {
      set({
        queryStatus: "cancelled",
        activeQueryId: null,
      });
    }
  },

  clearActiveQuery: () =>
    set({
      activeQueryId: null,
      activeQuestion: null,
      activeIntent: null,
      queryStatus: "idle",
      activeAnswer: null,
      activeAnswerSaved: false,
      activeError: null,
    }),

  addQuestionHistory: (question: string) => {
    const q = question.trim();
    if (!q) return;
    set((state) => {
      const filtered = state.questionHistory.filter((item) => item !== q);
      return { questionHistory: [q, ...filtered].slice(0, 30) };
    });
  },

  addCustomPrompt: (label: string, question: string, intent: InnerOSIntent) => {
    const id = `custom_${Date.now()}_${Math.random().toString(36).slice(2, 6)}`;
    const newPrompt: QuickPromptItem = {
      id,
      category: "custom",
      label: label.trim(),
      question: question.trim(),
      intent,
      isCustom: true,
    };
    set((state) => {
      const updated = [newPrompt, ...state.customPrompts];
      saveStoredCustomPrompts(updated);
      return { customPrompts: updated };
    });
  },

  removeCustomPrompt: (promptId: string) => {
    set((state) => {
      const updated = state.customPrompts.filter((p) => p.id !== promptId);
      saveStoredCustomPrompts(updated);
      return { customPrompts: updated };
    });
  },

  saveExchangeAction: async (meetingId, exchangeId) => {
    const saved = await innerOSApi.saveExchange(meetingId, exchangeId);
    set((state) => ({
      activeAnswerSaved: state.activeQueryId === exchangeId ? true : state.activeAnswerSaved,
      unsavedExchanges: state.unsavedExchanges.map((item) =>
        item.queryId === exchangeId ? { ...item, saved: true } : item,
      ),
      historyList: [saved, ...state.historyList.filter((h) => h.id !== exchangeId)],
    }));
    return saved;
  },

  saveAllExchangesAction: async (meetingId: string) => {
    const { unsavedExchanges } = get();
    const toSave = unsavedExchanges.filter((item) => !item.saved);
    let count = 0;
    for (const item of toSave) {
      try {
        await get().saveExchangeAction(meetingId, item.queryId);
        count++;
      } catch {
        // continue saving other items
      }
    }
    return count;
  },

  deleteExchangeAction: async (meetingId, exchangeId) => {
    await innerOSApi.deleteExchange(meetingId, exchangeId);
    set((state) => ({
      historyList: state.historyList.filter((h) => h.id !== exchangeId),
      unsavedExchanges: state.unsavedExchanges.filter((item) => item.queryId !== exchangeId),
    }));
  },

  fetchHistory: async (meetingId) => {
    set({ isLoadingHistory: true });
    try {
      const res = await innerOSApi.listExchanges(meetingId);
      set({ historyList: res?.items || [], isLoadingHistory: false });
    } catch {
      set({ historyList: [], isLoadingHistory: false });
    }
  },

  dismissUnsavedItem: (queryId) =>
    set((state) => ({
      unsavedExchanges: state.unsavedExchanges.filter((item) => item.queryId !== queryId),
    })),

  toggleItemExpanded: (queryId) =>
    set((state) => ({
      unsavedExchanges: state.unsavedExchanges.map((item) =>
        item.queryId === queryId ? { ...item, isExpanded: !item.isExpanded } : item,
      ),
    })),

  exportNotesAsMarkdown: (meetingTitle?: string) => {
    const { unsavedExchanges, historyList } = get();
    const title = meetingTitle || "Voice Studio 会议";
    const dateStr = new Date().toLocaleString("zh-CN", { hour12: false });

    const allMap = new Map<string, { question: string; answer: InnerOSAnswer; createdAt: string }>();

    for (const h of historyList) {
      allMap.set(h.id, { question: h.question, answer: h.answer, createdAt: h.created_at });
    }
    for (const u of unsavedExchanges) {
      if (!allMap.has(u.queryId)) {
        allMap.set(u.queryId, { question: u.question, answer: u.answer, createdAt: u.createdAt });
      }
    }

    const items = Array.from(allMap.values());
    if (items.length === 0) {
      return `# ${title} · 内心 OS 私密副驾驶笔记\n\n> 记录时间: ${dateStr}\n\n*暂无问答记录*\n`;
    }

    let md = `# ${title} · 内心 OS 私密副驾驶笔记\n\n> 记录时间: ${dateStr}\n> 共有 ${items.length} 轮私密研判问答记录（单机离线生成，仅本地可见）\n\n---\n\n`;

    items.forEach((item, index) => {
      const timeTag = new Date(item.createdAt).toLocaleTimeString();
      md += `### ${index + 1}. ${item.question} (${timeTag})\n\n`;

      if (item.answer.facts && item.answer.facts.length > 0) {
        md += `#### 事实依据 (Facts)\n`;
        item.answer.facts.forEach((f) => {
          md += `- ${f.text}\n`;
        });
        md += `\n`;
      }

      if (item.answer.judgements && item.answer.judgements.length > 0) {
        md += `#### 局势研判 (Judgements)\n`;
        item.answer.judgements.forEach((j) => {
          const unc = j.uncertainty === "low" ? "低不确定性" : j.uncertainty === "medium" ? "中不确定性" : "高不确定性";
          md += `- ${j.text} *[${unc} · ${j.uncertainty_reason}]*\n`;
        });
        md += `\n`;
      }

      if (item.answer.draft?.text) {
        md += `#### 建议发言草稿 (Draft)\n`;
        md += `> ${item.answer.draft.text}\n\n`;
      }

      md += `---\n\n`;
    });

    return md;
  },

  reset: () =>
    set({
      activeMeetingId: null,
      activeQueryId: null,
      activeQuestion: null,
      activeIntent: null,
      queryStatus: "idle",
      activeAnswer: null,
      activeAnswerSaved: false,
      activeError: null,
      unsavedExchanges: [],
      historyList: [],
      questionHistory: [],
    }),
}));
