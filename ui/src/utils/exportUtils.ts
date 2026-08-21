import type {
  ExportFormat,
  MeetingDetail,
  MeetingMinutesVersion,
  TranscriptSegment,
} from "../contracts/meetingContract";

/**
 * Format milliseconds to standard SRT timestamp format: HH:MM:SS,mmm
 */
export function msToSrtTimestamp(ms: number): string {
  const safeMs = Math.max(0, Math.floor(ms));
  const hours = Math.floor(safeMs / 3600000);
  const minutes = Math.floor((safeMs % 3600000) / 60000);
  const seconds = Math.floor((safeMs % 60000) / 1000);
  const millis = safeMs % 1000;

  return (
    String(hours).padStart(2, "0") +
    ":" +
    String(minutes).padStart(2, "0") +
    ":" +
    String(seconds).padStart(2, "0") +
    "," +
    String(millis).padStart(3, "0")
  );
}

/**
 * Format milliseconds to readable time MM:SS
 */
export function msToReadableTime(ms: number): string {
  const safeMs = Math.max(0, Math.floor(ms));
  const minutes = Math.floor(safeMs / 60000);
  const seconds = Math.floor((safeMs % 60000) / 1000);
  return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
}

export function generateSrtContent(segments: readonly TranscriptSegment[]): string {
  return segments
    .map((seg, index) => {
      const startTime = msToSrtTimestamp(seg.start_ms);
      const endTime = msToSrtTimestamp(seg.end_ms);
      return `${index + 1}\n${startTime} --> ${endTime}\n[${seg.speaker_name}] ${seg.text}\n`;
    })
    .join("\n");
}

export function generatePlainTextContent(
  meeting: MeetingDetail,
  segments: readonly TranscriptSegment[],
  minutes: MeetingMinutesVersion | null,
): string {
  const lines: string[] = [];
  lines.push(`会议主题：${meeting.title}`);
  lines.push(`会议状态：${meeting.status}`);
  lines.push(`开始时间：${meeting.started_at || meeting.created_at}`);
  if (meeting.ended_at) lines.push(`结束时间：${meeting.ended_at}`);
  lines.push("\n" + "=".repeat(40) + "\n");

  if (minutes?.content_json) {
    const j = minutes.content_json;
    lines.push("【AI 会议纪要】\n");
    if (j.overview) lines.push(`概要：\n${j.overview}\n`);
    if (j.topics?.length) {
      lines.push("核心议题：");
      j.topics.forEach((t, i) => lines.push(`  ${i + 1}. ${t.title}: ${t.summary}`));
      lines.push("");
    }
    if (j.decisions?.length) {
      lines.push("决策事项：");
      j.decisions.forEach((d, i) => lines.push(`  ${i + 1}. ${d.content}`));
      lines.push("");
    }
    if (j.action_items?.length) {
      lines.push("待办行动项：");
      j.action_items.forEach((a, i) =>
        lines.push(
          `  ${i + 1}. ${a.task}${a.owner ? ` (负责人: ${a.owner})` : ""}${
            a.due_date ? ` [截止: ${a.due_date}]` : ""
          }`,
        ),
      );
      lines.push("");
    }
    lines.push("=".repeat(40) + "\n");
  }

  lines.push("【会议转录记录】\n");
  for (const seg of segments) {
    lines.push(`[${msToReadableTime(seg.start_ms)} - ${msToReadableTime(seg.end_ms)}] ${seg.speaker_name}:`);
    lines.push(`  ${seg.text}\n`);
  }

  return lines.join("\n");
}

