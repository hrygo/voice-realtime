import { create } from "zustand";
import {
  type MeetingDetail,
  type MeetingMinutesVersion,
  type MeetingSnapshotPayload,
  type MeetingSpeaker,
  type MeetingStatus,
  type MeetingSummary,
  type MinutesStatus,
  type StorageHealth,
  type TranscriptSegment,
} from "../contracts/meetingContract";
import { meetingApi } from "../services/meetingApi";

export interface TranscriptionGap {
  readonly start_ms: number;
  readonly end_ms: number;
  readonly reason?: string;
}

export interface MeetingHealthState {
  readonly storage: StorageHealth;
  readonly transcription: string;
  readonly mic_muted: boolean;
  readonly recovery_journal_active: boolean;
}

export interface MeetingStoreState {
  // Active Recording Session
  readonly activeMeetingId: string | null;
  readonly activeMeeting: MeetingDetail | null;
  readonly status: MeetingStatus | "idle";
  readonly segments: readonly TranscriptSegment[];
  readonly partialText: string | null;
  readonly partialSpeaker: string | null;
  readonly transcriptRevision: number;
  readonly contentRevision: number;
  readonly speakers: Record<string, MeetingSpeaker>;
  readonly gaps: readonly TranscriptionGap[];
  readonly minutes: MeetingMinutesVersion | null;
  readonly minutesHistory: readonly MeetingMinutesVersion[];
  readonly activeMinutesVersion: number | null;
  readonly health: MeetingHealthState;
  readonly isFinalizing: boolean;
  readonly sessionStartedAt: string | null;
  readonly sessionEndedAt: string | null;
  readonly interruptionReason: string | null;
  readonly errorMessage: string | null;

  // History Browsing / Detail View
  readonly historyList: readonly MeetingSummary[];
  readonly nextCursor: string | null;
  readonly isLoadingHistory: boolean;
  readonly selectedMeetingId: string | null;
  readonly selectedMeeting: MeetingDetail | null;
  readonly selectedSegments: readonly TranscriptSegment[];
  readonly selectedMinutes: MeetingMinutesVersion | null;
  readonly selectedMinutesVersion: number | null;
  readonly selectedMinutesList: readonly MeetingMinutesVersion[];
  readonly isLoadingSelected: boolean;

  // Active Session Actions
  readonly setPartial: (text: string | null, speakerName?: string | null) => void;
  readonly reconcileTranscript: (
    replaceFromMs: number,
    newSegments: TranscriptSegment[],
    transcriptRevision: number,
    contentRevision: number,
  ) => void;
  readonly applySnapshot: (snapshot: MeetingSnapshotPayload) => void;
  readonly updateMeetingState: (
    status: MeetingStatus,
    startedAt?: string | null,
    endedAt?: string | null,
    reason?: string | null,
    meetingId?: string | null,
  ) => void;
  readonly setSpeaker: (speakerKey: string, displayName: string, contentRevision: number) => void;
  readonly setMinutesState: (
    version: number,
    status: MinutesStatus,
    errCode?: string | null,
    errMsg?: string | null,
    minutes?: MeetingMinutesVersion | null,
    meetingId?: string | null,
    minutesId?: string | null,
  ) => void;
  readonly setActiveMinutesVersion: (version: number) => void;
  readonly addGap: (start_ms: number, end_ms: number, reason?: string) => void;
  readonly updateHealth: (health: Partial<MeetingHealthState>) => void;
  readonly setErrorMessage: (msg: string | null) => void;
  readonly resetActiveSession: () => void;
  readonly syncBaselineTranscript: (meetingId: string) => Promise<void>;

  readonly returnToActiveMeeting: () => void;

  // History Actions
  readonly fetchHistory: (cursor?: string | null) => Promise<void>;
  readonly selectMeeting: (id: string | null) => Promise<void>;
  readonly updateMeetingTitle: (id: string, title: string) => Promise<void>;
  readonly updateSpeakerName: (id: string, speakerKey: string, displayName: string) => Promise<void>;
  readonly triggerGenerateMinutes: (id: string) => Promise<void>;
  readonly selectHistoryMinutesVersion: (id: string, version: number) => Promise<void>;
  readonly deleteMeeting: (id: string) => Promise<void>;
}

