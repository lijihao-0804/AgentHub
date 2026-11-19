import type { ReactNode } from "react";
import BrandMark from "@/components/ui/brand-mark";

export default function AuthLayout({ children }: { children: ReactNode }) {
  return (
    <section className="auth-card" aria-labelledby="auth-form-title">
      <div className="auth-brand" translate="no"><BrandMark size={26} />AgentHub</div>
      {children}
    </section>
  );
}
