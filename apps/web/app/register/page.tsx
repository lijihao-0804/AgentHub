"use client";

import Link from "next/link";
import { useState, type FormEvent } from "react";

import { useFrontendSession } from "@/components/providers/session-provider";
import { ApiError, toApiError } from "@/lib/api/client";
import { InlineError } from "@/components/ui/states";
import { useI18n } from "@/i18n/provider";

/** Backend contract: password is 8–128 characters. */
const MIN_PASSWORD_LENGTH = 8;

export default function RegisterPage() {
  const { t } = useI18n();
  const { signUp } = useFrontendSession();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<ApiError | null>(null);
  // Held apart from `error`: these rules are checked here, before anything is
  // sent, so there is no error code behind them and none should be shown.
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
    if (password.length < MIN_PASSWORD_LENGTH) {
      setInvalid(t("auth.passwordTooShort"));
      return;
    }
    setSubmitting(true);
    try {
      await signUp({ email: email.trim(), password });
      setPassword("");
    } catch (caught) {
      const apiError = toApiError(caught, t("auth.registerFailed"));
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
      <h1>{t("auth.registerTitle")}</h1>
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
            autoComplete="new-password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
          />
          <span className="state-hint">{t("auth.passwordHint")}</span>
        </label>
        {invalid && (
          <p className="inline-error" role="alert">
            {invalid}
          </p>
        )}
        <InlineError error={error} fallback={t("auth.registerFailed")} />
        <button type="submit" className="button button-primary" disabled={submitting}>
          {submitting ? t("common.loading") : t("auth.createAccount")}
        </button>
      </form>
      <p className="auth-alt">
        {t("auth.haveAccount")} <Link href="/login">{t("auth.login")}</Link>
      </p>
    </div>
  );
}
