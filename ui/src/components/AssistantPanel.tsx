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
import { useUISettingsStore } from "../stores/uiSettingsStore";
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

/** 音色请求失败时的回退列表。 */
const FALLBACK_VOICES: readonly string[] = ["default", "warm", "bright", "calm"];

export default function AssistantPanel() {
	const phase = useAssistantStore(selectAssistantPhase);
	const transcript = useAssistantStore(selectAssistantTranscript);
	const connected = useAssistantStore(selectAssistantConnected);
	const clearTranscript = useAssistantStore((state) => state.clearTranscript);
	const latestBubble = transcript.at(-1);
	const transcriptRef = useRef<HTMLDivElement>(null);
	const commandSocketRef = useRef<WebSocket | null>(null);
	const [commandState, setCommandState] = useState<CommandState>("waiting");

	/* ---- 人格编辑器 ---- */
	const [personaOpen, setPersonaOpen] = useState(false);
	const persona = useUISettingsStore((s) => s.persona);
	const setPersona = useUISettingsStore((s) => s.setPersona);
	const [personaDraft, setPersonaDraft] = useState(persona);
	const [personaError, setPersonaError] = useState("");

	/* ---- 音色选择 ---- */
	const voice = useUISettingsStore((s) => s.voice);
	const setVoice = useUISettingsStore((s) => s.setVoice);
	const [availableVoices, setAvailableVoices] = useState<readonly string[]>(FALLBACK_VOICES);
	const [voiceFeedback, setVoiceFeedback] = useState("");

	/** 通用命令发送：接受任意 payload，发送到 /ws/assistant/cmd。 */
	const sendCommandWith = useCallback(
		(payload: Record<string, unknown>) => {
			const commandSocket = commandSocketRef.current;
			if (!commandSocket || commandSocket.readyState !== WebSocket.OPEN) {
				console.warn("语音助手控制端接入中", payload);
				setCommandState("waiting");
				return;
			}

			try {
				commandSocket.send(JSON.stringify(payload));
				setCommandState("sent");
			} catch (error) {
				console.warn("语音助手控制指令发送失败", error);
				setCommandState("waiting");
			}
		},
		[],
	);

	const sendCommand = useCallback((command: Command) => {
		sendCommandWith({ cmd: command });
	}, [sendCommandWith]);

	/** 打开人格编辑器时同步草稿。 */
	const openPersona = useCallback(() => {
		setPersonaDraft(persona);
		setPersonaError("");
		setPersonaOpen(true);
	}, [persona]);

	/** 保存人格：非空校验，发送 set_persona 命令，持久化。 */
	const savePersona = useCallback(() => {
		const trimmed = personaDraft.trim();
		if (!trimmed) {
			setPersonaError("人格提示不能为空");
			return;
		}
		setPersona(trimmed);
		sendCommandWith({ cmd: "set_persona", prompt: trimmed });
		setPersonaOpen(false);
	}, [personaDraft, setPersona, sendCommandWith]);

	/** 取消人格编辑。 */
	const cancelPersona = useCallback(() => {
		setPersonaOpen(false);
		setPersonaError("");
	}, []);

	/** 音色切换。 */
	const handleVoiceChange = useCallback(
		(value: string) => {
			const previous = voice;
			setVoice(value);
			setVoiceFeedback("");
			const socket = commandSocketRef.current;
			if (!socket || socket.readyState !== WebSocket.OPEN) {
				console.warn("语音助手控制端接入中，音色切换暂未发送");
				return;
			}
			try {
				socket.send(JSON.stringify({ cmd: "set_voice", voice: value }));
				// 假设后端无响应，直接标记成功。
			} catch (error) {
				console.warn("音色切换指令发送失败", error);
				setVoice(previous);
				setVoiceFeedback("音色切换失败，已恢复原值");
			}
		},
		[voice, setVoice],
	);

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
			console.warn("语音助手控制端接入中", error);
		}

		return () => {
			commandSocket?.close();
			commandSocketRef.current = null;
		};
	}, []);

	/** 获取可用音色列表。 */
	useEffect(() => {
		let cancelled = false;
		fetch("/v1/voices")
			.then((res) => {
				if (!res.ok) throw new Error(`HTTP ${res.status}`);
				return res.json() as Promise<{ voice: string; available: string[] }>;
			})
			.then((data) => {
				if (cancelled) return;
				if (data.available && data.available.length > 0) {
					setAvailableVoices(data.available);
				}
			})
			.catch(() => {
				if (!cancelled) console.warn("音色列表获取失败，使用回退列表");
			});
		return () => {
			cancelled = true;
		};
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
				<button type="button" onClick={openPersona}>
					人格编辑
				</button>
				<div className="assistant-voice-select">
					<label htmlFor="assistant-voice">音色</label>
					<select
						id="assistant-voice"
						value={voice}
						onChange={(e) => handleVoiceChange(e.target.value)}
					>
						{availableVoices.map((v) => (
							<option key={v} value={v}>
								{v}
							</option>
						))}
					</select>
					{voiceFeedback && (
						<span className="assistant-voice-feedback">{voiceFeedback}</span>
					)}
				</div>
				<span className="assistant-command-status">
					{commandStatusLabel(commandState)}
				</span>
			</footer>

			{/* 人格编辑器 dialog */}
			{personaOpen && (
				<div
					className="persona-overlay"
					role="button"
					tabIndex={0}
					onClick={cancelPersona}
					onKeyDown={(e) => {
						if (e.key === "Escape") cancelPersona();
					}}
				>
					<dialog
						className="persona-dialog"
						open
						onClick={(e) => e.stopPropagation()}
						onKeyDown={(e) => e.stopPropagation()}
						aria-label="人格编辑器"
					>
						<div className="persona-dialog-header">
							<h3>人格编辑</h3>
							<button
								type="button"
								className="persona-dialog-close"
								onClick={cancelPersona}
								aria-label="关闭"
							>
								✕
							</button>
						</div>
						<div className="persona-dialog-body">
							<textarea
								className="persona-textarea"
								value={personaDraft}
								onChange={(e) => {
									setPersonaDraft(e.target.value);
									if (personaError) setPersonaError("");
								}}
								placeholder="输入系统提示词…（留空则使用默认人格）"
								rows={10}
								aria-label="系统提示词"
								aria-invalid={!!personaError}
							/>
							{personaError && (
								<p className="persona-error" role="alert">
									{personaError}
								</p>
							)}
						</div>
						<div className="persona-dialog-footer">
							<button type="button" onClick={savePersona}>
								保存
							</button>
							<button type="button" onClick={cancelPersona}>
								取消
							</button>
						</div>
					</dialog>
				</div>
			)}
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