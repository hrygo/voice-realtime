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
import { ReconnectingSocket, type ConnectionState } from "./useEventSocket";

interface PendingRequest {
  readonly resolve: (state: RuntimeStateSnapshot) => void;
  readonly reject: (reason: Error) => void;
  readonly timer: ReturnType<typeof setTimeout>;
}

interface CommandChannelOptions {
  readonly applyState: (state: RuntimeStateSnapshot) => void;
  readonly onReady?: (ready: boolean) => void;
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

/** 控制面的协议状态机：握手、request_id 关联、超时与服务端权威状态。 */
export class CommandChannel {
  private socket: WebSocket | null = null;
  private ready = false;
  private readonly pending = new Map<string, PendingRequest>();

  constructor(private readonly options: CommandChannelOptions) {}

  attach(socket: WebSocket): void {
    this.rejectPending("控制连接已重建", "service_unavailable");
    this.socket = socket;
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

    if (value.event === "state" && isRuntimeState(value.state)) {
      this.options.applyState(value.state);
      this.setReady(true);
      return;
    }

    if (
      typeof value.request_id !== "string"
      || typeof value.cmd !== "string"
      || typeof value.ok !== "boolean"
      || !isRuntimeState(value.state)
    ) {
      return;
    }
    const response = value as unknown as CommandResponse;
    this.options.applyState(response.state);
    const request = this.pending.get(response.request_id);
    if (!request) return;
    clearTimeout(request.timer);
    this.pending.delete(response.request_id);
    if (response.ok) {
      request.resolve(response.state);
    } else {
      request.reject(new CommandError(
        response.message || "控制指令执行失败",
        response.error_code || "command_failed",
      ));
    }
  }

  send(command: ControlCommand): Promise<RuntimeStateSnapshot> {
    const socket = this.socket;
    if (!this.ready || !socket || socket.readyState !== WebSocket.OPEN) {
      return Promise.reject(new CommandError("控制端尚未完成状态同步", "service_unavailable"));
    }

    const requestId = makeRequestId();
    return new Promise<RuntimeStateSnapshot>((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(requestId);
        reject(new CommandError("控制指令确认超时", "timeout"));
      }, this.options.timeoutMs ?? 8000);
      this.pending.set(requestId, { resolve, reject, timer });
      try {
        socket.send(JSON.stringify({ request_id: requestId, ...command }));
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
  readonly sendCommand: (command: ControlCommand) => Promise<RuntimeStateSnapshot>;
}

export function useCommandSocket(url = "/ws/assistant/cmd"): CommandSocketApi {
  const [state, setState] = useState<ConnectionState>("connecting");
  const [ready, setReady] = useState(false);
  const channelRef = useRef<CommandChannel | null>(null);
  if (channelRef.current === null) {
    channelRef.current = new CommandChannel({
      applyState: (snapshot) => {
        useUISettingsStore.getState().applyRuntimeState(snapshot);
        useAssistantStore.getState().syncPipelineState(snapshot.pipeline);
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

  const sendCommand = useCallback((command: ControlCommand) => {
    const channel = channelRef.current;
    return channel
      ? channel.send(command)
      : Promise.reject(new CommandError("控制端尚未初始化", "service_unavailable"));
  }, []);

  return { state, ready: ready && state === "open", sendCommand };
}
