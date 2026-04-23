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

export function liveWebsocketUrl(meetingId: string): string {
  const base = API_URL.replace(/^http/, "ws");
  return `${base}/live/${meetingId}`;
}
