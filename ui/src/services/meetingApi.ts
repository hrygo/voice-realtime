import {
  ApiError,
  type ExportFormat,
  type MeetingDetail,
  type MeetingListResponse,
  type MeetingMinutesVersion,
  type MeetingSpeaker,
  type TranscriptResponse,
} from "../contracts/meetingContract";
import type { RuntimeStateSnapshot } from "../protocol";
import { apiUrl } from "../config/runtimeConfig";

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
      // Non-JSON response
    }

    throw new ApiError(errorMessage, errorCode, requestId, details);
  }

  if (res.status === 204) {
    return undefined as unknown as T;
  }

  return (await res.json()) as T;
}

export const meetingApi = {
  async fetchRuntimeState(): Promise<RuntimeStateSnapshot> {
    const res = await fetch(apiUrl("/api/v1/runtime"));
    return handleResponse<RuntimeStateSnapshot>(res);
  },

  async fetchMeetings(cursor?: string | null, limit = 20): Promise<MeetingListResponse> {
    const params = new URLSearchParams();
    if (cursor) params.set("cursor", cursor);
    if (limit) params.set("limit", String(limit));
    const qs = params.toString();
    const res = await fetch(apiUrl(`/api/v1/meetings${qs ? `?${qs}` : ""}`));
    return handleResponse<MeetingListResponse>(res);
  },

  async fetchMeeting(id: string): Promise<MeetingDetail> {
    const res = await fetch(apiUrl(`/api/v1/meetings/${encodeURIComponent(id)}`));
    return handleResponse<MeetingDetail>(res);
  },

  async fetchTranscript(id: string): Promise<TranscriptResponse> {
    const res = await fetch(apiUrl(`/api/v1/meetings/${encodeURIComponent(id)}/transcript`));
    return handleResponse<TranscriptResponse>(res);
  },

  async updateMeetingTitle(id: string, title: string): Promise<MeetingDetail> {
    const res = await fetch(apiUrl(`/api/v1/meetings/${encodeURIComponent(id)}`), {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title }),
    });
    return handleResponse<MeetingDetail>(res);
  },

  async generateMeetingTitle(id: string): Promise<MeetingDetail> {
    const res = await fetch(apiUrl(`/api/v1/meetings/${encodeURIComponent(id)}/generate-title`), {
      method: "POST",
    });
    return handleResponse<MeetingDetail>(res);
  },


  async updateSpeakerName(id: string, speakerKey: string, displayName: string): Promise<MeetingSpeaker> {
    const res = await fetch(
      apiUrl(`/api/v1/meetings/${encodeURIComponent(id)}/speakers/${encodeURIComponent(speakerKey)}`),
      {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ display_name: displayName }),
      },
    );
    return handleResponse<MeetingSpeaker>(res);
  },

  async generateMinutes(id: string, idempotencyKey?: string): Promise<MeetingMinutesVersion> {
    const headers: Record<string, string> = {
      "Content-Type": "application/json",
    };
    if (idempotencyKey) {
      headers["Idempotency-Key"] = idempotencyKey;
    }
    const res = await fetch(apiUrl(`/api/v1/meetings/${encodeURIComponent(id)}/minutes`), {
      method: "POST",
      headers,
    });
    return handleResponse<MeetingMinutesVersion>(res);
  },

  async fetchMinutesVersion(id: string, version: number): Promise<MeetingMinutesVersion> {
    const res = await fetch(
      apiUrl(`/api/v1/meetings/${encodeURIComponent(id)}/minutes/${encodeURIComponent(String(version))}`),
    );
    return handleResponse<MeetingMinutesVersion>(res);
  },

  getExportUrl(id: string, format: ExportFormat): string {
    return apiUrl(`/api/v1/meetings/${encodeURIComponent(id)}/export?format=${encodeURIComponent(format)}`);
  },

  async downloadExport(id: string, format: ExportFormat, filename?: string): Promise<void> {
    const url = this.getExportUrl(id, format);
    const res = await fetch(url);
    if (!res.ok) {
      await handleResponse(res);
      return;
    }
    const blob = await res.blob();
    const defaultExt = format === "srt" ? ".srt" : format === "json" ? ".json" : format === "txt" ? ".txt" : ".md";
    const saveName = filename ? (filename.endsWith(defaultExt) ? filename : `${filename}${defaultExt}`) : `meeting-${id}${defaultExt}`;

    const objectUrl = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = objectUrl;
    link.download = saveName;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    URL.revokeObjectURL(objectUrl);
  },

  async deleteMeeting(id: string): Promise<void> {
    const res = await fetch(apiUrl(`/api/v1/meetings/${encodeURIComponent(id)}`), {
      method: "DELETE",
    });
    return handleResponse<void>(res);
  },
};
