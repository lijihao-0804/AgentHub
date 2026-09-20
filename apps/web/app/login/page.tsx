"use client";

import Link from "next/link";
import { useState, type FormEvent } from "react";

import { useFrontendSession } from "../../components/session-provider";
import { toApiError } from "../../lib/api-client";
import { useI18n } from "../../i18n/provider";

export default function LoginPage() {
  const { t } = useI18n();
  const { signIn } = useFrontendSession();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<{ code: string; message: string } | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    if (!email.trim() || !password) {
      setError({ code: "VALIDATION_ERROR", message: t("auth.requiredFields") });
      return;
    }
    setSubmitting(true);
    try {
      await signIn({ email: email.trim(), password });
      // The shell redirects to /dashboard once the session is adopted.
      setPassword("");
    } catch (caught) {
      const apiError = toApiError(caught, t("auth.loginFailed"));
      setError({ code: apiError.code, message: apiError.message || t("auth.loginFailed") });
      setPassword("");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="auth-card">
      <div className="auth-brand">
        <span className="brand-name">AgentHub</span>
        <span className="brand-subtitle">{t("brand.subtitle")}</span>
      </div>
      <h1>{t("auth.loginTitle")}</h1>
      <form onSubmit={handleSubmit} noValidate>
        <label>
          {t("auth.email")}
          <input
            type="email"
            autoComplete="username"
            value={email}
            onChange={(event) => setEmail(event.target.value)}
            spellCheck={false}
            autoFocus
          />
        </label>
        <label>
          {t("auth.password")}
          <input
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
          />
        </label>
        {error && (
          <p className="session-error" role="alert">
            <code>{error.code}</code> {error.message}
          </p>
        )}
        <button type="submit" className="button button-primary" disabled={submitting}>
          {submitting ? t("common.loading") : t("auth.login")}
        </button>
      </form>
      <p className="auth-alt">
        {t("auth.noAccount")} <Link href="/register">{t("auth.register")}</Link>
      </p>
    </div>
  );
}
