import type { Metadata } from "next";
import Link from "next/link";
import "./globals.css";

export const metadata: Metadata = {
  title: "TrustGate",
  description: "Find the Flagsmith flag change that broke your AI agent, and repair it with approval.",
};

// System fonts only: no build-time font download, so offline and Docker builds stay reliable.
export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en">
      <body>
        <nav className="topbar">
          <Link href="/" className="brand">
            TrustGate
          </Link>
          <span className="muted small">Approval-gated repairs for Flagsmith-controlled AI agents</span>
        </nav>
        <main className="container">{children}</main>
      </body>
    </html>
  );
}
