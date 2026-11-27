import type { Metadata, Viewport } from "next";
import "@fontsource/geist-sans/400.css";
import "@fontsource/geist-sans/500.css";
import "@fontsource/geist-sans/600.css";
import "@fontsource/geist-mono/400.css";
import "@fontsource/geist-mono/500.css";
import "./globals.css";

export const metadata: Metadata = {
  title: "TrustGate",
  description: "Find the Flagsmith flag change that broke your AI agent, and repair it with approval.",
};

export const viewport: Viewport = {
  themeColor: "#0e1a2d",
  colorScheme: "dark",
};

// Fonts are self-hosted from npm (@fontsource), so builds never fetch from Google: offline and
// Docker builds stay reliable. Each page draws its own heads-up display over its 3D scene.
export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en">
      <body>
        <main>{children}</main>
      </body>
    </html>
  );
}
