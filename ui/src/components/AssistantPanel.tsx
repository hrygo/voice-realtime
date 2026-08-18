import { useCallback, useEffect, useRef, useState } from "react";
import { useEventSocket } from "../hooks/useEventSocket";
import type { AssistantPhase } from "../stores/assistantStore";
import {
	parseAssistantEvent,
	selectAssistantConnected,
	selectAssistantPhase,
	selectAssistantTranscript,
	useAssistantStore,
} from "../stores/assistantStore";
import "./AssistantPanel.css";

type Command = "clear_context" | "stop_session";
type CommandState = "waiting" | "ready" | "sent";

const PHASE_LABELS: Record<AssistantPhase, string> = {
	idle: "待命",
	listening: "聆听",
	thinking: "思考",
	speaking: "播报",
};

const ACTIVE_PHASES: readonly AssistantPhase[] = [
	"listening",
	"thinking",
	"speaking",
];

export default function AssistantPanel() {
	const phase = useAssistantStore(selectAssistantPhase);
	const transcript = useAssistantStore(selectAssistantTranscript);
	const connected = useAssistantStore(selectAssistantConnected);
	const clearTranscript = useAssistantStore((state) => state.clearTranscript);
	const latestBubble = transcript.at(-1);
	const transcriptRef = useRef<HTMLDivElement>(null);
	const commandSocketRef = useRef<WebSocket | null>(null);
	const [commandState, setCommandState] = useState<CommandState>("waiting");

	const handleMessage = useCallback((message: MessageEvent) => {
		if (typeof message.data !== "string") return;

		try {
			const event = parseAssistantEvent(JSON.parse(message.data));
			if (event) useAssistantStore.getState().applyEvent(event);
		} catch {
			// 不可信或非 JSON 的 WS 帧不应影响面板渲染。
		}
	}, []);

	const { state: socketState } = useEventSocket("/ws/assistant", handleMessage);

	useEffect(() => {
		useAssistantStore.getState().setConnected(socketState === "open");
	}, [socketState]);

	useEffect(() => {
		const transcriptElement = transcriptRef.current;
		if (transcriptElement && latestBubble)
			transcriptElement.scrollTop = transcriptElement.scrollHeight;
	}, [latestBubble]);

	useEffect(() => {
		let commandSocket: WebSocket | null = null;
		try {
			commandSocket = new WebSocket("/ws/assistant/cmd");
			commandSocketRef.current = commandSocket;
			commandSocket.onopen = () => setCommandState("ready");
			commandSocket.onclose = () => setCommandState("waiting");
			commandSocket.onerror = () => setCommandState("waiting");
		} catch (error) {
			// 控制端尚未实现时保持可用界面，不让连接失败打断语音事件流。
			console.warn("语音助手控制端接入中", error);
		}

		return () => {
			commandSocket?.close();
			commandSocketRef.current = null;
		};
	}, []);

	const sendCommand = useCallback((command: Command) => {
		const commandSocket = commandSocketRef.current;
		if (!commandSocket || commandSocket.readyState !== WebSocket.OPEN) {
			console.warn("语音助手控制端接入中", command);
			setCommandState("waiting");
			return;
		}

		try {
			commandSocket.send(JSON.stringify({ cmd: command }));
			setCommandState("sent");
		} catch (error) {
			// 发送与关闭并发时静默降级，下一次点击会重新检测连接状态。
			console.warn("语音助手控制指令发送失败", error);
			setCommandState("waiting");
		}
	}, []);

	return (
		<section className="panel assistant-panel" aria-label="语音助手">
			<header className="panel-header assistant-header">
				<div>
					<h2>语音助手</h2>
					<p className="assistant-connection">
						{connected ? "状态桥已连接" : "状态桥未连接"}
					</p>
				</div>
				<span className={`assistant-phase-label phase-${phase}`}>
					当前：{PHASE_LABELS[phase]}
				</span>
			</header>

			<div
				className="assistant-status"
				role="status"
				aria-label={`当前处于${PHASE_LABELS[phase]}状态`}
			>
				{ACTIVE_PHASES.map((item) => (
					<span
						className={`assistant-light ${phase === item ? "active" : ""}`}
						key={item}
					>
						<span className="assistant-light-dot" aria-hidden="true" />
						{PHASE_LABELS[item]}
					</span>
				))}
			</div>

			<div className="assistant-waveform">
				<AssistantWaveform phase={phase} />
			</div>

			<div
				className="assistant-transcript"
				ref={transcriptRef}
				aria-live="polite"
			>
				{transcript.map((bubble) => (
					<p
						className={`assistant-bubble ${bubble.role} ${bubble.final ? "final" : "streaming"}`}
						key={`${bubble.role}-${bubble.turnId ?? bubble.text}`}
					>
						<span className="assistant-bubble-role">
							{bubble.role === "user" ? "你" : "助手"}
						</span>
						<span>{bubble.text}</span>
					</p>
				))}
				{!transcript.length && <p className="assistant-empty">等待语音事件…</p>}
			</div>

			<footer className="assistant-controls">
				<button type="button" onClick={() => sendCommand("stop_session")}>
					停止会话
				</button>
				<button type="button" onClick={() => sendCommand("clear_context")}>
					清空上下文
				</button>
				<button
					type="button"
					onClick={clearTranscript}
					disabled={!transcript.length}
				>
					清空记录
				</button>
				<span className="assistant-command-status">
					{commandStatusLabel(commandState)}
				</span>
			</footer>
		</section>
	);
}

