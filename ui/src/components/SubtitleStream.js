import { jsx as _jsx, jsxs as _jsxs } from "react/jsx-runtime";
import { useEffect, useRef } from "react";
import { useEventSocket } from "../hooks/useEventSocket";
import { speakerColor, toSRT, useSubtitleStore } from "../stores/subtitleStore";
import "./SubtitleStream.css";
function downloadBlob(content, filename, mime) {
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
    const scrollRef = useRef(null);
    const handleMessage = (evt) => {
        try {
            const payload = JSON.parse(evt.data);
            useSubtitleStore.getState().applySnapshot(payload);
        }
        catch {
            // 非 JSON 帧忽略
        }
    };
    const { state } = useEventSocket("/ws/subtitles", handleMessage);
    useEffect(() => {
        if (state === "open")
            useSubtitleStore.getState().setConnected(true);
        else
            useSubtitleStore.getState().setConnected(false);
    }, [state]);
    useEffect(() => {
        const el = scrollRef.current;
        if (el)
            el.scrollTop = el.scrollHeight;
    }, [lines, partial]);
    return (_jsxs("section", { className: "subtitle-panel", children: [_jsxs("header", { className: "subtitle-header", children: [_jsx("h2", { children: "\u5B9E\u65F6\u5B57\u5E55" }), _jsx("span", { className: `subtitle-status ${connected ? "ok" : "off"}`, children: connected ? "● 已连接" : "○ 未连接" })] }), _jsxs("div", { className: "subtitle-stream", ref: scrollRef, children: [lines.map((line, i) => (_jsx(SubtitleRow, { line: line }, i))), partial && (_jsxs("p", { className: "subtitle-partial", children: [_jsx("span", { style: { color: speakerColor(0) }, children: "\u2026" }), " ", partial] })), !lines.length && !partial && (_jsx("p", { className: "subtitle-empty", children: "\u7B49\u5F85\u5B57\u5E55\u2026\uFF08\u542F\u52A8 wlk \u540E\u81EA\u52A8\u63A5\u6536\uFF09" }))] }), _jsxs("footer", { className: "subtitle-toolbar", children: [_jsx("button", { type: "button", onClick: () => downloadBlob(toSRT(lines), `subtitles-${new Date().toISOString()}.srt`, "application/x-subrip"), disabled: !lines.length, children: "\u5BFC\u51FA SRT" }), _jsx("button", { type: "button", onClick: () => useSubtitleStore.getState().clear(), disabled: !lines.length, children: "\u6E05\u7A7A" }), _jsxs("span", { className: "subtitle-count", children: [lines.length, " \u884C"] })] })] }));
}
function SubtitleRow({ line }) {
    return (_jsxs("p", { className: "subtitle-line", children: [_jsx("span", { className: "subtitle-speaker", style: { color: speakerColor(line.speaker) }, children: line.speaker >= 0 ? `👤${line.speaker}` : "" }), _jsx("span", { className: "subtitle-text", children: line.text }), line.translation && _jsx("span", { className: "subtitle-translation", children: line.translation })] }));
}