export const useMeetingStore = create<MeetingStoreState>((set, get) => ({
  // Active Recording Session Initial State
  activeMeetingId: null,
  activeMeeting: null,
  status: "idle",
  segments: [],
  partialText: null,
  partialSpeaker: null,
  transcriptRevision: 0,
  contentRevision: 0,
  speakers: {},
  gaps: [],
  minutes: null,
  minutesHistory: [],
  activeMinutesVersion: null,
  health: {
    storage: "ok",
    transcription: "ok",
    mic_muted: false,
    recovery_journal_active: false,
  },
  isFinalizing: false,
  sessionStartedAt: null,
  sessionEndedAt: null,
  interruptionReason: null,
  errorMessage: null,

  // History Initial State
  historyList: [],
  nextCursor: null,
  isLoadingHistory: false,
  selectedMeetingId: null,
  selectedMeeting: null,
  selectedSegments: [],
  selectedMinutes: null,
  selectedMinutesVersion: null,
  selectedMinutesList: [],
  isLoadingSelected: false,

  setPartial: (text, speakerName) => {
    set({
      partialText: text || null,
      partialSpeaker: speakerName || null,
    });
  },

  /**
   * 滑动窗口转录对账算法 (§8)
   * 1. 保留 end_ms < replace_from_ms 的稳定历史
   * 2. 删除所有与活动窗口重叠的段 (end_ms >= replace_from_ms)
   * 3. 插入当前窗口的最新段并按 start_ms, order 稳定排序
   * 4. 更新 revision，清空已提交对应的 partial
   */
  reconcileTranscript: (replaceFromMs, newSegments, transcriptRevision, contentRevision) => {
    const currentSegments = get().segments;

    // 保留与新窗口无重叠的历史（结束时间在替换起始点之前或等于替换点）
    const stableHistory = currentSegments.filter((seg) => seg.end_ms <= replaceFromMs);

    // 过滤掉 newSegments 中可能与 stableHistory id 重复的段
    const existingIds = new Set(stableHistory.map((s) => s.id));
    const dedupedNew = newSegments.filter((s) => !existingIds.has(s.id));

    // 合并新段
    const combined = [...stableHistory, ...dedupedNew];

    // 稳定排序: 优先按 start_ms，相同按 order
    combined.sort((a, b) => {
      if (a.start_ms !== b.start_ms) {
        return a.start_ms - b.start_ms;
      }
      return a.order - b.order;
    });

    set({
      segments: combined,
      transcriptRevision,
      contentRevision,
      partialText: null, // 清空过时的 partial
    });
  },

  applySnapshot: (snapshot) => {
    const meeting = snapshot.meeting;
    const isFinalizing = meeting.status === "finalizing";

    set((state) => ({
      activeMeetingId: meeting.id,
      status: meeting.status,
      sessionStartedAt: meeting.started_at,
      sessionEndedAt: meeting.ended_at,
      interruptionReason: meeting.interruption_reason || null,
      transcriptRevision: Math.max(state.transcriptRevision, snapshot.transcript_revision),
      contentRevision: Math.max(state.contentRevision, snapshot.content_revision),
      partialText: snapshot.partial || null,
      isFinalizing,
      health: snapshot.health
        ? {
            ...state.health,
            storage: snapshot.health.storage || state.health.storage,
            transcription: snapshot.health.transcription || state.health.transcription,
            mic_muted: snapshot.health.mic_muted ?? state.health.mic_muted,
            recovery_journal_active:
              snapshot.health.recovery_journal_active ?? state.health.recovery_journal_active,
          }
        : state.health,
    }));
  },

  updateMeetingState: (status, startedAt, endedAt, reason, meetingId) => {
    set((state) => {
      const effectiveMeetingId =
        meetingId !== undefined && meetingId !== null && meetingId !== ""
          ? meetingId
          : state.activeMeetingId;

      const exists = state.historyList.some((m) => m.id === effectiveMeetingId);
      let updatedHistoryList = state.historyList;
      if (effectiveMeetingId) {
        if (exists) {
          updatedHistoryList = state.historyList.map((m) =>
            m.id === effectiveMeetingId
              ? {
                  ...m,
                  status,
                  started_at: startedAt !== undefined && startedAt !== null ? startedAt : m.started_at,
                  ended_at: endedAt !== undefined ? endedAt : m.ended_at,
                  interruption_reason: reason !== undefined ? reason : m.interruption_reason,
                }
              : m,
          );
        } else if (state.activeMeeting || status === "recording" || status === "completed") {
          updatedHistoryList = [
            {
              id: effectiveMeetingId,
              title: state.activeMeeting?.title || "新会议",
              status,
              language: state.activeMeeting?.language || "Chinese",
              started_at: startedAt || state.sessionStartedAt || new Date().toISOString(),
              ended_at: endedAt || null,
              transcript_revision: state.transcriptRevision,
              content_revision: state.contentRevision,
              interruption_reason: reason || null,
              created_at: startedAt || state.sessionStartedAt || new Date().toISOString(),
            },
            ...state.historyList,
          ];
        }
      }

      return {
        status,
        activeMeetingId: effectiveMeetingId,
        isFinalizing: status === "finalizing",
        sessionStartedAt: startedAt !== undefined ? startedAt : state.sessionStartedAt,
        sessionEndedAt: endedAt !== undefined ? endedAt : state.sessionEndedAt,
        interruptionReason: reason !== undefined ? reason : state.interruptionReason,
        historyList: updatedHistoryList,
      };
    });
  },

  setSpeaker: (speakerKey, displayName, contentRevision) => {
    set((state) => {
      const existingSpeaker = state.speakers[speakerKey];
      const updatedSpeakers = {
        ...state.speakers,
        [speakerKey]: {
          speaker_key: speakerKey,
          default_label: existingSpeaker?.default_label || speakerKey,
          display_name: displayName,
          updated_at: new Date().toISOString(),
        },
      };

      // 同步更新 segments 中的 speaker_name
      const updatedSegments = state.segments.map((seg) =>
        seg.speaker_key === speakerKey ? { ...seg, speaker_name: displayName } : seg,
      );

      // 若纪要已生成，标记旧版本纪要为 stale
      const updatedMinutes = state.minutes
        ? { ...state.minutes, is_stale: contentRevision > state.minutes.source_content_revision }
        : null;

      return {
        speakers: updatedSpeakers,
        segments: updatedSegments,
        contentRevision,
        minutes: updatedMinutes,
      };
    });
  },

  setMinutesState: (
    version,
    status,
    errCode,
    errMsg,
    minutesData,
    meetingId,
    minutesId,
  ) => {
    set((state) => {
      const isTargetActive =
        !meetingId ||
        meetingId === state.activeMeetingId ||
        (!state.selectedMeetingId && Boolean(state.activeMeetingId));
      const isTargetSelected =
        Boolean(meetingId) &&
        (meetingId === state.selectedMeetingId || meetingId === state.selectedMeeting?.id);

      const resolveMinutesItem = (
        existing: MeetingMinutesVersion | null,
        targetMeetingId: string,
      ): MeetingMinutesVersion => {
        if (minutesData) {
          return {
            ...minutesData,
            is_stale: state.contentRevision > minutesData.source_content_revision,
          };
        }
        if (existing && existing.version === version) {
          return {
            ...existing,
            status,
            error_code: errCode !== undefined ? errCode : existing.error_code,
            error_message: errMsg !== undefined ? errMsg : existing.error_message,
          };
        }
        return {
          id: minutesId || existing?.id || "",
          meeting_id: targetMeetingId,
          version,
          status,
          source_content_revision: existing?.source_content_revision ?? state.contentRevision,
          model: existing?.model || "qwen/qwen3.8-27b",
          prompt_version: existing?.prompt_version || "v1",
          content_json: null,
          content_markdown: null,
          raw_output: null,
          error_code: errCode || null,
          error_message: errMsg || null,
          is_stale: false,
          created_at: existing?.created_at || new Date().toISOString(),
        };
      };

      let nextMinutes = state.minutes;
      let nextActiveVersion = state.activeMinutesVersion;
      let nextMinutesHistory = state.minutesHistory;
      let nextSelectedMinutes = state.selectedMinutes;
      let nextSelectedVersion = state.selectedMinutesVersion;
      let nextSelectedList = state.selectedMinutesList;

      // 1. 更新活跃会议纪要
      if (isTargetActive) {
        const targetActiveMeetingId = meetingId || state.activeMeetingId || "";
        nextMinutes = resolveMinutesItem(state.minutes, targetActiveMeetingId);
        nextActiveVersion = nextMinutes.version;
        const updatedHistory = state.minutesHistory.filter((m) => m.version !== version);
        updatedHistory.push(nextMinutes);
        updatedHistory.sort((a, b) => a.version - b.version);
        nextMinutesHistory = updatedHistory;
      }

      // 2. 更新选中历史会议纪要
      if (isTargetSelected) {
        const targetSelectedId = state.selectedMeetingId || meetingId || "";
        nextSelectedMinutes = resolveMinutesItem(state.selectedMinutes, targetSelectedId);
        nextSelectedVersion = nextSelectedMinutes.version;
        const updatedSelectedList = state.selectedMinutesList.filter((m) => m.version !== version);
        updatedSelectedList.push(nextSelectedMinutes);
        updatedSelectedList.sort((a, b) => a.version - b.version);
        nextSelectedList = updatedSelectedList;
      }

      const nextSelectedMeeting =
        state.selectedMeeting && state.selectedMeeting.id === (meetingId || state.selectedMeetingId)
          ? {
              ...state.selectedMeeting,
              latest_minutes: nextSelectedMinutes || state.selectedMeeting.latest_minutes,
            }
          : state.selectedMeeting;

      return {
        minutes: nextMinutes,
        activeMinutesVersion: nextActiveVersion,
        minutesHistory: nextMinutesHistory,
        selectedMinutes: nextSelectedMinutes,
        selectedMinutesVersion: nextSelectedVersion,
        selectedMinutesList: nextSelectedList,
        selectedMeeting: nextSelectedMeeting,
      };
    });
  },

  setActiveMinutesVersion: (version) => {
    const history = get().minutesHistory;
    const target = history.find((m) => m.version === version);
    if (target) {
      set({
        minutes: target,
        activeMinutesVersion: version,
      });
    }
  },

  addGap: (start_ms, end_ms, reason) => {
    set((state) => ({
      gaps: [...state.gaps, { start_ms, end_ms, reason }],
    }));
  },

  updateHealth: (healthUpdate) => {
    set((state) => ({
      health: { ...state.health, ...healthUpdate },
    }));
  },

  setErrorMessage: (msg) => {
    set({ errorMessage: msg });
  },

  resetActiveSession: () => {
    set({
      activeMeetingId: null,
      activeMeeting: null,
      status: "idle",
      segments: [],
      partialText: null,
      partialSpeaker: null,
      transcriptRevision: 0,
      contentRevision: 0,
      speakers: {},
      gaps: [],
      minutes: null,
      minutesHistory: [],
      activeMinutesVersion: null,
      isFinalizing: false,
      sessionStartedAt: null,
      sessionEndedAt: null,
      interruptionReason: null,
      errorMessage: null,
      selectedMeetingId: null,
      selectedMeeting: null,
      selectedSegments: [],
      selectedMinutes: null,
      selectedMinutesVersion: null,
      selectedMinutesList: [],
      isLoadingSelected: false,
    });
  },

  syncBaselineTranscript: async (meetingId) => {
    try {
      const [transcriptResp, meetingDetail] = await Promise.allSettled([
        meetingApi.fetchTranscript(meetingId),
        meetingApi.fetchMeeting(meetingId),
      ]);

      if (transcriptResp.status === "fulfilled") {
        const resp = transcriptResp.value;
        set({
          segments: resp.segments,
          transcriptRevision: resp.transcript_revision,
          contentRevision: resp.content_revision,
        });
      }

      if (meetingDetail.status === "fulfilled") {
        const detail = meetingDetail.value;
        set((state) => ({
          activeMeeting: detail,
          speakers: detail.speakers || state.speakers,
          minutes: detail.latest_minutes || state.minutes,
          minutesHistory: detail.latest_minutes
            ? [detail.latest_minutes]
            : state.minutesHistory,
        }));
      }
    } catch {
      // 网络瞬时异常，由重连退避重试
    }
  },

  returnToActiveMeeting: () => {
    set({
      selectedMeetingId: null,
      selectedMeeting: null,
      selectedSegments: [],
      selectedMinutes: null,
      selectedMinutesVersion: null,
      selectedMinutesList: [],
      isLoadingSelected: false,
    });
  },

  // 历史会议列表加载
  fetchHistory: async (cursor = null) => {
    set({ isLoadingHistory: true });
    try {
      const resp = await meetingApi.fetchMeetings(cursor, 20);
      set((state) => ({
        historyList: cursor ? [...state.historyList, ...resp.items] : resp.items,
        nextCursor: resp.next_cursor,
        isLoadingHistory: false,
      }));
    } catch (err) {
      set({ isLoadingHistory: false });
    }
  },

  // 选中历史会议详情
  selectMeeting: async (id) => {
    const state = get();
    const isActiveMeeting =
      Boolean(id) &&
      id === state.activeMeetingId &&
      (state.status === "recording" || state.status === "finalizing");

    if (!id || isActiveMeeting) {
      get().returnToActiveMeeting();
      return;
    }

    set({ selectedMeetingId: id, isLoadingSelected: true });
    try {
      const [detail, transcript] = await Promise.all([
        meetingApi.fetchMeeting(id),
        meetingApi.fetchTranscript(id),
      ]);

      const latestMin = detail.latest_minutes || null;
      set({
        selectedMeeting: detail,
        selectedSegments: transcript.segments,
        selectedMinutes: latestMin,
        selectedMinutesVersion: latestMin ? latestMin.version : null,
        selectedMinutesList: latestMin ? [latestMin] : [],
        isLoadingSelected: false,
      });
    } catch {
      set({ isLoadingSelected: false });
    }
  },

  updateMeetingTitle: async (id, title) => {
    const updated = await meetingApi.updateMeetingTitle(id, title);
    set((state) => ({
      selectedMeeting:
        state.selectedMeeting?.id === id
          ? { ...state.selectedMeeting, title: updated.title }
          : state.selectedMeeting,
      activeMeeting:
        state.activeMeeting?.id === id
          ? { ...state.activeMeeting, title: updated.title }
          : state.activeMeeting,
      historyList: state.historyList.map((m) =>
        m.id === id ? { ...m, title: updated.title } : m,
      ),
    }));
  },

  updateSpeakerName: async (id, speakerKey, displayName) => {
    const updated = await meetingApi.updateSpeakerName(id, speakerKey, displayName);
    set((state) => {
      // 如果当前是查看历史详情
      if (state.selectedMeeting?.id === id) {
        const speakers = { ...state.selectedMeeting.speakers, [speakerKey]: updated };
        const segments = state.selectedSegments.map((seg) =>
          seg.speaker_key === speakerKey ? { ...seg, speaker_name: displayName } : seg,
        );
        const isStale = state.selectedMinutes ? true : false;
        return {
          selectedMeeting: { ...state.selectedMeeting, speakers },
          selectedSegments: segments,
          selectedMinutes: state.selectedMinutes
            ? { ...state.selectedMinutes, is_stale: isStale }
            : null,
        };
      }
      return {};
    });
  },

  triggerGenerateMinutes: async (id) => {
    const newMinutes = await meetingApi.generateMinutes(id);
    set((state) => {
      let nextSelectedMinutes = state.selectedMinutes;
      let nextSelectedVersion = state.selectedMinutesVersion;
      let nextSelectedList = state.selectedMinutesList;
      let nextMinutes = state.minutes;
      let nextActiveVersion = state.activeMinutesVersion;
      let nextMinutesHistory = state.minutesHistory;

      if (state.selectedMeeting?.id === id || state.selectedMeetingId === id) {
        const list = [
          ...state.selectedMinutesList.filter((m) => m.version !== newMinutes.version),
          newMinutes,
        ];
        nextSelectedMinutes = newMinutes;
        nextSelectedVersion = newMinutes.version;
        nextSelectedList = list;
      }
      if (state.activeMeetingId === id) {
        const list = [
          ...state.minutesHistory.filter((m) => m.version !== newMinutes.version),
          newMinutes,
        ];
        nextMinutes = newMinutes;
        nextActiveVersion = newMinutes.version;
        nextMinutesHistory = list;
      }
      return {
        selectedMinutes: nextSelectedMinutes,
        selectedMinutesVersion: nextSelectedVersion,
        selectedMinutesList: nextSelectedList,
        minutes: nextMinutes,
        activeMinutesVersion: nextActiveVersion,
        minutesHistory: nextMinutesHistory,
      };
    });
  },

  selectHistoryMinutesVersion: async (id, version) => {
    const versionData = await meetingApi.fetchMinutesVersion(id, version);
    set({
      selectedMinutes: versionData,
      selectedMinutesVersion: version,
    });
  },

  deleteMeeting: async (id) => {
    await meetingApi.deleteMeeting(id);
    set((state) => ({
      historyList: state.historyList.filter((m) => m.id !== id),
      selectedMeetingId: state.selectedMeetingId === id ? null : state.selectedMeetingId,
      selectedMeeting: state.selectedMeetingId === id ? null : state.selectedMeeting,
      selectedSegments: state.selectedMeetingId === id ? [] : state.selectedSegments,
      selectedMinutes: state.selectedMeetingId === id ? null : state.selectedMinutes,
      selectedMinutesList: state.selectedMeetingId === id ? [] : state.selectedMinutesList,
      activeMeetingId: state.activeMeetingId === id ? null : state.activeMeetingId,
      activeMeeting: state.activeMeetingId === id ? null : state.activeMeeting,
      status: state.activeMeetingId === id ? "idle" : state.status,
    }));
  },
}));
