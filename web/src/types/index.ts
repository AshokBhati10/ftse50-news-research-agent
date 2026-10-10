/** Shared types mirroring the FastAPI response schemas (api/schemas.py). */

export interface DependencyStatus {
  ok: boolean;
  detail: string;
}

export interface HealthResponse {
  status: string;
  version: string;
  searxng: DependencyStatus;
  ollama: DependencyStatus;
  you_fallback: DependencyStatus;
  cache: Record<string, unknown>;
}

export interface Company {
  name: string;
  ticker: string;
  market_cap: number | null;
}

export interface CompaniesResponse {
  count: number;
  companies: Company[];
}

export interface ReportSummary {
  id: string;
  date: string;
  generated_at: string | null;
  company_count: number;
  article_count: number;
  companies: string[];
}

export type Impact = "high" | "medium" | "low";
export type Sentiment = "positive" | "negative" | "neutral";

export interface ReportArticle {
  title: string;
  url: string;
  source: string;
  published_at: string | null;
  impact: Impact;
  sentiment: Sentiment;
  confidence: number;
  reason: string;
  evidence_excerpt: string;
  source_quality: number | null;
  source_quality_tier: string | null;
}

export interface ReportCompany {
  company: string;
  ticker: string;
  market_cap: number | null;
  candidates_considered: number;
  providers_used: string[];
  fallback_used: boolean;
  notes: string;
  articles: ReportArticle[];
}

export interface ReportDetail {
  summary: ReportSummary;
  companies: ReportCompany[];
  has_markdown: boolean;
}

export interface ReportsResponse {
  count: number;
  reports: ReportSummary[];
}

export type RunStatus =
  | "queued"
  | "running"
  | "completed"
  | "failed"
  | "cancelled"
  | "interrupted";

export interface CompanyProgress {
  ticker: string;
  status: "completed" | "failed";
  articles: number;
  notes: string;
}

export interface ResearchRun {
  run_id: string;
  status: RunStatus;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  total_companies: number;
  completed: number;
  failed: number;
  current_company: string | null;
  articles_found: number;
  error: string | null;
  report_id: string | null;
  companies: CompanyProgress[];
  request: Record<string, unknown>;
}

export interface RunsResponse {
  count: number;
  runs: ResearchRun[];
}

export interface ResearchRunCreate {
  tickers?: string[];
  limit_companies?: number;
  agentic?: boolean;
  no_cache?: boolean;
}

export interface SettingsStatus {
  ollama_configured: boolean;
  ollama_model: string;
  ollama_host: string;
  searxng_base_url: string;
  searxng_configured: boolean;
  you_fallback_configured: boolean;
  defaults: Record<string, unknown>;
  warnings: string[];
}

/** Article enriched with its parent company context for display. */
export interface ArticleWithCompany extends ReportArticle {
  company: string;
  ticker: string;
  report_id: string;
}
