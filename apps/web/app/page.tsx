import Link from "next/link";
import { listMeetings, type MeetingListItem } from "@/lib/api";

export const revalidate = 0;

const STATUS_COLOR: Record<string, string> = {
  requested: "bg-[var(--surface-2)] text-[var(--muted)]",
  joining: "bg-amber-500/10 text-amber-300",
  in_call: "bg-emerald-500/10 text-emerald-400",
  processing: "bg-indigo-500/10 text-indigo-300",
  done: "bg-indigo-500/10 text-indigo-300",
  failed: "bg-red-500/10 text-red-400",
};

function StatusBadge({ status }: { status: string }) {
  const cls = STATUS_COLOR[status] ?? "bg-[var(--surface-2)] text-[var(--muted)]";
  const label = status === "in_call" ? "live" : status;
  const dot = status === "in_call" ? "bg-emerald-400 animate-pulse" : "bg-current opacity-60";
  return (
    <span className={`inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-xs ${cls}`}>
      <span className={`w-1.5 h-1.5 rounded-full ${dot}`} />
      {label}
    </span>
  );
}

function TimeAgo({ iso }: { iso: string }) {
  const d = new Date(iso);
  const diff = (Date.now() - d.getTime()) / 1000;
  let text: string;
  if (diff < 60) text = `${Math.floor(diff)}s ago`;
  else if (diff < 3600) text = `${Math.floor(diff / 60)}m ago`;
  else if (diff < 86400) text = `${Math.floor(diff / 3600)}h ago`;
  else text = d.toLocaleDateString();
  return <span title={d.toISOString()}>{text}</span>;
}

export default async function InboxPage() {
  let meetings: MeetingListItem[] = [];
  let error: string | null = null;
  try {
    meetings = await listMeetings();
  } catch (e) {
    error = e instanceof Error ? e.message : "failed to load";
  }

  return (
    <div>
      <div className="flex items-end justify-between mb-6">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Inbox</h1>
          <p className="text-sm text-[var(--muted)] mt-1">
            Every customer call. Live-transcribed, signal-extracted, event-sourced.
          </p>
        </div>
        {meetings.length > 0 && (
          <div className="text-sm text-[var(--muted)]">
            {meetings.length} meeting{meetings.length === 1 ? "" : "s"}
          </div>
        )}
      </div>

      {error && (
        <div className="rounded-md border border-red-500/30 bg-red-500/5 p-4 text-sm text-red-300">
          {error}
          <div className="mt-1 text-[var(--muted)]">
            Is the api running on {process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000"}?
          </div>
        </div>
      )}

      {!error && meetings.length === 0 && (
        <div className="border hairline rounded-lg p-10 text-center">
          <div className="text-lg font-medium mb-2">No meetings yet</div>
          <p className="text-sm text-[var(--muted)] mb-4">
            Create your first bot — paste a Zoom, Google Meet, or Teams URL.
          </p>
          <Link
            href="/meetings/new"
            className="inline-block px-4 py-2 rounded-md bg-[var(--accent)] text-white hover:brightness-110 transition"
          >
            New meeting
          </Link>
        </div>
      )}

      {meetings.length > 0 && (
        <ul className="space-y-2">
          {meetings.map((m) => (
            <li key={m.id}>
              <Link
                href={`/meetings/${m.id}`}
                className="block border hairline rounded-lg bg-[var(--surface)]/50 hover:bg-[var(--surface)] transition p-4"
              >
                <div className="flex items-start justify-between gap-4">
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2 mb-1">
                      <div className="font-medium truncate">
                        {m.title ?? "Untitled meeting"}
                      </div>
                      <StatusBadge status={m.status} />
                      {m.has_high_severity && (
                        <span className="text-xs px-1.5 py-0.5 rounded bg-red-500/10 text-red-400">
                          high risk
                        </span>
                      )}
                    </div>
                    <div className="text-sm text-[var(--muted)] truncate">
                      {m.top_insight_title ? (
                        <>
                          <span className="text-[var(--foreground)]/70">
                            {formatType(m.top_insight_type)}:
                          </span>{" "}
                          {m.top_insight_title}
                        </>
                      ) : (
                        <span className="italic">no insights yet</span>
                      )}
                    </div>
                  </div>
                  <div className="text-right text-xs text-[var(--muted)] shrink-0">
                    <div>
                      <TimeAgo iso={m.created_at} />
                    </div>
                    <div className="mt-0.5">
                      {m.insight_count} insight{m.insight_count === 1 ? "" : "s"}
                    </div>
                  </div>
                </div>
              </Link>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function formatType(t: string | null): string {
  if (!t) return "";
  return t.replace(/_/g, " ");
}
