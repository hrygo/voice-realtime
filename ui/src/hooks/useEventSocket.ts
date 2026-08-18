import { useEffect, useRef, useState, useCallback } from "react";

export type ConnectionState = "connecting" | "open" | "closed";

/**
 * WebSocket 生命周期管理：指数退避重连（移植 wlk 官方 UI 语义）。
 * 连接打开后启动心跳；断线后按 1s, 2s, 4s… 上限 30s 重连。
 */
export function useEventSocket(url: string, onMessage: (evt: MessageEvent) => void) {
  const [state, setState] = useState<ConnectionState>("connecting");
  const wsRef = useRef<WebSocket | null>(null);
  const retryRef = useRef(0);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const onMessageRef = useRef(onMessage);
  onMessageRef.current = onMessage;

  const scheduleReconnect = useCallback(() => {
    const delay = Math.min(1000 * 2 ** retryRef.current, 30_000);
    retryRef.current += 1;
    timerRef.current = setTimeout(() => {
      wsRef.current?.close();
      connect();
    }, delay);
    setState("closed");
  }, []);

  const connect = useCallback(() => {
    setState("connecting");
    try {
      const ws = new WebSocket(url);
      wsRef.current = ws;
      ws.onopen = () => {
        retryRef.current = 0;
        setState("open");
      };
      ws.onmessage = (evt) => onMessageRef.current(evt);
      ws.onclose = () => scheduleReconnect();
      ws.onerror = () => ws.close();
    } catch {
      scheduleReconnect();
    }
  }, [url, scheduleReconnect]);

  useEffect(() => {
    connect();
    return () => {
      if (timerRef.current) clearTimeout(timerRef.current);
      wsRef.current?.close();
    };
  }, [connect]);

  return { state };
}