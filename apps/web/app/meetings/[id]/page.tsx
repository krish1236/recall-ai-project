"use client";

import Link from "next/link";
import { use, useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  getMeeting,
  type Insight,
  type MeetingDetail,
  type Utterance,
} from "@/lib/api";
import { useLive, type LiveFrame } from "@/lib/useLive";

const LIVE_STATES = new Set(["requested", "joining", "in_call"]);

export default function MeetingPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  const [meeting, setMeeting] = useState<MeetingDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [utterances, setUtterances] = useState<Utterance[]>([]);
  const [insights, setInsights] = useState<Insight[]>([]);
  const [status, setStatus] = useState<string>("requested");

  useEffect(() => {
    let cancelled = false;
    getMeeting(id)
      .then((m) => {
        if (cancelled) return;
        setMeeting(m);
        setUtterances(m.utterances);
        setInsights(m.insights);
        setStatus(m.status);
      })
      .catch((e) => !cancelled && setError(e.message ?? "failed to load"));
    return () => {
      cancelled = true;
    };
  }, [id]);

  const onFrame = useCallback((frame: LiveFrame) => {
    if (frame.type === "utterance") {
      setUtterances((prev) => {
        if (prev.some((u) => u.id === frame.id)) return prev;
        return [
          ...prev,
          {
            id: frame.id,
            speaker_label: frame.speaker_label,
            text: frame.text,
            start_ms: frame.start_ms,
            end_ms: frame.end_ms,
            created_at: new Date().toISOString(),
          },
        ];
      });
    } else if (frame.type === "insights") {
      setInsights((prev) => {
        const existing = new Set(prev.map((i) => i.id));
        const incoming = frame.insights
          .filter((i) => !existing.has(i.id))
          .map((i) => ({
            id: i.id,
            type: i.type,
            title: i.title,
            description: i.description,
            severity: i.severity,
            confidence: i.confidence,
            created_at: new Date().toISOString(),
            evidence_utterance_ids: [] as string[],
          }));
        return [...prev, ...incoming];
      });
    } else if (frame.type === "state") {
      setStatus(frame.status);
    }
  }, []);

  const liveState = useLive(id, onFrame);
  const isLive = LIVE_STATES.has(status);

  if (error) {
    return (
      <div className="rounded-md border border-red-500/30 bg-red-500/5 p-4 text-sm text-red-300">
        {error}
      </div>
    );
  }
  if (!meeting) {
    return <div className="text-sm text-[var(--muted)]">Loading…</div>;
  }

  return (
    <div className="space-y-6">
      <div>
        <Link href="/" className="text-sm text-[var(--muted)] hover:text-[var(--foreground)]">
          ← Inbox
        </Link>
        <div className="mt-2 flex items-center gap-3">
          <h1 className="text-2xl font-semibold tracking-tight">
            {meeting.title ?? "Untitled meeting"}
          </h1>
          <StatusPill status={status} />
          <WsDot conn={liveState.state} lastFrameAt={liveState.lastFrameAt} />
        </div>
        <p className="text-sm text-[var(--muted)] mt-1">
          {meeting.meeting_url ? (
            <a
              href={meeting.meeting_url}
              target="_blank"
              rel="noreferrer"
              className="hover:text-[var(--foreground)]"
            >
              {meeting.meeting_url}
            </a>
          ) : (
            <span>no url</span>
          )}
          {meeting.recall_bot_id && (
            <span className="ml-3 font-mono text-xs">
              bot {meeting.recall_bot_id.slice(0, 12)}…
            </span>
          )}
        </p>
      </div>

      {isLive ? (
        <LiveLayout utterances={utterances} insights={insights} />
      ) : (
        <IntelligenceLayout
          meeting={meeting}
          utterances={utterances}
          insights={insights}
        />
      )}
    </div>
  );
}

function LiveLayout({
  utterances,
  insights,
}: {
  utterances: Utterance[];
  insights: Insight[];
}) {
  return (
    <div className="grid grid-cols-1 lg:grid-cols-[1fr_360px] gap-6">
      <div className="border hairline rounded-lg bg-[var(--surface)]/50">
        <div className="px-4 py-2 border-b hairline text-xs uppercase tracking-wider text-[var(--muted)]">
          Transcript ({utterances.length})
        </div>
        <TranscriptPanel utterances={utterances} />
      </div>
      <aside className="space-y-4">
        <div className="border hairline rounded-lg bg-[var(--surface)]/50">
          <div className="px-4 py-2 border-b hairline text-xs uppercase tracking-wider text-[var(--muted)]">
            Live signals ({insights.length})
          </div>
          <div className="p-3 space-y-2 max-h-[480px] overflow-y-auto">
            {insights.length === 0 ? (
              <div className="text-sm text-[var(--muted)] italic px-1 py-4 text-center">
                Waiting for the batcher to flush…
              </div>
            ) : (
              insights
                .slice()
                .reverse()
                .map((i) => <InsightRow key={i.id} insight={i} />)
            )}
          </div>
        </div>
      </aside>
    </div>
  );
}

