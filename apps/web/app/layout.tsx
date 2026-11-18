import type { Metadata } from "next";
import AppShell from "@/components/layout/app-shell";
import { appearanceBootstrap } from "@/lib/appearance";
import "./globals.css";

export const metadata: Metadata = {
  title: "AgentHub",
  description: "Enterprise Agent Runtime & Control Plane",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en-US" suppressHydrationWarning>
      <head><script dangerouslySetInnerHTML={{ __html: appearanceBootstrap }} /></head>
      <body>
        <AppShell>{children}</AppShell>
      </body>
    </html>
  );
}
