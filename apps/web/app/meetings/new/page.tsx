"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { createMeeting } from "@/lib/api";

export default function NewMeetingPage() {
  const router = useRouter();
  const [url, setUrl] = useState("");
  const [title, setTitle] = useState("");
  const [meetingType, setMeetingType] = useState("");
  const [ownerName, setOwnerName] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      const res = await createMeeting({
        meeting_url: url.trim(),
        title: title.trim() || undefined,
        meeting_type: meetingType.trim() || undefined,
        owner_name: ownerName.trim() || undefined,
      });
      router.push(`/meetings/${res.meeting_id}`);
    } catch (e) {
      setError(e instanceof Error ? e.message : "failed");
      setSubmitting(false);
    }
  }

  return (
    <div className="max-w-xl">
      <Link href="/" className="text-sm text-[var(--muted)] hover:text-[var(--foreground)]">
        ← Inbox
      </Link>
      <h1 className="text-2xl font-semibold tracking-tight mt-2">New meeting</h1>
      <p className="text-sm text-[var(--muted)] mt-1 mb-8">
        Paste a live Zoom, Google Meet, or Teams URL — a Recall bot joins and
        transcript streams start flowing.
      </p>

      <form onSubmit={onSubmit} className="space-y-5">
        <Field label="Meeting URL" required>
          <input
            type="url"
            required
            placeholder="https://meet.google.com/abc-defg-hij"
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            className={inputCls}
          />
        </Field>
        <div className="grid grid-cols-2 gap-5">
          <Field label="Title">
            <input
              type="text"
              placeholder="Acme Q2 discovery"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              className={inputCls}
            />
          </Field>
          <Field label="Type">
            <input
              type="text"
              placeholder="discovery, renewal, onboarding…"
              value={meetingType}
              onChange={(e) => setMeetingType(e.target.value)}
              className={inputCls}
            />
          </Field>
        </div>
        <Field label="Owner">
          <input
            type="text"
            placeholder="rep@company.com"
            value={ownerName}
            onChange={(e) => setOwnerName(e.target.value)}
            className={inputCls}
          />
        </Field>

        {error && (
          <div className="text-sm text-red-400 bg-red-500/10 border border-red-500/30 rounded-md p-3">
            {error}
          </div>
        )}

        <div className="flex items-center gap-3 pt-2">
          <button
            type="submit"
            disabled={submitting || !url}
            className="px-4 py-2 rounded-md bg-[var(--accent)] text-white hover:brightness-110 transition disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {submitting ? "Dispatching bot…" : "Send bot"}
          </button>
          <Link
            href="/"
            className="text-sm text-[var(--muted)] hover:text-[var(--foreground)] transition"
          >
            Cancel
          </Link>
        </div>
      </form>
    </div>
  );
}

function Field({
  label,
  required,
  children,
}: {
  label: string;
  required?: boolean;
  children: React.ReactNode;
}) {
  return (
    <label className="block">
      <span className="block text-sm text-[var(--muted)] mb-1.5">
        {label}
        {required && <span className="text-[var(--accent)] ml-1">*</span>}
      </span>
      {children}
    </label>
  );
}

const inputCls =
  "w-full rounded-md bg-[var(--surface)] border hairline px-3 py-2 outline-none focus:border-[var(--accent)] transition text-sm";
