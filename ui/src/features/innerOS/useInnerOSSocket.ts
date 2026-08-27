import { useEffect, useRef, useState, useCallback } from "react";
import { ReconnectingSocket, type ConnectionState } from "../../hooks/useEventSocket";
import { runtimeConfig, deriveWebSocketUrl } from "../../config/runtimeConfig";
import {
  isInnerOSEventEnvelope,
  type InnerOSAnswer,
  type InnerOSCancelCommand,
  type InnerOSEphemeralContext,
  type InnerOSIntent,
  type InnerOSQueryCommand,
} from "./contracts";
import { useInnerOSStore } from "./innerOSStore";
import { innerOSMetrics } from "./metrics";

function generateUUID(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0;
    const v = c === "x" ? r : (r & 0x3) | 0x8;
    return v.toString(16);
  });
}

function isLoopbackHost(hostname: string): boolean {
  return (
    hostname === "localhost" ||
    hostname === "127.0.0.1" ||
    hostname === "[::1]" ||
    hostname === "::1" ||
    hostname.endsWith(".localhost")
  );
}

export interface UseInnerOSSocketOptions {
  readonly meetingId: string | null;
  readonly enabled?: boolean;
}

export function useInnerOSSocket({ meetingId, enabled = true }: UseInnerOSSocketOptions) {
  const [connectionState, setConnectionState] = useState<ConnectionState>("closed");
  const [isLoopbackSecure, setIsLoopbackSecure] = useState(true);
  const socketRef = useRef<WebSocket | null>(null);
  const reconnectingSocketRef = useRef<ReconnectingSocket | null>(null);
  const queryStartTimeRef = useRef<number | null>(null);

  const activeQueryId = useInnerOSStore((s) => s.activeQueryId);
  const queryStatus = useInnerOSStore((s) => s.queryStatus);

  useEffect(() => {
    if (typeof window !== "undefined") {
      const isLoopback = isLoopbackHost(window.location.hostname);
      setIsLoopbackSecure(isLoopback);
    }
  }, []);

  useEffect(() => {
    if (!enabled || !meetingId) {
      if (reconnectingSocketRef.current) {
        reconnectingSocketRef.current.stop();
        reconnectingSocketRef.current = null;
      }
      setConnectionState("closed");
      return;
    }

    const wsPath = `/ws/v1/meetings/${encodeURIComponent(meetingId)}/inner-os`;
    const wsUrl = deriveWebSocketUrl(runtimeConfig.apiBaseUrl, wsPath);

    const rSocket = new ReconnectingSocket(wsUrl, {
      onOpen: (ws) => {
        socketRef.current = ws;
      },
      onState: (state) => {
        setConnectionState(state);
      },
      onDisconnect: () => {
        socketRef.current = null;
      },
      onMessage: (event) => {
        if (typeof event.data !== "string") return;
        try {
          const envelope = JSON.parse(event.data);
          if (!isInnerOSEventEnvelope(envelope)) return;
          if (envelope.meeting_id !== meetingId) return;

          const store = useInnerOSStore.getState();
          const queryId = envelope.query_id;

          switch (envelope.type) {
            case "inner_os_query_accepted":
              store.setAccepted(queryId);
              innerOSMetrics.recordQueryAccepted();
              break;

            case "inner_os_answer_started":
              store.setGenerating(queryId);
              break;

            case "inner_os_answer_completed": {
              const elapsedMs = queryStartTimeRef.current
                ? Date.now() - queryStartTimeRef.current
                : 1500;
              const answer = envelope.payload as InnerOSAnswer;
              store.setCompleted(queryId, answer);
              innerOSMetrics.recordQueryCompleted(elapsedMs);
              break;
            }

            case "inner_os_answer_failed": {
              const errPayload = envelope.payload as { error?: { code?: string; message?: string } };
              const code = errPayload?.error?.code || "inner_os_internal_error";
              const message = errPayload?.error?.message || "内心 OS 处理遇到异常";
              store.setFailed(queryId, code, message);
              innerOSMetrics.recordQueryFailed();
              break;
            }

            case "inner_os_answer_cancelled": {
              const cancelPayload = envelope.payload as { reason?: string };
              store.setCancelled(queryId, cancelPayload?.reason);
              innerOSMetrics.recordQueryCancelled();
              break;
            }
          }
        } catch {
          // ignore malformed socket events
        }
      },
    });

    reconnectingSocketRef.current = rSocket;
    rSocket.start();

    return () => {
      rSocket.stop();
      reconnectingSocketRef.current = null;
      socketRef.current = null;
    };
  }, [meetingId, enabled]);

  const sendQuery = useCallback(
    (
      question: string,
      intent: InnerOSIntent,
      contextVersion: number,
      ephemeralContext?: InnerOSEphemeralContext | null,
      focusSegmentIds?: readonly string[],
    ) => {
      if (!meetingId) return null;
      const ws = socketRef.current;
      if (!ws || ws.readyState !== WebSocket.OPEN) {
        return null;
      }

      const queryId = generateUUID();
      const requestId = `req-${Date.now()}`;
      queryStartTimeRef.current = Date.now();

      const cmd: InnerOSQueryCommand = {
        contract_version: "1",
        request_id: requestId,
        cmd: "query",
        query_id: queryId,
        meeting_id: meetingId,
        question: question.trim(),
        intent,
        context_version: contextVersion,
        ephemeral_context: ephemeralContext || null,
        focus_segment_ids: focusSegmentIds && focusSegmentIds.length > 0 ? focusSegmentIds : undefined,
      };

      useInnerOSStore.getState().startQuery(queryId, meetingId, question.trim(), intent);
      innerOSMetrics.recordQuerySubmitted();

      ws.send(JSON.stringify(cmd));
      return queryId;
    },
    [meetingId],
  );

  const sendCancel = useCallback(() => {
    const currentQueryId = useInnerOSStore.getState().activeQueryId;
    if (!currentQueryId) return;

    const ws = socketRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) {
      const cancelCmd: InnerOSCancelCommand = {
        contract_version: "1",
        request_id: `cancel-${Date.now()}`,
        cmd: "cancel",
        query_id: currentQueryId,
      };
      ws.send(JSON.stringify(cancelCmd));
    }

    useInnerOSStore.getState().setCancelled(currentQueryId, "user_cancelled");
    innerOSMetrics.recordQueryCancelled();
  }, []);

  return {
    connectionState,
    isConnected: connectionState === "open",
    isLoopbackSecure,
    activeQueryId,
    queryStatus,
    sendQuery,
    sendCancel,
  };
}