function IntelligenceLayout({
  meeting,
  utterances,
  insights,
}: {
  meeting: MeetingDetail;
  utterances: Utterance[];
  insights: Insight[];
}) {
  const insightsByType = useMemo(() => {
    const m: Record<string, Insight[]> = {};
    for (const i of insights) {
      (m[i.type] ??= []).push(i);
    }
    return m;
  }, [insights]);

  const summaryOrder = [
    "objection",
    "commitment",
    "competitor_mention",
    "feature_request",
    "customer_goal",
    "risk",
    "urgency",
  ];

  const utteranceMap = useMemo(() => {
    const m: Record<string, Utterance> = {};
    for (const u of utterances) m[u.id] = u;
    return m;
  }, [utterances]);

  const execSummary = meeting.summaries.find((s) => s.summary_type === "executive_summary");
  const followup = meeting.summaries.find((s) => s.summary_type === "followup_email");
  const crmNote = meeting.summaries.find((s) => s.summary_type === "crm_note");

  return (
    <div className="grid grid-cols-1 lg:grid-cols-[1fr_320px] gap-6">
      <div className="space-y-5">
        {execSummary && (
          <Card title="Summary">
            <Markdownish text={execSummary.content_markdown} />
          </Card>
        )}

        {summaryOrder.map((type) => {
          const items = insightsByType[type];
          if (!items?.length) return null;
          return (
            <Card key={type} title={sectionTitle(type)}>
              <ul className="space-y-3">
                {items.map((i) => (
                  <li key={i.id}>
                    <div className="flex items-baseline gap-2">
                      <span className="font-medium">{i.title}</span>
                      {i.severity && <SeverityPill severity={i.severity} />}
                      {i.confidence != null && (
                        <span className="text-xs text-[var(--muted)]">
                          {Math.round(i.confidence * 100)}%
                        </span>
                      )}
                    </div>
                    {i.description && (
                      <div className="text-sm text-[var(--muted)] mt-0.5">{i.description}</div>
                    )}
                    {i.evidence_utterance_ids.length > 0 && (
                      <div className="mt-2 pl-3 border-l hairline space-y-1">
                        {i.evidence_utterance_ids.map((uid) => {
                          const u = utteranceMap[uid];
                          if (!u) return null;
                          return (
                            <div key={uid} className="text-xs text-[var(--muted)]">
                              <span className="font-medium mr-1">{u.speaker_label ?? "?"}:</span>
                              {u.text}
                            </div>
                          );
                        })}
                      </div>
                    )}
                  </li>
                ))}
              </ul>
            </Card>
          );
        })}

        {meeting.action_items.length > 0 && (
          <Card title="Action items">
            <ul className="space-y-2">
              {meeting.action_items.map((a) => (
                <li key={a.id} className="flex items-baseline justify-between gap-3">
                  <div>
                    <div>{a.action_text}</div>
                    <div className="text-xs text-[var(--muted)] mt-0.5">
                      {a.owner_name ?? "unassigned"}
                      {a.due_hint ? ` · ${a.due_hint}` : ""}
                    </div>
                  </div>
                  <span className="text-xs text-[var(--muted)]">{a.status}</span>
                </li>
              ))}
            </ul>
          </Card>
        )}

        {followup && (
          <Card
            title="Follow-up email"
            action={<CopyButton text={followup.content_markdown} />}
          >
            <pre className="text-sm whitespace-pre-wrap font-sans text-[var(--foreground)]/90">
              {followup.content_markdown}
            </pre>
          </Card>
        )}

        {crmNote && (
          <Card title="CRM note" action={<CopyButton text={crmNote.content_markdown} />}>
            <pre className="text-sm whitespace-pre-wrap font-sans text-[var(--foreground)]/90">
              {crmNote.content_markdown}
            </pre>
          </Card>
        )}
      </div>

      <aside>
        <Card title={`Transcript (${utterances.length})`}>
          <TranscriptPanel utterances={utterances} compact />
        </Card>
      </aside>
    </div>
  );
}

function TranscriptPanel({
  utterances,
  compact,
}: {
  utterances: Utterance[];
  compact?: boolean;
}) {
  const endRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!compact) endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [utterances.length, compact]);

  return (
    <div className={compact ? "max-h-[500px] overflow-y-auto px-1" : "max-h-[560px] overflow-y-auto px-4 py-3"}>
      {utterances.length === 0 ? (
        <div className="text-sm text-[var(--muted)] italic py-6 text-center">
          No utterances yet. Speak in the meeting — captions will stream in.
        </div>
      ) : (
        <ul className="space-y-3">
          {utterances.map((u) => (
            <li key={u.id} className="text-sm">
              <div className="flex items-baseline gap-2">
                <span className="font-medium text-[var(--foreground)]/90">
                  {u.speaker_label ?? "?"}
                </span>
                {u.start_ms != null && (
                  <span className="text-xs text-[var(--muted)] font-mono">
                    {formatTime(u.start_ms)}
                  </span>
                )}
              </div>
              <div className="text-[var(--foreground)]/80">{u.text}</div>
            </li>
          ))}
          <div ref={endRef} />
        </ul>
      )}
    </div>
  );
}

