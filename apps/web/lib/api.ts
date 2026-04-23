export const API_URL =
  process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export type MeetingListItem = {
  id: string;
  title: string | null;
  meeting_url: string | null;
  status: string;
  started_at: string | null;
  ended_at: string | null;
  created_at: string;
  top_insight_title: string | null;
  top_insight_type: string | null;
  insight_count: number;
  has_high_severity: boolean;
};

export type Utterance = {
  id: string;
  speaker_label: string | null;
  text: string;
  start_ms: number | null;
  end_ms: number | null;
  created_at: string;
};

export type Insight = {
  id: string;
  type: string;
  title: string;
  description: string | null;
  severity: string | null;
  confidence: number | null;
  created_at: string;
  evidence_utterance_ids: string[];
};

export type ActionItem = {
  id: string;
  owner_name: string | null;
  action_text: string;
  due_hint: string | null;
  status: string;
};

export type Summary = {
  id: string;
  summary_type: string;
  content_markdown: string;
};

export type MeetingDetail = {
  id: string;
  title: string | null;
  meeting_url: string | null;
  status: string;
  started_at: string | null;
  ended_at: string | null;
  recall_bot_id: string | null;
  owner_name: string | null;
  utterances: Utterance[];
  insights: Insight[];
  action_items: ActionItem[];
  summaries: Summary[];
};

async function handle<T>(r: Response): Promise<T> {
  if (!r.ok) {
    const text = await r.text();
    throw new Error(`api ${r.status}: ${text.slice(0, 500)}`);
  }
  return r.json() as Promise<T>;
}

export async function listMeetings(opts?: { status?: string; signal?: AbortSignal }) {
  const qs = opts?.status ? `?status=${encodeURIComponent(opts.status)}` : "";
  const r = await fetch(`${API_URL}/meetings${qs}`, {
    cache: "no-store",
    signal: opts?.signal,
  });
  return handle<MeetingListItem[]>(r);
}

export async function getMeeting(id: string, opts?: { signal?: AbortSignal }) {
  const r = await fetch(`${API_URL}/meetings/${id}`, {
    cache: "no-store",
    signal: opts?.signal,
  });
  return handle<MeetingDetail>(r);
}

export async function createMeeting(body: {
  meeting_url: string;
  title?: string;
  meeting_type?: string;
  owner_name?: string;
}) {
  const r = await fetch(`${API_URL}/meetings`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  return handle<{ meeting_id: string; recall_bot_id: string | null; status: string }>(r);
}

export async function finalizeMeeting(id: string) {
  const r = await fetch(`${API_URL}/meetings/${id}/finalize`, { method: "POST" });
  return handle<{ meeting_id: string; status: string }>(r);
}

export async function crmPush(id: string) {
  const r = await fetch(`${API_URL}/meetings/${id}/crm-push`, { method: "POST" });
  return handle<{ meeting_id: string; pushed_at: string }>(r);
}

export type OpsEvent = {
  id: number;
  source: string;
  event_type: string;
  event_timestamp: string;
  received_at: string;
  persisted_at: string;
  dedupe_key: string;
  signature_valid: boolean;
};

export type OpsDelivery = {
  id: number;
  event_type: string | null;
  signature_valid: boolean;
  response_code: number | null;
  attempt_count: number;
  received_at: string;
  remote_addr: string | null;
};

export type OpsSpan = {
  utterance_id: string;
  text: string;
  speaker_label: string | null;
  start_ms: number | null;
  received_at: string | null;
  persisted_at: string | null;
  enqueued_at: string | null;
  classified_at: string | null;
  pushed_at: string | null;
  end_to_end_ms: number | null;
};

export type OpsDLQ = {
  id: string;
  job_kind: string;
  error: string | null;
  status: string;
  attempt_count: number;
  created_at: string;
};

export type OpsMetrics = {
  events_accepted: number;
  webhook_deliveries_ok: number;
  webhook_deliveries_bad_sig: number;
  duplicates_absorbed: number;
  utterance_count: number;
  p50_end_to_end_ms: number | null;
  p95_end_to_end_ms: number | null;
  p99_end_to_end_ms: number | null;
};

export type OpsResponse = {
  meeting_id: string;
  status: string;
  events: OpsEvent[];
  deliveries: OpsDelivery[];
  utterance_spans: OpsSpan[];
  dlq: OpsDLQ[];
  metrics: OpsMetrics;
};

export async function getMeetingOps(id: string, opts?: { signal?: AbortSignal }) {
  const r = await fetch(`${API_URL}/meetings/${id}/ops`, {
    cache: "no-store",
    signal: opts?.signal,
  });
  return handle<OpsResponse>(r);
}

export async function replayMeeting(id: string) {
  const r = await fetch(`${API_URL}/admin/replay/${id}`, { method: "POST" });
  return handle<{ meeting_id: string; status: string }>(r);
}

export async function retryDlq(jobId: string) {
  const r = await fetch(`${API_URL}/admin/dlq/${jobId}/retry`, { method: "POST" });
  return handle<{ id: string; status: string; detail?: string }>(r);
}

export async function resolveDlq(jobId: string) {
  const r = await fetch(`${API_URL}/admin/dlq/${jobId}/resolve`, { method: "POST" });
  return handle<{ id: string; status: string }>(r);
}

export function liveWebsocketUrl(meetingId: string): string {
  const base = API_URL.replace(/^http/, "ws");
  return `${base}/live/${meetingId}`;
}
