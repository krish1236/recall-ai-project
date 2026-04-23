import type { Metadata } from "next";
import Link from "next/link";
import { Geist, Geist_Mono } from "next/font/google";
import "./globals.css";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "Recall Mission Control",
  description:
    "Customer-call intelligence inbox with a webhook-aware ops layer.",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html
      lang="en"
      className={`${geistSans.variable} ${geistMono.variable} h-full antialiased`}
    >
      <body className="min-h-full flex flex-col">
        <header className="border-b hairline bg-[var(--surface)]/60 backdrop-blur-sm sticky top-0 z-10">
          <div className="max-w-6xl mx-auto px-6 py-3 flex items-center justify-between">
            <Link href="/" className="flex items-center gap-2 group">
              <span className="inline-block w-2.5 h-2.5 rounded-full bg-[var(--accent)] group-hover:shadow-[0_0_12px_var(--accent)] transition" />
              <span className="font-semibold tracking-tight">Recall Mission Control</span>
            </Link>
            <nav className="flex items-center gap-4 text-sm text-[var(--muted)]">
              <Link href="/" className="hover:text-[var(--foreground)] transition">
                Inbox
              </Link>
              <Link
                href="/meetings/new"
                className="px-3 py-1.5 rounded-md bg-[var(--accent)] text-white hover:brightness-110 transition"
              >
                New meeting
              </Link>
            </nav>
          </div>
        </header>
        <main className="flex-1 max-w-6xl w-full mx-auto px-6 py-8">
          {children}
        </main>
        <footer className="border-t hairline text-xs text-[var(--muted)] py-4">
          <div className="max-w-6xl mx-auto px-6 flex justify-between">
            <span>Built on Recall.ai · live transcripts · event-sourced state</span>
            <a
              href="https://github.com/krish1236/recall-ai-project"
              className="hover:text-[var(--foreground)]"
              target="_blank"
              rel="noreferrer"
            >
              github.com/krish1236/recall-ai-project
            </a>
          </div>
        </footer>
      </body>
    </html>
  );
}