function AssistantWaveform({ phase }: { readonly phase: AssistantPhase }) {
	const canvasRef = useRef<HTMLCanvasElement>(null);
	const phaseRef = useRef(phase);
	phaseRef.current = phase;

	useEffect(() => {
		const canvas = canvasRef.current;
		const context = canvas?.getContext("2d");
		if (!canvas || !context) return;

		const barCount = 18;
		const levels = Array.from({ length: barCount }, () => 1);
		const targets = Array.from({ length: barCount }, () => 1);
		let frame = 0;
		let animationFrame = 0;

		const resize = () => {
			const bounds = canvas.getBoundingClientRect();
			const pixelRatio = window.devicePixelRatio || 1;
			canvas.width = Math.max(1, Math.round(bounds.width * pixelRatio));
			canvas.height = Math.max(1, Math.round(bounds.height * pixelRatio));
			context.setTransform(pixelRatio, 0, 0, pixelRatio, 0, 0);
		};

		const render = () => {
			const bounds = canvas.getBoundingClientRect();
			const accent = getComputedStyle(canvas)
				.getPropertyValue("--accent")
				.trim();
			frame += 1;
			if (frame % 8 === 0) {
				for (let index = 0; index < barCount; index += 1) {
					targets[index] = targetLevel(phaseRef.current, index, frame);
				}
			}

			context.clearRect(0, 0, bounds.width, bounds.height);
			context.fillStyle = accent;
			for (let index = 0; index < barCount; index += 1) {
				const level = levels[index];
				const target = targets[index];
				if (level === undefined || target === undefined) continue;
				const nextLevel = level + (target - level) * 0.16;
				levels[index] = nextLevel;
				const gap = bounds.width / barCount;
				const barWidth = Math.max(2, gap * 0.46);
				const barHeight = Math.max(2, nextLevel);
				context.fillRect(
					index * gap + (gap - barWidth) / 2,
					(bounds.height - barHeight) / 2,
					barWidth,
					barHeight,
				);
			}
			animationFrame = requestAnimationFrame(render);
		};

		const observer = new ResizeObserver(resize);
		observer.observe(canvas);
		resize();
		render();
		return () => {
			observer.disconnect();
			cancelAnimationFrame(animationFrame);
		};
	}, []);

	return (
		<canvas
			className="assistant-waveform-canvas"
			ref={canvasRef}
			role="img"
			aria-label="语音活动波形"
		/>
	);
}

function targetLevel(
	phase: AssistantPhase,
	index: number,
	frame: number,
): number {
	switch (phase) {
		case "idle":
			return 2;
		case "thinking":
			return 5 + Math.abs(Math.sin((frame + index * 3) / 14)) * 5;
		case "listening":
		case "speaking":
			return 10 + Math.random() * 28;
	}
}

function commandStatusLabel(state: CommandState): string {
	switch (state) {
		case "ready":
			return "控制端已连接";
		case "sent":
			return "指令已发送";
		case "waiting":
			return "控制端接入中";
	}
}
