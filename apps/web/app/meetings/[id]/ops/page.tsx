"use client";

import Link from "next/link";
import { use, useCallback, useEffect, useMemo, useState } from "react";
import {
  getMeetingOps,
  replayMeeting,
  resolveDlq,
  retryDlq,
  type OpsDelivery,
  type OpsDLQ,
  type OpsEvent,
  type OpsMetrics,
  type OpsResponse,
  type OpsSpan,
} from "@/lib/api";

export default function OpsPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  const [ops, setOps] = useState<OpsResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [replaying, setReplaying] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setOps(await getMeetingOps(id));
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "failed");
    } finally {
      setLoading(false);
    }
  }, [id]);

  useEffect(() => {
    load();
  }, [load]);

  async function onReplay() {
    if (!confirm("Replay this meeting from the event log? Derived state will be rebuilt.")) return;
    setReplaying(true);
    try {
      await replayMeeting(id);
      // give the background task a moment, then refresh
      setTimeout(load, 2500);
    } catch (e) {
      setError(e instanceof Error ? e.message : "replay failed");
    } finally {
      setReplaying(false);
    }
  }

  if (error) {
    return (
      <div className="rounded-md border border-red-500/30 bg-red-500/5 p-4 text-sm text-red-300">
        {error}
      </div>
    );
  }
  if (loading || !ops) {
    return <div className="text-sm text-[var(--muted)]">Loading ops…</div>;
  }

  return (
    <div className="space-y-6">
      <div className="flex items-start justify-between">
        <div>
          <Link href={`/meetings/${id}`} className="text-sm text-[var(--muted)] hover:text-[var(--foreground)]">
            ← Meeting detail
          </Link>
          <h1 className="text-2xl font-semibold tracking-tight mt-2">Mission Control</h1>
          <p className="text-sm text-[var(--muted)] mt-1">
            Every webhook. Every transition. Every ms. Replay rebuilds derived state from the event log.
          </p>
        </div>
        <div className="flex gap-2">
          <button
            type="button"
            onClick={load}
            className="text-sm px-3 py-1.5 rounded-md border hairline hover:bg-[var(--surface)] transition"
          >
            Refresh
          </button>
          <button
            type="button"
            onClick={onReplay}
            disabled={replaying}
            className="text-sm px-3 py-1.5 rounded-md bg-[var(--accent)] text-white hover:brightness-110 transition disabled:opacity-50"
          >
            {replaying ? "Queuing replay…" : "Replay"}
          </button>
        </div>
      </div>

      <MetricsBar metrics={ops.metrics} />

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <LatencyWaterfall spans={ops.utterance_spans} />
        <StateTransitions events={ops.events} />
      </div>

      <EventTimeline events={ops.events} />
      <DeliveriesTable deliveries={ops.deliveries} />
      <DLQPanel dlq={ops.dlq} onChanged={load} />
    </div>
  );
}

function MetricsBar({ metrics }: { metrics: OpsMetrics }) {
  return (
    <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
      <Metric label="Events persisted" value={metrics.events_accepted} />
      <Metric
        label="Deliveries ok / bad-sig"
        value={`${metrics.webhook_deliveries_ok} / ${metrics.webhook_deliveries_bad_sig}`}
      />
      <Metric label="Duplicates absorbed" value={metrics.duplicates_absorbed} />
      <Metric label="Utterances" value={metrics.utterance_count} />
      <Metric
        label="P50 / P95 / P99 end-to-end"
        value={
          metrics.p50_end_to_end_ms == null
            ? "—"
            : `${metrics.p50_end_to_end_ms}ms / ${metrics.p95_end_to_end_ms ?? "—"}ms / ${metrics.p99_end_to_end_ms ?? "—"}ms`
        }
        mono
      />
    </div>
  );
}

function Metric({
  label,
  value,
  mono,
}: {
  label: string;
  value: React.ReactNode;
  mono?: boolean;
}) {
  return (
    <div className="border hairline rounded-lg bg-[var(--surface)]/50 p-3">
      <div className="text-[11px] uppercase tracking-wider text-[var(--muted)]">{label}</div>
      <div className={`mt-1 text-xl ${mono ? "font-mono text-sm mt-2" : "font-semibold"}`}>
        {value}
      </div>
    </div>
  );
}

