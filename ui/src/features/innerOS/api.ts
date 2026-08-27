import { ApiError } from "../../contracts/meetingContract";
import { apiUrl } from "../../config/runtimeConfig";
import type {
  InnerOSExchange,
  InnerOSExchangeListResponse,
} from "./contracts";

async function handleResponse<T>(res: Response): Promise<T> {
  if (!res.ok) {
    let errorCode = "internal_error";
    let errorMessage = `请求失败 (HTTP ${res.status})`;
    let requestId: string | undefined;
    let details: Record<string, unknown> | undefined;

    try {
      const data = await res.json();
      if (data && typeof data === "object") {
        if ("error" in data && data.error && typeof data.error === "object") {
          errorCode = data.error.code || errorCode;
          errorMessage = data.error.message || errorMessage;
          requestId = data.error.request_id;
          details = data.error.details;
        } else if ("detail" in data && typeof data.detail === "string") {
          errorMessage = data.detail;
        }
      }
    } catch {
      // Non-JSON response fallback
    }

    throw new ApiError(errorMessage, errorCode, requestId, details);
  }

  if (res.status === 204) {
    return undefined as unknown as T;
  }

  return (await res.json()) as T;
}

export const innerOSApi = {
  /** 幂等保存一条问答记录 */
  async saveExchange(meetingId: string, exchangeId: string): Promise<InnerOSExchange> {
    const res = await fetch(
      apiUrl(`/api/v1/meetings/${encodeURIComponent(meetingId)}/inner-os/exchanges/${encodeURIComponent(exchangeId)}`),
      {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
      },
    );
    return handleResponse<InnerOSExchange>(res);
  },

  /** 分页获取已保存的内心 OS 问答列表 */
  async listExchanges(
    meetingId: string,
    cursor?: string | null,
    limit = 20,
  ): Promise<InnerOSExchangeListResponse> {
    const params = new URLSearchParams();
    if (cursor) params.set("cursor", cursor);
    if (limit) params.set("limit", String(limit));
    const qs = params.toString();
    const res = await fetch(
      apiUrl(`/api/v1/meetings/${encodeURIComponent(meetingId)}/inner-os/exchanges${qs ? `?${qs}` : ""}`),
    );
    return handleResponse<InnerOSExchangeListResponse>(res);
  },

  /** 获取单条已保存的内心 OS 问答详情 */
  async getExchange(meetingId: string, exchangeId: string): Promise<InnerOSExchange> {
    const res = await fetch(
      apiUrl(`/api/v1/meetings/${encodeURIComponent(meetingId)}/inner-os/exchanges/${encodeURIComponent(exchangeId)}`),
    );
    return handleResponse<InnerOSExchange>(res);
  },

  /** 幂等删除单条已保存的问答记录 */
  async deleteExchange(meetingId: string, exchangeId: string): Promise<void> {
    const res = await fetch(
      apiUrl(`/api/v1/meetings/${encodeURIComponent(meetingId)}/inner-os/exchanges/${encodeURIComponent(exchangeId)}`),
      { method: "DELETE" },
    );
    return handleResponse<void>(res);
  },
};
