import { useCallback, useEffect, useRef, useState } from "react";

import {
  isRecord,
  isRuntimeState,
  type CommandResponse,
  type ControlCommand,
  type RuntimeStateSnapshot,
} from "../protocol";
import { useUISettingsStore } from "../stores/uiSettingsStore";
import { useAssistantStore } from "../stores/assistantStore";
import { useMeetingStore } from "../stores/meetingStore";
import { audioEnergyService } from "../services/audioEnergyService";
import { apiUrl } from "../config/runtimeConfig";
import { runtimeConfig } from "../config/runtimeConfig";
import { ReconnectingSocket, type ConnectionState } from "./useEventSocket";

interface PendingRequest {
  readonly resolve: (state: RuntimeStateSnapshot) => void;
  readonly reject: (reason: Error) => void;
  readonly timer: ReturnType<typeof setTimeout>;
}

interface CommandChannelOptions {
  readonly applyState: (state: RuntimeStateSnapshot) => void;
  readonly onReady?: (ready: boolean) => void;
  readonly onProtocolError?: (error: CommandError) => void;
  readonly timeoutMs?: number;
}

export class CommandError extends Error {
  constructor(
    message: string,
    readonly code: string,
  ) {
    super(message);
    this.name = "CommandError";
  }
}

export const OWNERSHIP_KEYS = [
  "mode",
  "pcm_owner",
  "active_meeting_id",
  "meeting_state",
  "meeting_started_at",
  "runtime_revision",
] as const satisfies readonly (keyof RuntimeStateSnapshot)[];

function hasSameOwnership(
  current: RuntimeStateSnapshot,
  incoming: RuntimeStateSnapshot,
): boolean {
  return OWNERSHIP_KEYS.every((key) => current[key] === incoming[key]);
}

export function mergeRuntimeState(
  current: RuntimeStateSnapshot | null,
  incoming: RuntimeStateSnapshot,
): RuntimeStateSnapshot {
  if (!current || incoming.runtime_revision > current.runtime_revision) return incoming;
  if (incoming.runtime_revision < current.runtime_revision) return current;
  if (
    incoming.runtime_revision === current.runtime_revision
    && hasSameOwnership(current, incoming)
  ) {
    return incoming;
  }
  return {
    ...incoming,
    mode: current.mode,
    pcm_owner: current.pcm_owner,
    active_meeting_id: current.active_meeting_id,
    meeting_state: current.meeting_state,
    meeting_started_at: current.meeting_started_at,
    runtime_revision: current.runtime_revision,
  };
}

/** 控制面的协议状态机：握手、request_id 关联、超时与服务端权威状态。 */
export class CommandChannel {
  private socket: WebSocket | null = null;
  private ready = false;
  private readonly pending = new Map<string, PendingRequest>();
  private currentState: RuntimeStateSnapshot | null = null;
  private currentHighestRuntimeRevision: number | null = null;
  private reconcileRequest: Promise<RuntimeStateSnapshot> | null = null;

  constructor(private readonly options: CommandChannelOptions) {}

  get latestState(): RuntimeStateSnapshot | null {
    return this.currentState;
  }

  get highestRuntimeRevision(): number | null {
    return this.currentHighestRuntimeRevision;
  }

  get reconciling(): boolean {
    return this.reconcileRequest !== null;
  }

  attach(socket: WebSocket): void {
    this.rejectPending("控制连接已重建", "service_unavailable");
    this.socket = socket;
    this.currentState = null;
    this.currentHighestRuntimeRevision = null;
    this.setReady(false);
  }

  detach(): void {
    this.socket = null;
    this.setReady(false);
    this.rejectPending("控制连接已断开", "service_unavailable");
  }

  dispose(): void {
    this.socket = null;
    this.ready = false;
    this.rejectPending("控制连接已关闭", "service_unavailable");
  }

