/**
 * Design system primitives: buttons, cards, badges, inputs, dialogs,
 * loading/empty/error states, alerts and stats.
 */
import React, { useEffect } from "react";
import { AlertTriangle, Inbox, Loader2, X } from "lucide-react";
import { cn } from "../lib/cn";

/* ---------------------------------- Button --------------------------------- */

type ButtonVariant = "primary" | "secondary" | "ghost" | "danger";

const buttonStyles: Record<ButtonVariant, string> = {
  primary:
    "bg-indigo-500 text-white hover:bg-indigo-400 focus-visible:ring-indigo-400 disabled:bg-indigo-500/40",
  secondary:
    "bg-terminal-surface-2 text-terminal-text border border-terminal-border hover:border-indigo-400/60 hover:text-white disabled:opacity-50",
  ghost: "text-terminal-muted hover:text-white hover:bg-white/5 disabled:opacity-50",
  danger:
    "bg-red-500/15 text-red-300 border border-red-500/40 hover:bg-red-500/25 disabled:opacity-50",
};

export function Button({
  variant = "secondary",
  className,
  disabled,
  children,
  ...rest
}: React.ButtonHTMLAttributes<HTMLButtonElement> & { variant?: ButtonVariant }) {
  return (
    <button
      className={cn(
        "inline-flex items-center justify-center gap-2 rounded-md px-3.5 py-2 text-sm font-medium",
        "transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-offset-2 focus-visible:ring-offset-terminal-bg",
        "disabled:cursor-not-allowed",
        buttonStyles[variant],
        className
      )}
      disabled={disabled}
      {...rest}
    >
      {children}
    </button>
  );
}

/* ----------------------------------- Card ---------------------------------- */

export function Card({
  className,
  children,
}: {
  className?: string;
  children: React.ReactNode;
}) {
  return (
    <div
      className={cn(
        "rounded-xl border border-terminal-border bg-terminal-surface",
        className
      )}
    >
      {children}
    </div>
  );
}

export function CardHeader({
  title,
  subtitle,
  action,
}: {
  title: string;
  subtitle?: string;
  action?: React.ReactNode;
}) {
  return (
    <div className="flex items-start justify-between gap-4 border-b border-terminal-border px-5 py-4">
      <div>
        <h3 className="text-sm font-semibold text-white">{title}</h3>
        {subtitle && <p className="mt-0.5 text-xs text-terminal-muted">{subtitle}</p>}
      </div>
      {action}
    </div>
  );
}

/* ----------------------------------- Badge --------------------------------- */

type BadgeTone = "up" | "down" | "mixed" | "info" | "neutral";

const badgeTones: Record<BadgeTone, string> = {
  up: "bg-emerald-500/10 text-emerald-300 border-emerald-500/30",
  down: "bg-red-500/10 text-red-300 border-red-500/30",
  mixed: "bg-amber-500/10 text-amber-300 border-amber-500/30",
  info: "bg-indigo-500/10 text-indigo-300 border-indigo-500/30",
  neutral: "bg-white/5 text-terminal-muted border-white/10",
};

export function Badge({
  tone = "neutral",
  className,
  children,
}: {
  tone?: BadgeTone;
  className?: string;
  children: React.ReactNode;
}) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] font-medium capitalize",
        badgeTones[tone],
        className
      )}
    >
      {children}
    </span>
  );
}

export function sentimentTone(s: string): BadgeTone {
  const v = s.toLowerCase();
  if (v === "positive") return "up";
  if (v === "negative") return "down";
  return "mixed";
}

export function impactTone(i: string): BadgeTone {
  const v = i.toLowerCase();
  if (v === "high") return "down";
  if (v === "medium") return "mixed";
  return "neutral";
}

/* ------------------------------ Inputs & forms ----------------------------- */

export function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <label className="block">
      <span className="mb-1.5 block text-xs font-medium uppercase tracking-wide text-terminal-muted">
        {label}
      </span>
      {children}
      {hint && <span className="mt-1 block text-xs text-terminal-faint">{hint}</span>}
    </label>
  );
}

const inputBase =
  "w-full rounded-md border border-terminal-border bg-terminal-bg px-3 py-2 text-sm text-terminal-text placeholder:text-terminal-faint focus:border-indigo-400 focus:outline-none disabled:opacity-50";

export function TextInput(props: React.InputHTMLAttributes<HTMLInputElement>) {
  return <input {...props} className={cn(inputBase, props.className)} />;
}

export function Textarea(props: React.TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return <textarea {...props} className={cn(inputBase, "min-h-[96px] resize-y", props.className)} />;
}

export function SelectInput(props: React.SelectHTMLAttributes<HTMLSelectElement>) {
  return <select {...props} className={cn(inputBase, props.className)} />;
}

