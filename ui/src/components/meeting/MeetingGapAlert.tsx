import type { TranscriptionGap } from "../../stores/meetingStore";

export function formatTimeRange(startMs: number, endMs: number): string {
  const format = (ms: number) => {
    const totalSec = Math.floor(ms / 1000);
    const m = Math.floor(totalSec / 60);
    const s = totalSec % 60;
    return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  };
  return `[${format(startMs)} – ${format(endMs)}]`;
}

interface MeetingGapAlertProps {
  gaps: readonly TranscriptionGap[];
}

export function MeetingGapAlert({ gaps }: MeetingGapAlertProps) {
  if (!gaps || gaps.length === 0) return null;

  return (
    <div
      className="gap-alert-container"
      style={{
        padding: "8px 14px",
        background: "rgba(245, 158, 11, 0.15)",
        borderBottom: "1px solid var(--color-yellow)",
        color: "var(--color-yellow)",
        fontSize: "0.78rem",
        display: "flex",
        flexDirection: "column",
        gap: "4px",
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: "6px", fontWeight: 700 }}>
        <span>⚠️</span>
        <span>转录区间存在网络/服务中断缺口（以下区间未录入）：</span>
      </div>
      <div style={{ display: "flex", flexWrap: "wrap", gap: "8px", paddingLeft: "20px" }}>
        {gaps.map((g, idx) => (
          <span key={idx} style={{ fontFamily: "var(--font-mono)", fontSize: "0.72rem" }}>
            {formatTimeRange(g.start_ms, g.end_ms)} {g.reason ? `(${g.reason})` : ""}
          </span>
        ))}
      </div>
    </div>
  );
}
