import { create } from "zustand";

/** SpeechRail 字幕快照的前端消费字段。 */
export interface SubtitleLine {
  speaker: number;
  text: string;
  start: string;
  end: string;
  translation?: string | null;
  detected_language?: string | null;
}

export interface SubtitleSnapshot {
  lines: SubtitleLine[];
  buffer_transcription: string;
  buffer_diarization?: string;
  remaining_time?: number;
}

interface SubtitleState {
  lines: SubtitleLine[];
  partial: string;
  connected: boolean;
  starredIndices: Set<number>;
  applySnapshot: (snap: Partial<SubtitleSnapshot>) => void;
  setConnected: (v: boolean) => void;
  toggleStar: (index: number) => void;
  clear: () => void;
}

export interface SubtitleReducerState {
  readonly lines: SubtitleLine[];
  readonly rawLines?: SubtitleLine[];
  readonly partial: string;
  readonly clearedOffset?: number;
}

export function reduceSubtitleSnapshot(
  state: SubtitleReducerState,
  snap: Partial<SubtitleSnapshot>,
): SubtitleReducerState {
  const rawLines = snap.lines ?? state.rawLines ?? state.lines;
  const rawCount = rawLines.length;
  const currentCleared = state.clearedOffset ?? 0;
  // 如果 SpeechRail 新 session 导致 rawLines 变短，重置 offset
  const clearedOffset = currentCleared > rawCount ? 0 : currentCleared;
  const visibleLines = rawLines.slice(clearedOffset);

  return {
    rawLines,
    lines: visibleLines,
    partial: snap.buffer_transcription ?? state.partial,
    clearedOffset,
  };
}

interface SubtitleState {
  lines: SubtitleLine[];
  rawLines: SubtitleLine[];
  partial: string;
  connected: boolean;
  starredIndices: Set<number>;
  clearedOffset: number;
  applySnapshot: (snap: Partial<SubtitleSnapshot>) => void;
  setConnected: (v: boolean) => void;
  toggleStar: (index: number) => void;
  clear: () => void;
}

export const useSubtitleStore = create<SubtitleState>((set) => ({
  lines: [],
  rawLines: [],
  partial: "",
  connected: false,
  starredIndices: new Set<number>(),
  clearedOffset: 0,
  applySnapshot: (snap) =>
    set((state) => {
      const reduced = reduceSubtitleSnapshot(state, snap);
      return {
        ...state,
        ...reduced,
      };
    }),
  setConnected: (v) => set({ connected: v }),
  toggleStar: (index) =>
    set((s) => {
      const next = new Set(s.starredIndices);
      if (next.has(index)) {
        next.delete(index);
      } else {
        next.add(index);
      }
      return { starredIndices: next };
    }),
  clear: () =>
    set((state) => {
      const totalRaw = state.rawLines.length > 0 ? state.rawLines.length : (state.lines.length + state.clearedOffset);
      return {
        clearedOffset: totalRaw,
        lines: [],
        partial: "",
        starredIndices: new Set<number>(),
      };
    }),
}));

/** 格式化说话人展示名：将未聚类/初始负数统一归一化为说话人 0，避免出现未知的混淆。 */
export function formatSpeaker(speaker: number): string {
  const normalized = speaker >= 0 ? speaker : 0;
  return `说话人 ${normalized}`;
}

/** 说话人配色：按 speaker 取色，超过 8 轮换。 */
export function speakerColor(speaker: number): string {
  const palette = [
    "#2563eb",
    "#16a34a",
    "#ea580c",
    "#9333ea",
    "#0891b2",
    "#e11d48",
    "#65a30d",
    "#475569",
  ];
  const normalized = speaker >= 0 ? speaker : 0;
  return palette[normalized % palette.length];
}

/** 生成 SRT 文件内容（索引 + 时间戳 + 文本 + 空行）。 */
export function toSRT(lines: SubtitleLine[]): string {
  return lines
    .map((line, i) => {
      const start = srtTime(line.start);
      const end = srtTime(line.end) || start;
      const text = line.translation && line.translation.trim()
        ? `${line.text}\n${line.translation}`
        : line.text;
      return `${i + 1}\n${start} --> ${end}\n${text}\n`;
    })
    .join("\n");
}

/** 生成结构化 Markdown 会议纪要与对话转写。 */
export function toMarkdownNotes(lines: SubtitleLine[], starred: Set<number>): string {
  const now = new Date();
  const dateStr = now.toLocaleDateString("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  });
  const timeStr = now.toLocaleTimeString("zh-CN", { hour12: false });

  const uniqueSpeakers = Array.from(
    new Set(lines.map((l) => (l.speaker >= 0 ? l.speaker : 0))),
  ).sort((a, b) => a - b);
  const totalDuration =
    lines.length > 0
      ? `${lines[0]?.start ?? "00:00:00"} ~ ${lines.at(-1)?.end ?? lines.at(-1)?.start ?? "00:00:00"}`
      : "00:00:00";

  let md = `# Sona 会议与语音对话纪要\n\n`;
  md += `> 自动生成于：${dateStr} ${timeStr} | 引擎：SpeechRail / Apple Silicon\n\n`;

  md += `## 📋 会议概要\n\n`;
  md += `- **记录时间**：${dateStr} ${timeStr}\n`;
  md += `- **有效时间段**：\`${totalDuration}\`\n`;
  md += `- **发言人数**：${uniqueSpeakers.length} 位 (${uniqueSpeakers.map((s) => `说话人 ${s}`).join(", ")})\n`;
  md += `- **总转写条目**：${lines.length} 条\n`;
  md += `- **重点星标标记**：${starred.size} 条\n\n`;

  // 重点星标部分
  if (starred.size > 0) {
    md += `## ⭐ 重点发言与结论速览\n\n`;
    lines.forEach((line, idx) => {
      if (starred.has(idx)) {
        const spk = line.speaker >= 0 ? line.speaker : 0;
        md += `- **[${line.start}] 说话人 ${spk}**：${line.text}\n`;
        if (line.translation) {
          md += `  > 译文：${line.translation}\n`;
        }
      }
    });
    md += `\n---\n\n`;
  }

  // 完整时序转写
  md += `## 📝 完整对话时序记录\n\n`;
  let currentSpeaker: number | null = null;

  lines.forEach((line, idx) => {
    const isStarred = starred.has(idx);
    const starTag = isStarred ? " ⭐" : "";
    const spk = line.speaker >= 0 ? line.speaker : 0;

    if (spk !== currentSpeaker) {
      currentSpeaker = spk;
      md += `\n### 👤 说话人 ${spk} (\`${line.start}\`)\n\n`;
    }

    md += `- \`[${line.start} - ${line.end || line.start}]\` ${line.text}${starTag}\n`;
    if (line.translation && line.translation.trim()) {
      md += `  > 译文：${line.translation}\n`;
    }
  });

  md += `\n\n---\n*由 Sona 本地离线工作台导出*\n`;
  return md;
}

/** "0:00:03" / "0:00:03,500" → SRT "00:00:03,000"。 */
function srtTime(raw: string | undefined): string {
  if (!raw) return "";
  const normalized = raw.trim().replace(",", ".");
  const [clock = "", fraction = ""] = normalized.split(".", 2);
  const [h, m, s] = clock.split(":").map(Number);
  const millis = Number(fraction.padEnd(3, "0").slice(0, 3));
  if (Number.isNaN(h) || Number.isNaN(m) || Number.isNaN(s) || Number.isNaN(millis)) return "";
  return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}:${String(
    s,
  ).padStart(2, "0")},${String(millis).padStart(3, "0")}`;
}
