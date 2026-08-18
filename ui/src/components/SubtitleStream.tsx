import { useEffect, useRef } from "react";
import { useEventSocket } from "../hooks/useEventSocket";
import { speakerColor, toSRT, useSubtitleStore, type SubtitleLine } from "../stores/subtitleStore";
import "./SubtitleStream.css";

function downloadBlob(content: string, filename: string, mime: string) {
  const blob = new Blob([content], { type: mime });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

export default function SubtitleStream() {
  const { lines, partial, connected } = useSubtitleStore();
  const scrollRef = useRef<HTMLDivElement>(null);

  const handleMessage = (evt: MessageEvent) => {
    try {
      const payload = JSON.parse(evt.data as string);
      useSubtitleStore.getState().applySnapshot(payload);
    } catch {
      // 非 JSON 帧忽略
    }
  };

  const { state } = useEventSocket("/ws/subtitles", handleMessage);

  useEffect(() => {
    if (state === "open") useSubtitleStore.getState().setConnected(true);
    else useSubtitleStore.getState().setConnected(false);
  }, [state]);

  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [lines, partial]);

  return (
    <section className="subtitle-panel">
      <header className="subtitle-header">
        <h2>实时字幕</h2>
        <span className={`subtitle-status ${connected ? "ok" : "off"}`}>
          {connected ? "● 已连接" : "○ 未连接"}
        </span>
      </header>

      <div className="subtitle-stream" ref={scrollRef}>
        {lines.map((line, i) => (
          <SubtitleRow key={i} line={line} />
        ))}
        {partial && (
          <p className="subtitle-partial">
            <span style={{ color: speakerColor(0) }}>…</span> {partial}
          </p>
        )}
        {!lines.length && !partial && (
          <p className="subtitle-empty">等待字幕…（启动 wlk 后自动接收）</p>
        )}
      </div>

      <footer className="subtitle-toolbar">
        <button
          type="button"
          onClick={() => downloadBlob(toSRT(lines), `subtitles-${new Date().toISOString()}.srt`, "application/x-subrip")}
          disabled={!lines.length}
        >
          导出 SRT
        </button>
        <button type="button" onClick={() => useSubtitleStore.getState().clear()} disabled={!lines.length}>
          清空
        </button>
        <span className="subtitle-count">{lines.length} 行</span>
      </footer>
    </section>
  );
}

function SubtitleRow({ line }: { line: SubtitleLine }) {
  return (
    <p className="subtitle-line">
      <span className="subtitle-speaker" style={{ color: speakerColor(line.speaker) }}>
        {line.speaker >= 0 ? `👤${line.speaker}` : ""}
      </span>
      <span className="subtitle-text">{line.text}</span>
      {line.translation && <span className="subtitle-translation">{line.translation}</span>}
    </p>
  );
}