  receive(raw: string): void {
    let value: unknown;
    try {
      value = JSON.parse(raw);
    } catch {
      return;
    }
    if (!isRecord(value)) return;

    if ((value.event === "state" || value.event === "runtime_state") && isRuntimeState(value.state)) {
      this.receiveState(value.state);
      this.setReady(true);
      return;
    }

    if (typeof value.request_id !== "string" || typeof value.ok !== "boolean") {
      return;
    }
    const response = value as unknown as CommandResponse;
    let mergedState: RuntimeStateSnapshot | null = null;
    if (isRuntimeState(response.state)) {
      mergedState = this.receiveState(response.state);
    }
    const request = this.pending.get(response.request_id);
    if (!request) return;
    clearTimeout(request.timer);
    this.pending.delete(response.request_id);
    if (response.ok) {
      if (mergedState) {
        request.resolve(mergedState);
      } else {
        request.reject(new CommandError("服务端状态快照格式无效", "invalid_response"));
      }
    } else {
      const errObj = response.error;
      const errMsg = errObj?.message || response.message || "控制指令执行失败";
      const errCode = errObj?.code || response.error_code || "command_failed";
      request.reject(new CommandError(errMsg, errCode));
    }
  }

  receiveState(incoming: RuntimeStateSnapshot): RuntimeStateSnapshot {
    const current = this.currentState;
    if (
      current
      && incoming.runtime_revision === current.runtime_revision
      && !hasSameOwnership(current, incoming)
    ) {
      this.options.onProtocolError?.(
        new CommandError("相同 runtime_revision 的所有权字段不一致", "protocol_error"),
      );
    }
    const merged = mergeRuntimeState(current, incoming);
    if (merged === current) return current;
    this.currentState = merged;
    this.currentHighestRuntimeRevision = merged.runtime_revision;
    this.options.applyState(merged);
    return merged;
  }

  reconcileRuntime(): Promise<RuntimeStateSnapshot> {
    if (this.reconcileRequest) return this.reconcileRequest;
    const request = this.fetchRuntimeState();
    this.reconcileRequest = request.finally(() => {
      this.reconcileRequest = null;
    });
    return this.reconcileRequest;
  }

  send(command: ControlCommand, timeoutMs?: number): Promise<RuntimeStateSnapshot> {
    const socket = this.socket;
    if (!this.ready || !socket || socket.readyState !== WebSocket.OPEN) {
      return Promise.reject(new CommandError("控制端尚未完成状态同步", "service_unavailable"));
    }

    const requestId = makeRequestId();
    // 会议冲刷/结束涉及 WhisperLiveKit 声纹对账与数据库事务，给予充足的 30s 超时时间
    const isMeetingOrModeCmd =
      command.cmd === "end_meeting" ||
      command.cmd === "stop_active_mode" ||
      command.cmd === "start_meeting";
    const effectiveTimeoutMs =
      timeoutMs ??
      (this.options.timeoutMs !== undefined
        ? this.options.timeoutMs
        : isMeetingOrModeCmd
          ? 30000
          : 10000);

    return new Promise<RuntimeStateSnapshot>((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(requestId);
        reject(new CommandError("控制指令确认超时", "timeout"));
      }, effectiveTimeoutMs);
      this.pending.set(requestId, { resolve, reject, timer });
      try {
        const payload: Record<string, unknown> = {
          request_id: requestId,
          ...command,
        };
        if (
          "contract_version" in command ||
          command.cmd === "start_meeting" ||
          command.cmd === "end_meeting" ||
          command.cmd === "start_assistant" ||
          command.cmd === "start_subtitles" ||
          command.cmd === "stop_active_mode"
        ) {
          payload.contract_version = "1";
        }
        socket.send(JSON.stringify(payload));
      } catch {
        clearTimeout(timer);
        this.pending.delete(requestId);
        reject(new CommandError("控制指令发送失败", "service_unavailable"));
      }
    });
  }

  private setReady(value: boolean): void {
    if (this.ready === value) return;
    this.ready = value;
    this.options.onReady?.(value);
  }

  private async fetchRuntimeState(): Promise<RuntimeStateSnapshot> {
    let response: Response;
    try {
      response = await fetch(apiUrl("/api/runtime"));
    } catch {
      throw new CommandError("运行时状态对账失败", "service_unavailable");
    }
    if (!response.ok) {
      throw new CommandError("运行时状态对账失败", "service_unavailable");
    }

    let value: unknown;
    try {
      value = await response.json();
    } catch {
      throw new CommandError("服务端状态快照格式无效", "invalid_response");
    }
    if (!isRuntimeState(value)) {
      throw new CommandError("服务端状态快照格式无效", "invalid_response");
    }
    return this.receiveState(value);
  }

  private rejectPending(message: string, code: string): void {
    for (const request of this.pending.values()) {
      clearTimeout(request.timer);
      request.reject(new CommandError(message, code));
    }
    this.pending.clear();
  }
}

