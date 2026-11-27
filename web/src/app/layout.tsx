import type { Metadata, Viewport } from "next";
import Link from "next/link";
import "@fontsource/ibm-plex-sans/400.css";
import "@fontsource/ibm-plex-sans/500.css";
import "@fontsource/ibm-plex-sans/600.css";
import "@fontsource/jetbrains-mono/400.css";
import "@fontsource/jetbrains-mono/500.css";
import "@fontsource/jetbrains-mono/600.css";
import "./globals.css";
import { Icon } from "@/components/Icon";

export const metadata: Metadata = {
  title: "TrustGate",
  description: "Find the Flagsmith flag change that broke your AI agent, and repair it with approval.",
};

export const viewport: Viewport = {
  themeColor: "#0b1120",
  colorScheme: "dark",
};

// Fonts are self-hosted from npm (@fontsource), so builds never fetch from Google: offline and
// Docker builds stay reliable.
export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en">
      <body>
        <nav className="topbar" aria-label="TrustGate">
          <Link href="/" className="brand">
            <span className="brand-mark">
              <Icon name="shield" size={16} />
            </span>
            TrustGate
          </Link>
          <span className="topbar-tagline">Approval-gated repairs for Flagsmith-controlled AI agents</span>
        </nav>
        <main className="container">{children}</main>
      </body>
    </html>
  );
}
