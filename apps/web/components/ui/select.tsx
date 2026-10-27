"use client";
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";

export type SelectOption = { value: string; label: ReactNode; disabled?: boolean };

/**
 * Custom single-select dropdown. The native popup is drawn by the OS and
 * cannot follow the dark theme (white panel, blue system highlight), so the
 * application draws its own: a button styled like the other form controls
 * plus a fully themed listbox.
 *
 * Keyboard contract: Enter/Space/ArrowDown opens, arrows move the active
 * option, Enter confirms, Escape closes, Home/End jump, Tab closes. The
 * active option scrolls into view. Click-outside closes.
 */
export default function Select({
  value,
  onChange,
  options,
  disabled,
  ariaLabel,
  className,
  placeholder,
}: {
  value: string;
  onChange: (value: string) => void;
  options: SelectOption[];
  disabled?: boolean;
  /** Accessible name for the closed control when no wrapping label exists. */
  ariaLabel?: string;
  className?: string;
  /** Shown on the closed control when `value` matches no option (e.g. ""). */
  placeholder?: string;
}) {
  const [open, setOpen] = useState(false);
  const selectedIndex = useMemo(() => options.findIndex((option) => option.value === value), [options, value]);
  const [activeIndex, setActiveIndex] = useState(selectedIndex >= 0 ? selectedIndex : 0);
  const rootRef = useRef<HTMLDivElement>(null);
  const listRef = useRef<HTMLUListElement>(null);

  const openMenu = useCallback(() => {
    if (disabled) return;
    setActiveIndex(selectedIndex >= 0 ? selectedIndex : 0);
    setOpen(true);
  }, [disabled, selectedIndex]);

  // Re-opening moves activation to the selected option; while open, arrows
  // move it and it must track the rendered list height for scrollIntoView.
  useEffect(() => {
    if (!open) return;
    const active = options[activeIndex];
    const node = listRef.current?.children[activeIndex] as HTMLElement | undefined;
    node?.scrollIntoView({ block: "nearest" });
    void active;
  }, [open, activeIndex, options]);

  useEffect(() => {
    if (!open) return;
    function onPointerDown(event: PointerEvent) {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    }
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") setOpen(false);
    }
    document.addEventListener("pointerdown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open]);

  function commit(index: number) {
    const option = options[index];
    if (!option || option.disabled) return;
    onChange(option.value);
    setOpen(false);
  }

  function onControlKeyDown(event: React.KeyboardEvent) {
    if (disabled) return;
    if (!open) {
      if (event.key === "ArrowDown" || event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        openMenu();
      }
      return;
    }
    switch (event.key) {
      case "ArrowDown":
        event.preventDefault();
        setActiveIndex((index) => Math.min(index + 1, options.length - 1));
        break;
      case "ArrowUp":
        event.preventDefault();
        setActiveIndex((index) => Math.max(index - 1, 0));
        break;
      case "Home":
        event.preventDefault();
        setActiveIndex(0);
        break;
      case "End":
        event.preventDefault();
        setActiveIndex(options.length - 1);
        break;
      case "Enter":
      case " ":
        event.preventDefault();
        commit(activeIndex);
        break;
      case "Tab":
        setOpen(false);
        break;
      default:
        break;
    }
  }

  const selectedLabel =
    selectedIndex >= 0 ? options[selectedIndex].label : (placeholder ?? options[0]?.label ?? "");

  return (
    <div className={`select-root${className ? ` ${className}` : ""}`} ref={rootRef} data-open={open || undefined}>
      <button
        type="button"
        className="select-control"
        role="combobox"
        aria-expanded={open}
        aria-haspopup="listbox"
        aria-label={ariaLabel}
        disabled={disabled}
        onClick={() => (open ? setOpen(false) : openMenu())}
        onKeyDown={onControlKeyDown}
      >
        <span className="select-value">{selectedLabel}</span>
        <span className="select-caret" aria-hidden="true">
          ▾
        </span>
      </button>
      {open && (
        <ul className="select-menu" role="listbox" ref={listRef} tabIndex={-1}>
          {options.map((option, index) => (
            <li
              key={option.value}
              role="option"
              aria-selected={option.value === value}
              aria-disabled={option.disabled || undefined}
              className={`select-option${index === activeIndex ? " select-option-active" : ""}${
                option.value === value ? " select-option-selected" : ""
              }${option.disabled ? " select-option-disabled" : ""}`}
              onPointerEnter={() => setActiveIndex(index)}
              onClick={() => commit(index)}
            >
              {option.label}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
