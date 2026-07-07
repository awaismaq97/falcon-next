// SSE client for POST /api/chat/send.
//
// EventSource only supports GET, but the send endpoint is a POST with a JSON
// body, so we stream the text/event-stream response through fetch + a
// ReadableStream reader and parse SSE frames ourselves.

import { API_BASE } from "./api";
import type { ChatSettings, DocAttachment, SSEEvent } from "./types";

export interface SendParams {
  identity_id: string;
  message: string;
  images: string[];
  documents: DocAttachment[];
  settings: ChatSettings;
}

export function streamChat(
  params: SendParams,
  onEvent: (event: SSEEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  return (async () => {
    const res = await fetch(`${API_BASE}/api/chat/send`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
      body: JSON.stringify(params),
      signal,
    });

    if (!res.ok || !res.body) {
      let detail = res.statusText;
      try {
        detail = (await res.json()).detail ?? detail;
      } catch {
        /* ignore */
      }
      onEvent({ type: "error", message: detail });
      return;
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    const flushFrame = (frame: string) => {
      // Collect all `data:` lines in the frame; ignore `event:` (type is in the
      // JSON) and `:` comment/ping lines.
      const dataLines: string[] = [];
      for (const line of frame.split("\n")) {
        if (line.startsWith("data:")) dataLines.push(line.slice(5).trimStart());
      }
      if (dataLines.length === 0) return;
      const payload = dataLines.join("\n");
      try {
        onEvent(JSON.parse(payload) as SSEEvent);
      } catch {
        /* skip malformed frame */
      }
    };

    // eslint-disable-next-line no-constant-condition
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      // Frames are separated by a blank line. Normalise CRLF.
      let sep: number;
      buffer = buffer.replace(/\r\n/g, "\n");
      while ((sep = buffer.indexOf("\n\n")) !== -1) {
        const frame = buffer.slice(0, sep);
        buffer = buffer.slice(sep + 2);
        flushFrame(frame);
      }
    }
    if (buffer.trim()) flushFrame(buffer);
  })();
}