function EventTimeline({ events }: { events: OpsEvent[] }) {
  return (
    <Section title={`Event timeline (${events.length})`}>
      {events.length === 0 ? (
        <Empty>No events yet.</Empty>
      ) : (
        <ol className="relative pl-5 space-y-2 border-l hairline">
          {events.map((e) => (
            <li key={e.id} className="text-sm">
              <span className="absolute -left-[5px] w-2.5 h-2.5 rounded-full bg-[var(--accent)] mt-1.5" />
              <div className="flex items-baseline gap-2 flex-wrap">
                <span className="font-mono text-xs text-[var(--muted)]">
                  {formatTime(e.event_timestamp)}
                </span>
                <span className="font-medium">{e.event_type}</span>
                {e.source === "internal" && (
                  <span className="text-[10px] px-1.5 py-0.5 rounded bg-indigo-500/10 text-indigo-300">
                    internal
                  </span>
                )}
                {!e.signature_valid && (
                  <span className="text-[10px] px-1.5 py-0.5 rounded bg-red-500/10 text-red-400">
                    bad sig
                  </span>
                )}
                <span className="font-mono text-[10px] text-[var(--muted)]">
                  {e.dedupe_key.slice(0, 10)}…
                </span>
              </div>
            </li>
          ))}
        </ol>
      )}
    </Section>
  );
}

