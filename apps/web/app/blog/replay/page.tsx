import type { Metadata } from "next";
import Link from "next/link";

export const metadata: Metadata = {
  title: "Replaying a meeting from the event log",
  description:
    "How event sourcing, idempotent webhook ingestion, and a prompt hash cache combine to make meeting bot state deterministically reproducible.",
};

export default function ReplayPost() {
  return (
    <article className="max-w-3xl mx-auto prose-invert">
      <Link
        href="/"
        className="text-sm text-[var(--muted)] hover:text-[var(--foreground)]"
      >
        ← Home
      </Link>

      <header className="mt-4 mb-10">
        <h1 className="text-3xl font-semibold tracking-tight">
          Replaying a meeting from the event log
        </h1>
        <p className="text-sm text-[var(--muted)] mt-3">
          April 23, 2026 · 5 minute read
        </p>
      </header>

      <Body>
        <P>
          You run a customer call, the bot captures it, your dashboard says
          there were three objections and a commitment. A week later your
          customer asks why a fourth objection they remember raising doesn't
          show up. How do you find out what actually happened?
        </P>

        <P>
          If your system mutates state as events arrive, you can't. The
          transcript in your database is whatever it is today. The signals are
          whatever the classifier felt like returning that afternoon. If
          Anthropic had a bad minute and fell back to default output, that
          output is what you show the customer forever.
        </P>

        <P>
          Recall.ai retries a failed webhook sixty times at one second
          intervals. For every real event there are anywhere from one to sixty
          delivery attempts hitting your server. Without care, you end up with
          duplicate transcript rows, duplicate insights, and a fuzzy picture of
          what the bot actually saw.
        </P>

        <P>
          Event sourcing fixes this. It also turns debugging from archaeology
          into a single button press.
        </P>

        <H2>What we store</H2>

        <P>
          Every webhook from Recall lands in one Postgres table.
        </P>

        <Code>{`class MeetingEvent(Base):
    __tablename__ = "meeting_events"
    id: Mapped[int]              # bigserial
    meeting_id: Mapped[Optional[UUID]]
    source: Mapped[str]          # "recall" or "internal"
    event_type: Mapped[str]      # transcript.data, bot.status_change, ...
    event_timestamp: Mapped[datetime]
    received_at: Mapped[datetime]
    payload_json: Mapped[dict]
    dedupe_key: Mapped[str]      # UNIQUE
    signature_valid: Mapped[bool]`}</Code>

        <P>
          <Inline>dedupe_key</Inline> is either Recall's Svix message id or a
          content hash over (bot_id, event_type, event_timestamp, canonical
          payload). The column has a UNIQUE constraint, so a second delivery
          of the same event loses the race and the row count stays at one.
        </P>

        <P>
          Every other table in the system is a projection of this one.
          Transcript utterances come from <Inline>transcript.data</Inline>{" "}
          events. Meeting status comes from <Inline>bot.status_change</Inline>{" "}
          events. Insights come from classifier calls fed by the utterance
          projections. Nothing derived is truth; all of it can be rebuilt.
        </P>

        <H2>The replay function</H2>

        <P>Here's the whole thing.</P>

        <Code>{`async def replay_meeting(meeting_id: UUID) -> dict:
    with SessionLocal() as s:
        events = list(s.execute(
            select(MeetingEvent)
            .where(MeetingEvent.meeting_id == meeting_id)
            .order_by(MeetingEvent.event_timestamp, MeetingEvent.id)
        ).scalars().all())
        if not events:
            return {"status": "empty", "events": 0}
        _wipe_derived(s, meeting_id)
        _reset_meeting_state(s, meeting_id)
        s.commit()

    handlers = _build_handlers_for_replay()
    for event in events:
        await _run_event(handlers, event.id)

    await handlers["__batcher__"].flush_all()
    return {"status": "replayed", "events": len(events)}`}</Code>

        <P>
          Three steps. Load the events in event time order. Wipe the derived
          tables for this meeting. Run every event through the same handlers
          the live path uses.
        </P>

        <P>
          If the live handlers produce the right state for new events, they
          produce the right state for replayed events. No separate code path,
          no drift between live and replayed behavior.
        </P>

        <H2>The two things that make this work</H2>

        <P>
          One, the dedupe. The UNIQUE key guarantees you never have duplicate
          events in the log. Replay iterates a clean, ordered, deduplicated
          stream.
        </P>

        <P>Two, the LLM cache.</P>

        <P>
          Classification is stochastic. Feed the same prompt to Claude Haiku
          twice and in principle you get two different results. In practice,
          with temperature at zero and structured output forced, the outputs
          are close but not bit identical. That would be enough to break
          replay reliability.
        </P>

        <P>Our cache sidesteps the problem.</P>

        <Code>{`def compute_cache_key(model, prompt_parts, temperature, tool_schema):
    material = {
        "model": model,
        "prompt": prompt_parts,
        "temperature": round(float(temperature), 4),
        "tool_schema": tool_schema or {},
    }
    canon = json.dumps(material, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canon.encode()).hexdigest()`}</Code>

        <P>
          Every LLM call goes through{" "}
          <Inline>get_or_call(session, cache_key, caller)</Inline>. First time,
          we hit Anthropic and store the response keyed on the prompt hash.
          Every time after that, same prompt equals cache hit equals the exact
          same response. Replay hits the cache on every call.
        </P>

        <P>
          Practical consequence: a replayed meeting costs zero dollars in
          Anthropic tokens. The Mission Control page has a Replay button that
          rebuilds a meeting's entire derived state in about two seconds for a
          five minute call.
        </P>

        <H2>What it lets you do</H2>

        <P>Three things that justify the whole pattern.</P>

        <P>
          First, debug customer issues deterministically. A customer says a
          signal is missing. You pull up the meeting, click Replay, and watch
          the exact same state emerge. If it's wrong both times, the bug is in
          the code. If it's right now but wasn't before, something about the
          code changed between the first run and now.
        </P>

        <P>
          Second, safely evolve the classifier. You change the prompt, the
          signal taxonomy, anything, and you can rerun old meetings through
          the new code. The event log doesn't care what your business logic
          looks like today.
        </P>

        <P>
          Third, build trust in AI output. Every insight in the UI has a{" "}
          <Inline>go to transcript</Inline> link that jumps to the exact
          utterance that produced it. With deterministic replay, that link is
          stable forever. Nothing in our system silently rewrites history.
        </P>

        <H2>What we gave up</H2>

        <P>
          The event log grows unbounded, which will eventually need a
          retention policy. Derived tables double the storage. The cache is
          large, though cheap because it is mostly small JSON blobs.
        </P>

        <P>
          On the other side of the ledger: when something goes wrong with a
          customer meeting, we can reproduce it bit for bit on the first try.
          That tradeoff earns itself back the first time someone asks why
          their meeting looks the way it does.
        </P>

        <H2>Try it</H2>

        <P>
          The full code is open source at{" "}
          <A href="https://github.com/krish1236/recall-ai-project">
            github.com/krish1236/recall-ai-project
          </A>
          . A live demo runs at{" "}
          <A href="https://recall-ai-project.vercel.app">
            recall-ai-project.vercel.app
          </A>
          . Click into a meeting, open Mission Control, and press Replay. The
          whole derived state will wipe and rebuild in front of you.
        </P>
      </Body>
    </article>
  );
}

function Body({ children }: { children: React.ReactNode }) {
  return <div className="space-y-5 text-[var(--foreground)]/90 leading-relaxed">{children}</div>;
}

function P({ children }: { children: React.ReactNode }) {
  return <p className="text-base">{children}</p>;
}

function H2({ children }: { children: React.ReactNode }) {
  return (
    <h2 className="text-xl font-semibold tracking-tight mt-10 mb-3">
      {children}
    </h2>
  );
}

function Code({ children }: { children: string }) {
  return (
    <pre className="border hairline rounded-lg bg-[var(--surface)]/80 text-sm overflow-x-auto p-4 font-mono leading-6 my-2">
      <code>{children}</code>
    </pre>
  );
}

function Inline({ children }: { children: React.ReactNode }) {
  return (
    <code className="text-[0.92em] font-mono bg-[var(--surface-2)] rounded px-1.5 py-0.5">
      {children}
    </code>
  );
}

function A({ href, children }: { href: string; children: React.ReactNode }) {
  return (
    <a
      href={href}
      target="_blank"
      rel="noreferrer"
      className="underline underline-offset-4 decoration-[var(--muted)] hover:text-[var(--accent)]"
    >
      {children}
    </a>
  );
}