export function Toggle({
  label,
  hint,
  checked,
  onChange,
}: {
  label: string;
  hint?: string;
  checked: boolean;
  onChange: (v: boolean) => void;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      onClick={() => onChange(!checked)}
      className="flex w-full cursor-pointer items-center justify-between gap-3 rounded-md border border-terminal-border bg-terminal-bg px-3 py-2.5 text-left hover:border-indigo-400/50"
    >
      <span>
        <span className="block text-sm font-medium text-terminal-text">{label}</span>
        {hint && <span className="mt-0.5 block text-xs text-terminal-faint">{hint}</span>}
      </span>
      <span
        className={cn(
          "relative h-6 w-11 shrink-0 rounded-full transition-colors",
          checked ? "bg-indigo-500" : "bg-white/10"
        )}
      >
        <span
          className={cn(
            "absolute top-0.5 h-5 w-5 rounded-full bg-white transition-all",
            checked ? "left-[22px]" : "left-0.5"
          )}
        />
      </span>
    </button>
  );
}

/* ------------------------------- Loading etc ------------------------------- */

export function Spinner({ className }: { className?: string }) {
  return <Loader2 className={cn("h-5 w-5 animate-spin text-indigo-400", className)} />;
}

export function LoadingState({ message = "Loading…" }: { message?: string }) {
  return (
    <div className="flex flex-col items-center justify-center gap-3 py-16 text-terminal-muted">
      <Spinner className="h-8 w-8" />
      <p className="text-sm">{message}</p>
    </div>
  );
}

export function EmptyState({
  title,
  message,
  action,
  icon,
}: {
  title: string;
  message: string;
  action?: React.ReactNode;
  icon?: React.ReactNode;
}) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 px-6 py-16 text-center">
      <div className="mb-2 flex h-12 w-12 items-center justify-center rounded-full bg-white/5 text-terminal-faint">
        {icon ?? <Inbox className="h-6 w-6" />}
      </div>
      <h3 className="text-sm font-semibold text-white">{title}</h3>
      <p className="max-w-md text-sm text-terminal-muted">{message}</p>
      {action && <div className="mt-3">{action}</div>}
    </div>
  );
}

export function ErrorState({
  message,
  onRetry,
}: {
  message: string;
  onRetry?: () => void;
}) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 px-6 py-16 text-center">
      <div className="mb-2 flex h-12 w-12 items-center justify-center rounded-full bg-red-500/10 text-red-300">
        <AlertTriangle className="h-6 w-6" />
      </div>
      <h3 className="text-sm font-semibold text-white">Something went wrong</h3>
      <p className="max-w-md text-sm text-terminal-muted">{message}</p>
      {onRetry && (
        <div className="mt-3">
          <Button onClick={onRetry}>Try again</Button>
        </div>
      )}
    </div>
  );
}

export function Alert({
  tone = "mixed",
  children,
}: {
  tone?: BadgeTone;
  children: React.ReactNode;
}) {
  return (
    <div
      className={cn(
        "flex items-start gap-2.5 rounded-lg border px-3.5 py-3 text-sm",
        badgeTones[tone]
      )}
      role="alert"
    >
      <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
      <div className="text-terminal-text/90">{children}</div>
    </div>
  );
}

/* ---------------------------------- Dialog --------------------------------- */

export function Dialog({
  open,
  onClose,
  title,
  children,
  wide,
}: {
  open: boolean;
  onClose: () => void;
  title: React.ReactNode;
  children: React.ReactNode;
  wide?: boolean;
}) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    document.addEventListener("keydown", onKey);
    document.body.style.overflow = "hidden";
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = "";
    };
  }, [open, onClose]);

  if (!open) return null;
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4"
      onClick={onClose}
      role="dialog"
      aria-modal="true"
    >
      <div
        className={cn(
          "animate-fade-slide-in max-h-[90vh] w-full overflow-y-auto rounded-xl border border-terminal-border bg-terminal-surface shadow-2xl",
          wide ? "max-w-3xl" : "max-w-lg"
        )}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="sticky top-0 flex items-center justify-between gap-4 border-b border-terminal-border bg-terminal-surface px-5 py-4">
          <div className="min-w-0 text-sm font-semibold text-white">{title}</div>
          <button
            onClick={onClose}
            className="rounded-md p-1 text-terminal-muted hover:bg-white/5 hover:text-white"
            aria-label="Close"
          >
            <X className="h-5 w-5" />
          </button>
        </div>
        <div className="px-5 py-4">{children}</div>
      </div>
    </div>
  );
}

/* -------------------------------- ProgressBar ------------------------------- */

export function ProgressBar({
  value,
  max,
  className,
}: {
  value: number;
  max: number;
  className?: string;
}) {
  const pct = max > 0 ? Math.min(100, Math.round((value / max) * 100)) : 0;
  return (
    <div className={cn("h-2 w-full overflow-hidden rounded-full bg-white/5", className)}>
      <div
        className="h-full rounded-full bg-indigo-500 transition-all duration-500"
        style={{ width: `${pct}%` }}
      />
    </div>
  );
}
