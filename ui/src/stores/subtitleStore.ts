import { create } from "zustand";

/** 对照 wlk FrontData.to_dict() 的字段子集（结构等价官方 UI 渲染所需）。 */
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
  applySnapshot: (snap: Partial<SubtitleSnapshot>) => void;
  setConnected: (v: boolean) => void;
  clear: () => void;
}

export const useSubtitleStore = create<SubtitleState>((set) => ({
  lines: [],
  partial: "",
  connected: false,
  applySnapshot: (snap) =>
    set((s) => ({
      lines: snap.lines ?? s.lines,
      partial: snap.buffer_transcription ?? s.partial,
    })),
  setConnected: (v) => set({ connected: v }),
  clear: () => set({ lines: [], partial: "" }),
}));

/** 说话人配色：对齐 wlk 官方 UI（按 speaker 取色，超过 8 轮换）。 */
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
  return palette[Math.abs(speaker) % palette.length];
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

/** "0:00:03" / "0:00:03,500" → SRT "00:00:03,000"。 */
function srtTime(raw: string | undefined): string {
  if (!raw) return "";
  const [h, m, s] = raw.replace(/,/g, ".").split(".")[0].split(":").map(Number);
  const millis = Math.round((Number(raw.split(".")[1] || "0") / 1) * 1000) || 0;
  if (Number.isNaN(h) || Number.isNaN(m) || Number.isNaN(s)) return "";
  return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}:${String(
    s,
  ).padStart(2, "0")},${String(millis).padStart(3, "0")}`;
}