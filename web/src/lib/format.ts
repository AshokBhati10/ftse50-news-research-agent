/** Formatting helpers for market data and timestamps. */

export function formatMarketCap(value: number | null | undefined): string {
  if (value == null || !isFinite(value) || value <= 0) return "—";
  const abs = Math.abs(value);
  if (abs >= 1e12) return `£${(value / 1e12).toFixed(2)}T`;
  if (abs >= 1e9) return `£${(value / 1e9).toFixed(1)}B`;
  if (abs >= 1e6) return `£${(value / 1e6).toFixed(1)}M`;
  return `£${value.toFixed(0)}`;
}

export function formatDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return "—";
  return d.toLocaleDateString("en-GB", {
    day: "numeric",
    month: "short",
    year: "numeric",
  });
}

export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return "—";
  return d.toLocaleString("en-GB", {
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function formatConfidence(value: number | null | undefined): string {
  if (value == null || !isFinite(value)) return "—";
  return `${Math.round(value * 100)}%`;
}

export function timeAgo(iso: string | null | undefined): string {
  if (!iso) return "—";
  const then = new Date(iso).getTime();
  if (isNaN(then)) return "—";
  const secs = Math.max(0, Math.floor((Date.now() - then) / 1000));
  if (secs < 60) return "just now";
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  if (days < 30) return `${days}d ago`;
  return formatDate(iso);
}

export function hostOf(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return url;
  }
}