function InsightRow({ insight }: { insight: Insight }) {
  return (
    <div className="border hairline rounded-md bg-[var(--surface-2)]/50 p-2.5">
      <div className="flex items-center gap-2">
        <TypeTag type={insight.type} />
        {insight.severity && <SeverityPill severity={insight.severity} />}
        {insight.confidence != null && (
          <span className="text-xs text-[var(--muted)]">
            {Math.round(insight.confidence * 100)}%
          </span>
        )}
      </div>
      <div className="mt-1 text-sm font-medium">{insight.title}</div>
      {insight.description && (
        <div className="text-xs text-[var(--muted)] mt-1">{insight.description}</div>
      )}
    </div>
  );
}

function TypeTag({ type }: { type: string }) {
  const colors: Record<string, string> = {
    objection: "bg-red-500/10 text-red-300",
    feature_request: "bg-indigo-500/10 text-indigo-300",
    competitor_mention: "bg-amber-500/10 text-amber-300",
    commitment: "bg-emerald-500/10 text-emerald-300",
    customer_goal: "bg-sky-500/10 text-sky-300",
    risk: "bg-red-500/10 text-red-300",
    urgency: "bg-amber-500/10 text-amber-300",
  };
  return (
    <span className={`text-[10px] uppercase tracking-wider px-1.5 py-0.5 rounded ${
      colors[type] ?? "bg-[var(--surface-2)] text-[var(--muted)]"
    }`}>
      {type.replace(/_/g, " ")}
    </span>
  );
}

function StatusPill({ status }: { status: string }) {
  const colorMap: Record<string, string> = {
    requested: "bg-[var(--surface-2)] text-[var(--muted)]",
    joining: "bg-amber-500/10 text-amber-300",
    in_call: "bg-emerald-500/10 text-emerald-300",
    processing: "bg-indigo-500/10 text-indigo-300",
    done: "bg-indigo-500/10 text-indigo-300",
    failed: "bg-red-500/10 text-red-400",
  };
  const cls = colorMap[status] ?? "bg-[var(--surface-2)] text-[var(--muted)]";
  const label = status === "in_call" ? "live" : status;
  const dot = status === "in_call" ? "bg-emerald-400 animate-pulse" : "bg-current opacity-60";
  return (
    <span className={`inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-xs ${cls}`}>
      <span className={`w-1.5 h-1.5 rounded-full ${dot}`} />
      {label}
    </span>
  );
}

function SeverityPill({ severity }: { severity: string }) {
  const map: Record<string, string> = {
    low: "bg-[var(--surface-2)] text-[var(--muted)]",
    medium: "bg-amber-500/10 text-amber-300",
    high: "bg-red-500/10 text-red-300",
  };
  return (
    <span className={`text-[10px] px-1.5 py-0.5 rounded ${map[severity] ?? map.low}`}>
      {severity}
    </span>
  );
}

function WsDot({ conn, lastFrameAt }: { conn: string; lastFrameAt: number | null }) {
  const color =
    conn === "open"
      ? "bg-emerald-400"
      : conn === "connecting"
      ? "bg-amber-400 animate-pulse"
      : "bg-red-400";
  const label =
    conn === "open"
      ? lastFrameAt
        ? `last ${Math.max(0, Math.floor((Date.now() - lastFrameAt) / 1000))}s ago`
        : "idle"
      : conn;
  return (
    <span className="inline-flex items-center gap-1.5 text-xs text-[var(--muted)]">
      <span className={`w-1.5 h-1.5 rounded-full ${color}`} />
      ws {label}
    </span>
  );
}

function Card({
  title,
  children,
  action,
}: {
  title: string;
  children: React.ReactNode;
  action?: React.ReactNode;
}) {
  return (
    <section className="border hairline rounded-lg bg-[var(--surface)]/50">
      <div className="px-4 py-2 border-b hairline flex items-center justify-between">
        <h2 className="text-xs uppercase tracking-wider text-[var(--muted)]">{title}</h2>
        {action}
      </div>
      <div className="p-4">{children}</div>
    </section>
  );
}

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      onClick={() => {
        navigator.clipboard?.writeText(text);
        setCopied(true);
        setTimeout(() => setCopied(false), 1500);
      }}
      className="text-xs text-[var(--muted)] hover:text-[var(--foreground)] transition"
    >
      {copied ? "copied" : "copy"}
    </button>
  );
}

function Markdownish({ text }: { text: string }) {
  return <div className="text-sm whitespace-pre-wrap text-[var(--foreground)]/90">{text}</div>;
}

function formatTime(ms: number): string {
  const s = Math.floor(ms / 1000);
  const mm = Math.floor(s / 60);
  const ss = s % 60;
  return `${mm}:${ss.toString().padStart(2, "0")}`;
}

function sectionTitle(type: string): string {
  const map: Record<string, string> = {
    objection: "Objections",
    commitment: "Commitments",
    competitor_mention: "Competitors mentioned",
    feature_request: "Feature requests",
    customer_goal: "Customer goals",
    risk: "Risks",
    urgency: "Urgency",
  };
  return map[type] ?? type;
}
