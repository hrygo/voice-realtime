import { create } from "zustand";
import type {
  InnerOSAnswer,
  InnerOSExchange,
  InnerOSIntent,
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

  // Persistence & history actions
  saveExchangeAction: (meetingId: string, exchangeId: string) => Promise<InnerOSExchange>;
  deleteExchangeAction: (meetingId: string, exchangeId: string) => Promise<void>;
  fetchHistory: (meetingId: string) => Promise<void>;
  dismissUnsavedItem: (queryId: string) => void;
  reset: () => void;
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

  openPanel: () => set({ isPanelOpen: true }),
  closePanel: () => set({ isPanelOpen: false }),
  togglePanel: () => set((state) => ({ isPanelOpen: !state.isPanelOpen })),

  startQuery: (queryId, meetingId, question, intent) =>
    set({
      activeMeetingId: meetingId,
      activeQueryId: queryId,
      activeQuestion: question,
      activeIntent: intent,
      queryStatus: "accepted",
      activeAnswer: null,
      activeAnswerSaved: false,
      activeError: null,
    }),

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
    }),
}));
