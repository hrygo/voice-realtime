import { useEffect, useRef, useState } from "react";

export type ConnectionState = "connecting" | "open" | "closed";

interface ReconnectingSocketOptions {
  readonly createSocket?: (url: string) => WebSocket;
  readonly onOpen?: (socket: WebSocket) => void;
  readonly onMessage?: (event: MessageEvent) => void;
  readonly onState?: (state: ConnectionState) => void;
  readonly onDisconnect?: () => void;
  readonly baseDelayMs?: number;
  readonly maxDelayMs?: number;
}

function browserSocket(url: string): WebSocket {
  if (/^wss?:\/\//u.test(url)) return new WebSocket(url);
  const resolved = new URL(url, window.location.href);
  resolved.protocol = resolved.protocol === "https:" ? "wss:" : "ws:";
  return new WebSocket(resolved.toString());
}

/** 可取消、单定时器的指数退避 WebSocket，供事件面与控制面复用。 */
export class ReconnectingSocket {
  private socket: WebSocket | null = null;
  private retryTimer: ReturnType<typeof setTimeout> | null = null;
  private retryCount = 0;
  private disposed = false;
  private started = false;

  constructor(
    private readonly url: string,
    private readonly options: ReconnectingSocketOptions = {},
  ) {}

  start(): void {
    if (this.started || this.disposed) return;
    this.started = true;
    this.connect();
  }

  stop(): void {
    if (this.disposed) return;
    this.disposed = true;
    this.clearRetry();
    const socket = this.socket;
    this.socket = null;
    if (socket) {
      socket.onopen = null;
      socket.onmessage = null;
      socket.onerror = null;
      socket.onclose = null;
      socket.close();
    }
  }

  private connect(): void {
    if (this.disposed) return;
    this.clearRetry();
    this.options.onState?.("connecting");
    let socket: WebSocket;
    try {
      socket = (this.options.createSocket ?? browserSocket)(this.url);
    } catch {
      this.scheduleReconnect();
      return;
    }
    this.socket = socket;
    socket.onopen = () => {
      if (this.disposed || this.socket !== socket) return;
      this.retryCount = 0;
      this.options.onState?.("open");
      this.options.onOpen?.(socket);
    };
    socket.onmessage = (event) => {
      if (!this.disposed && this.socket === socket) this.options.onMessage?.(event);
    };
    socket.onerror = () => socket.close();
    socket.onclose = () => {
      if (this.socket === socket) this.socket = null;
      if (this.disposed) return;
      this.options.onDisconnect?.();
      this.scheduleReconnect();
    };
  }

  private scheduleReconnect(): void {
    if (this.disposed) return;
    this.clearRetry();
    this.options.onState?.("closed");
    const base = this.options.baseDelayMs ?? 1000;
    const maximum = this.options.maxDelayMs ?? 30_000;
    const delay = Math.min(base * 2 ** this.retryCount, maximum);
    this.retryCount += 1;
    this.retryTimer = setTimeout(() => {
      this.retryTimer = null;
      this.connect();
    }, delay);
  }

  private clearRetry(): void {
    if (this.retryTimer !== null) {
      clearTimeout(this.retryTimer);
      this.retryTimer = null;
    }
  }
}

/**
 * WebSocket 生命周期管理：指数退避重连。
 * 连接打开后启动心跳；断线后按 1s, 2s, 4s… 上限 30s 重连。
 */
export function useEventSocket(url: string, onMessage: (evt: MessageEvent) => void) {
  const [state, setState] = useState<ConnectionState>("connecting");
  const onMessageRef = useRef(onMessage);
  onMessageRef.current = onMessage;

  useEffect(() => {
    const socket = new ReconnectingSocket(url, {
      onMessage: (event) => onMessageRef.current(event),
      onState: setState,
    });
    socket.start();
    return () => socket.stop();
  }, [url]);

  return { state };
}
