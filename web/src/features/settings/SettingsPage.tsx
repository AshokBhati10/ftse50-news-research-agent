/**
 * Settings: operational health and configuration status.
 * Shows *whether* services are configured — never secret values.
 * This page is read-only; configuration lives in the backend .env file.
 */
import { useEffect, useState } from "react";
import { CheckCircle2, Database, KeyRound, Search, Server, XCircle } from "lucide-react";
import { api } from "../../lib/api";
import type { HealthResponse, SettingsStatus } from "../../types";
import {
  Alert,
  Badge,
  Card,
  CardHeader,
  ErrorState,
  LoadingState,
} from "../../components/ui";
import { cn } from "../../lib/cn";

function StatusRow({
  icon,
  label,
  ok,
  detail,
}: {
  icon: React.ReactNode;
  label: string;
  ok: boolean;
  detail: string;
}) {
  return (
    <div className="flex items-start gap-3 border-b border-terminal-border/50 px-5 py-3.5 last:border-0">
      <span className={cn("mt-0.5", ok ? "text-emerald-300" : "text-red-300")}>
        {ok ? <CheckCircle2 className="h-5 w-5" /> : <XCircle className="h-5 w-5" />}
      </span>
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2 text-sm font-medium text-white">
          {icon}
          {label}
        </div>
        <p className="mt-0.5 text-xs leading-relaxed text-terminal-muted">{detail}</p>
      </div>
      <Badge tone={ok ? "up" : "down"}>{ok ? "ok" : "issue"}</Badge>
    </div>
  );
}

export default function SettingsPage() {
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [settings, setSettings] = useState<SettingsStatus | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = async () => {
    setError(null);
    try {
      const [h, s] = await Promise.all([api.health(), api.settingsStatus()]);
      setHealth(h);
      setSettings(s);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load settings.");
    }
  };

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  if (error) return <ErrorState message={error} onRetry={load} />;
  if (!health || !settings)
    return <LoadingState message="Checking backend status…" />;

  const cacheEntries = health.cache["entries"];

  return (
    <div className="space-y-6">
      {settings.warnings.length > 0 && (
        <Alert tone="mixed">
          <ul className="list-disc space-y-1 pl-4">
            {settings.warnings.map((w) => (
              <li key={w}>{w}</li>
            ))}
          </ul>
          <p className="mt-2 text-xs text-terminal-faint">
            Set these in the backend <span className="font-mono">.env</span> file and
            restart the API server. This page never displays secret values.
          </p>
        </Alert>
      )}

      <Card>
        <CardHeader title="Service health" subtitle="Live dependency checks" />
        <StatusRow
          icon={<Search className="h-4 w-4" />}
          label="SearXNG search"
          ok={health.searxng.ok}
          detail={health.searxng.detail || "Primary news search provider."}
        />
        <StatusRow
          icon={<KeyRound className="h-4 w-4" />}
          label="LLM analysis"
          ok={health.ollama.ok}
          detail={
            health.ollama.detail ||
            (settings.ollama_configured
              ? `Configured · model ${settings.ollama_model || "—"} · host ${settings.ollama_host}`
              : "Not configured.")
          }
        />
        <StatusRow
          icon={<Server className="h-4 w-4" />}
          label="You.com fallback search"
          ok={health.you_fallback.ok}
          detail={health.you_fallback.detail}
        />
        <StatusRow
          icon={<Database className="h-4 w-4" />}
          label="Cache"
          ok
          detail={`${cacheEntries ?? 0} entries on disk · ${settings.defaults["cache_enabled"] ? "enabled" : "disabled"} by default (a run can disable it).`}
        />
      </Card>

      <Card>
        <CardHeader title="Configuration" subtitle="Non-secret operational values" />
        <dl className="grid gap-x-8 gap-y-3 px-5 py-4 text-sm sm:grid-cols-2">
          <ConfigRow label="LLM model" value={settings.ollama_model || "—"} mono />
          <ConfigRow label="LLM host" value={settings.ollama_host} mono />
          <ConfigRow label="SearXNG base URL" value={settings.searxng_base_url || "—"} mono />
          <ConfigRow label="API version" value={health.version} />
          <ConfigRow label="Top articles per company" value={String(settings.defaults["top_n_articles"] ?? "—")} />
          <ConfigRow label="Max articles analysed" value={String(settings.defaults["max_articles_per_company"] ?? "—")} />
          <ConfigRow label="Search concurrency" value={String(settings.defaults["max_search_concurrency"] ?? "—")} />
          <ConfigRow label="LLM concurrency" value={String(settings.defaults["max_llm_concurrency"] ?? "—")} />
          <ConfigRow label="News lookback (days)" value={String(settings.defaults["lookback_days"] ?? "—")} />
          <ConfigRow label="Dedup similarity threshold" value={String(settings.defaults["dedup_threshold"] ?? "—")} />
        </dl>
        <div className="border-t border-terminal-border px-5 py-3">
          <p className="text-xs leading-relaxed text-terminal-faint">
            This dashboard is intended for local, single-user operation and has no
            login — do not expose the backend publicly without a security review.
            API keys are configured server-side only and are never sent to the
            browser.
          </p>
        </div>
      </Card>
    </div>
  );
}

function ConfigRow({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div>
      <dt className="text-[11px] uppercase tracking-wide text-terminal-faint">{label}</dt>
      <dd className={cn("mt-0.5 break-all text-terminal-text", mono && "font-mono text-xs")}>
        {value}
      </dd>
    </div>
  );
}