function DeliveriesTable({ deliveries }: { deliveries: OpsDelivery[] }) {
  return (
    <Section title={`Webhook deliveries (${deliveries.length})`}>
      {deliveries.length === 0 ? (
        <Empty>No deliveries logged.</Empty>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="text-left text-[11px] uppercase tracking-wider text-[var(--muted)]">
              <tr>
                <th className="py-2 pr-3">Time</th>
                <th className="py-2 pr-3">Event</th>
                <th className="py-2 pr-3">Code</th>
                <th className="py-2 pr-3">Sig</th>
                <th className="py-2 pr-3">From</th>
              </tr>
            </thead>
            <tbody className="divide-y hairline">
              {deliveries.map((d) => (
                <tr key={d.id}>
                  <td className="py-1.5 pr-3 font-mono text-xs text-[var(--muted)]">
                    {formatTime(d.received_at)}
                  </td>
                  <td className="py-1.5 pr-3">{d.event_type ?? "—"}</td>
                  <td className="py-1.5 pr-3 font-mono text-xs">{d.response_code ?? "—"}</td>
                  <td className="py-1.5 pr-3">
                    {d.signature_valid ? (
                      <span className="text-[10px] px-1.5 py-0.5 rounded bg-emerald-500/10 text-emerald-300">
                        valid
                      </span>
                    ) : (
                      <span className="text-[10px] px-1.5 py-0.5 rounded bg-red-500/10 text-red-400">
                        invalid
                      </span>
                    )}
                  </td>
                  <td className="py-1.5 pr-3 font-mono text-xs text-[var(--muted)]">
                    {d.remote_addr ?? "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Section>
  );
}

function StateTransitions({ events }: { events: OpsEvent[] }) {
  const transitions = useMemo(() => {
    return events.filter((e) =>
      e.event_type.startsWith("bot.status_change") ||
      e.event_type.startsWith("internal.")
    );
  }, [events]);

  return (
    <Section title="State transitions">
      {transitions.length === 0 ? (
        <Empty>No state-change events.</Empty>
      ) : (
        <ul className="space-y-1 text-sm">
          {transitions.map((t) => (
            <li key={t.id} className="flex items-center gap-2">
              <span className="font-mono text-xs text-[var(--muted)]">
                {formatTime(t.event_timestamp)}
              </span>
              <span>{t.event_type}</span>
            </li>
          ))}
        </ul>
      )}
    </Section>
  );
}

function LatencyWaterfall({ spans }: { spans: OpsSpan[] }) {
  const maxMs = Math.max(1, ...spans.map((s) => s.end_to_end_ms ?? 0));
  return (
    <Section title={`Latency waterfall (${spans.length})`}>
      {spans.length === 0 ? (
        <Empty>No utterances yet.</Empty>
      ) : (
        <ul className="space-y-1.5 text-xs">
          {spans.slice(0, 20).map((s) => {
            const pct = s.end_to_end_ms ? (s.end_to_end_ms / maxMs) * 100 : 0;
            return (
              <li key={s.utterance_id} className="flex items-center gap-2">
                <span className="w-10 text-right text-[var(--muted)]">
                  {s.end_to_end_ms != null ? `${s.end_to_end_ms}ms` : "—"}
                </span>
                <div className="flex-1 h-3 bg-[var(--surface-2)] rounded-sm overflow-hidden">
                  <div
                    className="h-full bg-[var(--accent)]/60"
                    style={{ width: `${Math.min(100, pct)}%` }}
                  />
                </div>
                <span className="truncate max-w-[260px] text-[var(--muted)]" title={s.text}>
                  {s.text}
                </span>
              </li>
            );
          })}
          {spans.length > 20 && (
            <li className="text-[var(--muted)] pt-1">+ {spans.length - 20} more</li>
          )}
        </ul>
      )}
    </Section>
  );
}

function DLQPanel({ dlq, onChanged }: { dlq: OpsDLQ[]; onChanged: () => void }) {
  return (
    <Section title={`Dead-letter jobs (${dlq.length})`}>
      {dlq.length === 0 ? (
        <Empty>Nothing in the dead-letter queue.</Empty>
      ) : (
        <ul className="divide-y hairline">
          {dlq.map((j) => (
            <li key={j.id} className="py-2 flex items-start justify-between gap-3">
              <div className="min-w-0">
                <div className="flex items-center gap-2 text-sm">
                  <span className="font-medium">{j.job_kind}</span>
                  <StatusPill status={j.status} />
                  <span className="text-xs text-[var(--muted)]">
                    attempts: {j.attempt_count}
                  </span>
                </div>
                {j.error && (
                  <div className="text-xs text-[var(--muted)] mt-1 font-mono truncate">
                    {j.error}
                  </div>
                )}
              </div>
              <div className="flex gap-2 shrink-0">
                <button
                  type="button"
                  onClick={async () => {
                    await retryDlq(j.id);
                    onChanged();
                  }}
                  className="text-xs px-2.5 py-1 rounded-md border hairline hover:bg-[var(--surface)] transition"
                >
                  retry
                </button>
                <button
                  type="button"
                  onClick={async () => {
                    await resolveDlq(j.id);
                    onChanged();
                  }}
                  className="text-xs px-2.5 py-1 rounded-md border hairline hover:bg-[var(--surface)] transition"
                >
                  resolve
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}
    </Section>
  );
}

function StatusPill({ status }: { status: string }) {
  const colors: Record<string, string> = {
    open: "bg-amber-500/10 text-amber-300",
    resolved: "bg-emerald-500/10 text-emerald-300",
    circuit_open: "bg-red-500/10 text-red-300",
    wont_fix: "bg-[var(--surface-2)] text-[var(--muted)]",
  };
  return (
    <span className={`text-[10px] px-1.5 py-0.5 rounded ${colors[status] ?? colors.wont_fix}`}>
      {status.replace(/_/g, " ")}
    </span>
  );
}

function Section({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <section className="border hairline rounded-lg bg-[var(--surface)]/50">
      <div className="px-4 py-2 border-b hairline">
        <h2 className="text-xs uppercase tracking-wider text-[var(--muted)]">{title}</h2>
      </div>
      <div className="p-4">{children}</div>
    </section>
  );
}

function Empty({ children }: { children: React.ReactNode }) {
  return <div className="text-sm text-[var(--muted)] italic">{children}</div>;
}

function formatTime(iso: string): string {
  try {
    const d = new Date(iso);
    return d.toISOString().split("T")[1].replace("Z", "").slice(0, 12);
  } catch {
    return iso;
  }
}
