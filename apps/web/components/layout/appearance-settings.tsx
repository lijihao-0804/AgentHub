"use client";

import { useState } from "react";

import { useTheme } from "@/components/providers/theme-provider";
import BrandMark from "@/components/ui/brand-mark";
import Icon from "@/components/ui/icon";
import { useI18n } from "@/i18n/provider";
import { ACCENT_PRESETS, DEFAULT_APPEARANCE, THEME_MODES, parseAccent } from "@/lib/appearance";

export default function AppearanceSettings() {
  const { t } = useI18n();
  const { mode, accent, canPersist, setMode, setAccent, resetAppearance } = useTheme();
  const [draft, setDraft] = useState<{ accent: string; text: string } | null>(null);
  const hex = draft?.accent === accent ? draft.text : accent.toUpperCase();
  const invalid = parseAccent(hex) === null;

  function chooseColor(value: string) {
    setDraft(null);
    setAccent(value);
  }

  function editHex(text: string) {
    const parsed = parseAccent(text);
    setDraft({ accent: parsed ?? accent, text });
    if (parsed) setAccent(parsed);
  }

  return (
    <section className="panel appearance-panel" id="appearance" aria-labelledby="appearance-heading">
      <div className="appearance-heading">
        <div>
          <h2 id="appearance-heading">{t("appearance.title")}</h2>
          {!canPersist && <p role="status">{t("appearance.tabOnly")}</p>}
        </div>
        <button
          className="button button-ghost"
          type="button"
          disabled={mode === DEFAULT_APPEARANCE.mode && accent === DEFAULT_APPEARANCE.accent && !draft}
          onClick={() => { setDraft(null); resetAppearance(); }}
        >
          {t("appearance.reset")}
        </button>
      </div>

      <div className="appearance-layout">
        <div className="appearance-controls">
          <fieldset>
            <legend>{t("appearance.mode")}</legend>
            <div className="appearance-modes">
              {THEME_MODES.map((option) => (
                <label key={option}>
                  <input
                    className="sr-only" type="radio" name="appearance-mode"
                    value={option} checked={mode === option} onChange={() => setMode(option)}
                  />
                  <span>{t(`appearance.${option}`)}</span>
                </label>
              ))}
            </div>
          </fieldset>

          <fieldset>
            <legend>{t("appearance.accent")}</legend>
            <div className="appearance-presets">
              {ACCENT_PRESETS.map((preset) => (
                <button
                  type="button" className="appearance-preset" key={preset.name}
                  aria-pressed={accent === preset.color} onClick={() => chooseColor(preset.color)}
                >
                  <span aria-hidden="true" style={{ backgroundColor: preset.color }} />
                  {t(`appearance.${preset.name}`)}
                </button>
              ))}
            </div>
            <label className="appearance-custom-label" htmlFor="appearance-hex">{t("appearance.custom")}</label>
            <div className="appearance-color-editor">
              <input
                type="color" value={accent} aria-label={t("appearance.picker")}
                onChange={(event) => chooseColor(event.currentTarget.value)}
              />
              <input
                id="appearance-hex" value={hex} dir="ltr" spellCheck={false} autoComplete="off"
                aria-invalid={invalid} aria-describedby={invalid ? "appearance-hex-error" : undefined}
                onChange={(event) => editHex(event.currentTarget.value)}
                onBlur={() => { if (!invalid) setDraft(null); }}
              />
            </div>
            {invalid && <p id="appearance-hex-error" className="appearance-error">{t("appearance.invalid")}</p>}
          </fieldset>
        </div>

        <figure className="appearance-preview">
          <figcaption>{t("appearance.preview")}</figcaption>
          <div className="appearance-preview-shell" aria-hidden="true">
            <div className="appearance-preview-rail">
              <BrandMark size={25} />
              <Icon name="workspace" size={16} />
              <span><Icon name="runs" size={16} /></span>
            </div>
            <div className="appearance-preview-body">
              <strong className="appearance-preview-brand">AgentHub</strong>
              <div className="appearance-preview-content">
                <strong>{t("nav.runs")}</strong>
                <div className="appearance-preview-row">
                  <Icon name="runs" size={18} />
                  <span>{t("status.SUCCEEDED")}</span>
                </div>
                <span className="appearance-preview-action">{t("appearance.openRun")}</span>
              </div>
            </div>
          </div>
        </figure>
      </div>
    </section>
  );
}