export function generateMarkdownContent(
  meeting: MeetingDetail,
  segments: readonly TranscriptSegment[],
  minutes: MeetingMinutesVersion | null,
): string {
  if (minutes?.content_markdown) {
    return minutes.content_markdown;
  }

  const lines: string[] = [];
  lines.push(`# 会议纪要：${meeting.title}\n`);
  lines.push(`- **状态**：\`${meeting.status}\``);
  lines.push(`- **开始时间**：${meeting.started_at || meeting.created_at}`);
  if (meeting.ended_at) lines.push(`- **结束时间**：${meeting.ended_at}`);
  lines.push("\n---\n");

  if (minutes?.content_json) {
    const j = minutes.content_json;
    lines.push("## 1. 会议概要\n");
    lines.push(j.overview + "\n");

    if (j.topics?.length) {
      lines.push("## 2. 核心议题\n");
      j.topics.forEach((t, i) => {
        lines.push(`### 2.${i + 1} ${t.title}`);
        lines.push(`${t.summary}\n`);
      });
    }

    if (j.decisions?.length) {
      lines.push("## 3. 决策事项\n");
      j.decisions.forEach((d) => lines.push(`- [x] **${d.content}**`));
      lines.push("");
    }

    if (j.action_items?.length) {
      lines.push("## 4. 待办行动项\n");
      j.action_items.forEach((a) => {
        const owner = a.owner ? ` \`@${a.owner}\`` : "";
        const due = a.due_date ? ` (截止: ${a.due_date})` : "";
        lines.push(`- [ ] ${a.task}${owner}${due}`);
      });
      lines.push("");
    }

    if (j.risks?.length) {
      lines.push("## 5. 风险提示\n");
      j.risks.forEach((r) => lines.push(`- ⚠️ ${r.content}`));
      lines.push("");
    }

    if (j.open_questions?.length) {
      lines.push("## 6. 待定问题\n");
      j.open_questions.forEach((q) => lines.push(`- ❓ ${q.content}`));
      lines.push("");
    }

    if (j.highlights?.length) {
      lines.push("## 7. 精彩亮点\n");
      j.highlights.forEach((h) => lines.push(`- 🌟 ${h.content}`));
      lines.push("");
    }

    lines.push("---\n");
  }

  lines.push("## 逐字转录记录\n");
  lines.push("| 时间 | 说话人 | 转录内容 |");
  lines.push("|---|---|---|");
  for (const seg of segments) {
    const time = `${msToReadableTime(seg.start_ms)}–${msToReadableTime(seg.end_ms)}`;
    lines.push(`| \`${time}\` | **${seg.speaker_name}** | ${seg.text} |`);
  }

  return lines.join("\n");
}

export function generateJsonContent(
  meeting: MeetingDetail,
  segments: readonly TranscriptSegment[],
  minutes: MeetingMinutesVersion | null,
): string {
  return JSON.stringify(
    {
      meeting,
      minutes,
      segments,
      exported_at: new Date().toISOString(),
    },
    null,
    2,
  );
}

export function clientSideDownload(content: string, filename: string, mimeType: string): void {
  const blob = new Blob([content], { type: mimeType });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  URL.revokeObjectURL(url);
}

export function exportMeetingData(
  meeting: MeetingDetail,
  segments: readonly TranscriptSegment[],
  minutes: MeetingMinutesVersion | null,
  format: ExportFormat,
): void {
  const safeTitle = (meeting.title || "meeting").replace(/[/\\?%*:|"<>]/g, "_");

  switch (format) {
    case "srt": {
      const srt = generateSrtContent(segments);
      clientSideDownload(srt, `${safeTitle}.srt`, "text/plain;charset=utf-8");
      break;
    }
    case "txt": {
      const txt = generatePlainTextContent(meeting, segments, minutes);
      clientSideDownload(txt, `${safeTitle}.txt`, "text/plain;charset=utf-8");
      break;
    }
    case "json": {
      const json = generateJsonContent(meeting, segments, minutes);
      clientSideDownload(json, `${safeTitle}.json`, "application/json;charset=utf-8");
      break;
    }
    case "md":
    default: {
      const md = generateMarkdownContent(meeting, segments, minutes);
      clientSideDownload(md, `${safeTitle}.md`, "text/markdown;charset=utf-8");
      break;
    }
  }
}
