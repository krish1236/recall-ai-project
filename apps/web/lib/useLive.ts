"use client";

import { useEffect, useRef, useState } from "react";
import { liveWebsocketUrl } from "./api";

export type LiveFrame =
  | { type: "connected"; meeting_id: string }
  | {
      type: "utterance";
      id: string;
      speaker_label: string | null;
      text: string;
      start_ms: number | null;
      end_ms: number | null;
    }
  | {
      type: "insights";
      insights: Array<{
        id: string;
        type: string;
        title: string;
        description: string | null;
        severity: string | null;
        confidence: number | null;
      }>;
    }
  | {
      type: "state";
      status: string;
      state_changed_at: string;
    }
  | { type: "summary_ready" }
  | { type: "crm_pushed"; at: string };

export type ConnState = "connecting" | "open" | "closed" | "error";

/**
 * Subscribe to /live/{meetingId}. Handles reconnect with simple backoff. Frames
 * are pushed to onFrame; connection state is returned for UI feedback.
 */
export function useLive(meetingId: string, onFrame: (f: LiveFrame) => void) {
  const [state, setState] = useState<ConnState>("connecting");
  const [lastFrameAt, setLastFrameAt] = useState<number | null>(null);
  const onFrameRef = useRef(onFrame);
  onFrameRef.current = onFrame;

  useEffect(() => {
    let closed = false;
    let ws: WebSocket | null = null;
    let retry = 0;
    let timer: number | null = null;

    const connect = () => {
      if (closed) return;
      setState("connecting");
      ws = new WebSocket(liveWebsocketUrl(meetingId));
      ws.onopen = () => {
        retry = 0;
        setState("open");
      };
      ws.onmessage = (e) => {
        try {
          const frame = JSON.parse(e.data) as LiveFrame;
          setLastFrameAt(Date.now());
          onFrameRef.current(frame);
        } catch {
          /* ignore non-json */
        }
      };
      ws.onerror = () => setState("error");
      ws.onclose = () => {
        if (closed) return;
        setState("closed");
        const delay = Math.min(30_000, 500 * 2 ** retry++);
        timer = window.setTimeout(connect, delay);
      };
    };

    connect();
    return () => {
      closed = true;
      if (timer) window.clearTimeout(timer);
      if (ws) ws.close();
    };
  }, [meetingId]);

  return { state, lastFrameAt };
}