function makeRequestId(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}

export interface CommandSocketApi {
  readonly state: ConnectionState;
  readonly ready: boolean;
  readonly snapshot: RuntimeStateSnapshot | null;
  readonly highestRuntimeRevision: number | null;
  readonly sendCommand: (command: ControlCommand, timeoutMs?: number) => Promise<RuntimeStateSnapshot>;
  readonly reconcileRuntime: () => Promise<RuntimeStateSnapshot>;
}

export function useCommandSocket(url = runtimeConfig.controlWsUrl): CommandSocketApi {
  const [state, setState] = useState<ConnectionState>("connecting");
  const [ready, setReady] = useState(false);
  const [snapshot, setSnapshot] = useState<RuntimeStateSnapshot | null>(null);
  const [highestRuntimeRevision, setHighestRuntimeRevision] = useState<number | null>(null);
  const channelRef = useRef<CommandChannel | null>(null);
  if (channelRef.current === null) {
    channelRef.current = new CommandChannel({
      applyState: (snapshot) => {
        audioEnergyService.updateFromRuntimeState(snapshot);
        setSnapshot(snapshot);
        setHighestRuntimeRevision(snapshot.runtime_revision);
        useUISettingsStore.getState().applyRuntimeState(snapshot);
        useAssistantStore.getState().syncPipelineState(snapshot.pipeline);
        if (snapshot.active_meeting_id) {
          const meetingStore = useMeetingStore.getState();
          if (meetingStore.activeMeetingId !== snapshot.active_meeting_id) {
            useMeetingStore.setState({ activeMeetingId: snapshot.active_meeting_id });
          }
        }
        if (snapshot.meeting_state) {
          useMeetingStore.getState().updateMeetingState(
            snapshot.meeting_state,
            snapshot.meeting_started_at,
            null,
            null,
            snapshot.active_meeting_id,
          );
        }
      },
      onReady: setReady,
    });
  }

  useEffect(() => {
    const channel = channelRef.current;
    if (!channel) return;
    const socket = new ReconnectingSocket(url, {
      onState: setState,
      onOpen: (webSocket) => channel.attach(webSocket),
      onMessage: (event) => {
        if (typeof event.data === "string") channel.receive(event.data);
      },
      onDisconnect: () => channel.detach(),
    });
    socket.start();
    return () => {
      socket.stop();
      channel.dispose();
    };
  }, [url]);

  const sendCommand = useCallback((command: ControlCommand, timeoutMs?: number) => {
    const channel = channelRef.current;
    return channel
      ? channel.send(command, timeoutMs)
      : Promise.reject(new CommandError("控制端尚未初始化", "service_unavailable"));
  }, []);

  const reconcileRuntime = useCallback(() => {
    const channel = channelRef.current;
    return channel
      ? channel.reconcileRuntime()
      : Promise.reject(new CommandError("控制端尚未初始化", "service_unavailable"));
  }, []);

  return {
    state,
    ready: ready && state === "open",
    snapshot,
    highestRuntimeRevision,
    sendCommand,
    reconcileRuntime,
  };
}
