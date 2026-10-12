/**
 * Persistent application shell: sidebar navigation + top bar with
 * live backend connection status and last-run timestamp.
 */
import { useCallback, useEffect, useState } from "react";
import { Link, NavLink, Outlet, useLocation } from "react-router-dom";
import {
  Activity,
  FileText,
  LineChart,
  PlayCircle,
  RefreshCw,
  Settings as SettingsIcon,
  Telescope,
} from "lucide-react";
import { api } from "../lib/api";
import type { HealthResponse, ResearchRun } from "../types";
import { timeAgo } from "../lib/format";
import { cn } from "../lib/cn";

const NAV = [
  { to: "/runs", label: "Research Runs", icon: PlayCircle, end: false },
  { to: "/deep-research", label: "Deep Research", icon: Telescope, end: false },
  { to: "/reports", label: "Reports", icon: FileText, end: false },
  { to: "/settings", label: "Settings", icon: SettingsIcon, end: false },
];

function useBackendStatus(refreshKey: number) {
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [lastRun, setLastRun] = useState<ResearchRun | null>(null);
  const [error, setError] = useState(false);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [h, runs] = await Promise.all([
          api.health(),
          api.runs(20).catch(() => ({ count: 0, runs: [] })),
        ]);
        if (cancelled) return;
        setHealth(h);
        setError(false);
        const done = runs.runs.find((r) => r.status === "completed");
        setLastRun(done ?? null);
      } catch {
        if (!cancelled) {
          setHealth(null);
          setError(true);
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [refreshKey]);

  return { health, lastRun, error };
}

const TITLES: Record<string, string> = {
  "/runs": "Research Runs",
  "/reports": "Research Reports",
  "/settings": "Settings",
};

export default function Layout() {
  const location = useLocation();
  const [refreshKey, setRefreshKey] = useState(0);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const { health, lastRun, error } = useBackendStatus(refreshKey);

  const refresh = useCallback(() => setRefreshKey((k) => k + 1), []);

  // Keep the top-bar status fresh while runs are active.
  useEffect(() => {
    const id = setInterval(() => setRefreshKey((k) => k + 1), 30000);
    return () => clearInterval(id);
  }, []);

  const base = location.pathname.split("/").slice(0, 2).join("/") || "/";
  const title = TITLES[base] ?? TITLES[location.pathname] ?? "News Research Agent";

  const connected = !error && health?.status === "ok";
  const llmOk = health?.ollama.ok ?? false;

  const sidebar = (
    <div className="flex h-full flex-col">
      <Link to="/runs" className="flex items-center gap-2.5 px-5 pb-6 pt-6">
        <span className="flex h-9 w-9 items-center justify-center rounded-lg bg-indigo-500/15 text-indigo-300">
          <LineChart className="h-5 w-5" />
        </span>
        <span className="leading-tight">
          <span className="block text-sm font-semibold text-white">FTSE 50</span>
          <span className="block text-[11px] text-terminal-muted">News Research Agent</span>
        </span>
      </Link>
      <nav className="flex-1 space-y-1 px-3">
        {NAV.map((item) => (
          <NavLink
            key={item.to}
            to={item.to}
            end={item.end}
            onClick={() => setSidebarOpen(false)}
            className={({ isActive }) =>
              cn(
                "flex items-center gap-3 rounded-lg px-3 py-2.5 text-sm font-medium transition-colors",
                isActive
                  ? "bg-indigo-500/15 text-white"
                  : "text-terminal-muted hover:bg-white/5 hover:text-white"
              )
            }
          >
            <item.icon className="h-[18px] w-[18px]" />
            {item.label}
          </NavLink>
        ))}
      </nav>
      <div className="border-t border-terminal-border p-4">
        <div className="flex items-center gap-2 text-xs">
          <span
            className={cn(
              "h-2 w-2 rounded-full",
              connected ? "bg-emerald-400" : "animate-pulse-dot bg-red-400"
            )}
          />
          <span className={connected ? "text-terminal-muted" : "text-red-300"}>
            {connected ? "Backend connected" : "Backend unreachable"}
          </span>
        </div>
        {!llmOk && connected && (
          <p className="mt-1.5 text-[11px] leading-snug text-amber-300/90">
            LLM not configured — live runs disabled.
          </p>
        )}
      </div>
    </div>
  );

  return (
    <div className="flex h-screen overflow-hidden bg-terminal-bg">
      {/* Desktop sidebar */}
      <aside className="hidden w-60 shrink-0 border-r border-terminal-border bg-terminal-surface/60 md:block">
        {sidebar}
      </aside>
      {/* Mobile sidebar */}
      {sidebarOpen && (
        <div className="fixed inset-0 z-40 md:hidden">
          <div
            className="absolute inset-0 bg-black/70"
            onClick={() => setSidebarOpen(false)}
          />
          <aside className="absolute left-0 top-0 h-full w-60 border-r border-terminal-border bg-terminal-surface">
            {sidebar}
          </aside>
        </div>
      )}

      <div className="flex min-w-0 flex-1 flex-col">
        {/* Top bar */}
        <header className="flex items-center gap-3 border-b border-terminal-border bg-terminal-surface/60 px-4 py-3 md:px-6">
          <button
            className="rounded-md p-2 text-terminal-muted hover:bg-white/5 hover:text-white md:hidden"
            onClick={() => setSidebarOpen(true)}
            aria-label="Open navigation"
          >
            <Activity className="h-5 w-5" />
          </button>
          <div className="min-w-0 flex-1">
            <h1 className="truncate text-base font-semibold text-white">{title}</h1>
            <p className="hidden truncate text-xs text-terminal-faint sm:block">
              {lastRun
                ? `Last successful run ${timeAgo(lastRun.finished_at)}${lastRun.report_id ? ` · ${lastRun.report_id}` : ""}`
                : connected
                  ? "No research runs yet"
                  : "Waiting for backend…"}
            </p>
          </div>
          <button
            onClick={refresh}
            className="inline-flex items-center gap-2 rounded-md border border-terminal-border bg-terminal-surface-2 px-3 py-2 text-xs font-medium text-terminal-muted transition-colors hover:border-indigo-400/60 hover:text-white"
          >
            <RefreshCw className="h-3.5 w-3.5" />
            <span className="hidden sm:inline">Refresh</span>
          </button>
        </header>

        {/* Page content */}
        <main className="flex-1 overflow-y-auto">
          <div className="mx-auto max-w-7xl px-4 py-6 md:px-6" key={location.pathname}>
            <Outlet />
          </div>
        </main>
      </div>
    </div>
  );
}
