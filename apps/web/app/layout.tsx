import type { Metadata } from "next";
import AppShell from "../components/app-shell";
import "./globals.css";

export const metadata: Metadata = {
  title: "AgentHub",
  description: "Enterprise Agent Runtime & Control Plane",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en-US">
      <body>
        <AppShell>{children}</AppShell>
      </body>
    </html>
  );
}
