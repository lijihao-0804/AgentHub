"use client";

import Link from "next/link";
import { useState, type FormEvent } from "react";

import { useFrontendSession } from "@/components/providers/session-provider";
import { ApiError, toApiError } from "@/lib/api/client";
import { InlineError } from "@/components/ui/states";
import { useI18n } from "@/i18n/provider";

export default function LoginPage() {
  const { t } = useI18n();
  const { signIn } = useFrontendSession();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  // Held apart from `error`: an empty field is caught here, before anything is
  // sent, so there is no error code behind it and none should be shown.
  const [invalid, setInvalid] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    setInvalid(null);
    if (!email.trim() || !password) {
      setInvalid(t("auth.requiredFields"));
      return;
    }
    setSubmitting(true);
    try {
      await signIn({ email: email.trim(), password });
      // The shell redirects to /dashboard once the session is adopted.
      setPassword("");
    } catch (caught) {
      const apiError = toApiError(caught, t("auth.loginFailed"));
      setError(apiError);
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
          <span className="password-field">
            <input
              type={showPassword ? "text" : "password"}
              autoComplete="current-password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
            />
            <button
              type="button"
              className="password-toggle"
              onClick={() => setShowPassword((value) => !value)}
              aria-pressed={showPassword}
              aria-label={showPassword ? t("auth.hidePassword") : t("auth.showPassword")}
            >
              {showPassword ? t("auth.hidePassword") : t("auth.showPassword")}
            </button>
          </span>
        </label>
        {invalid && (
          <p className="inline-error" role="alert">
            {invalid}
          </p>
        )}
        <InlineError error={error} fallback={t("auth.loginFailed")} />
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
