const API_BASE = (import.meta.env.VITE_NOEMA_API || import.meta.env.VITE_AI_ACTIVITY_API || 'http://127.0.0.1:8765').replace(/\/$/, '');

export type ActivityEvent = {
  id?: string;
  timestamp?: string;
  app?: string;
  application?: string;
  domain?: string;
  url?: string;
  title?: string;
  duration?: number;
  category?: string;
  productivity?: string;
  browser?: string;
  device?: string;
  classification_confidence?: number;
  classification_source?: string | null;
  classification_status?: string;
  evidence_quality?: string | null;
  evidence_quality_score?: number | null;
  provider?: string | null;
  model?: string | null;
};

export type Health = {
  status?: string;
  pid?: number;
  provider?: string;
  provider_model?: string;
  provider_health?: string;
  source_connected?: boolean;
  gemini_configured?: boolean;
  openrouter_configured?: boolean;
  provider_chain?: { provider?: string; model?: string | null }[];
  uptime_seconds?: number;
  config?: Record<string, string | number | boolean | null>;
  workers?: Record<string, { status?: string; interval_seconds?: number; run_count?: number; error_count?: number }>;
};

export type Behavior = {
  state?: string;
  actionable?: boolean;
  distraction_score?: number;
  confidence?: number;
  reason?: string;
};

export type DailySegment = {
  timestamp: string;
  end: string;
  seconds: number;
  application: string;
  activity: string;
  title?: string;
  domain?: string;
  productivity: 'productive' | 'distracting' | 'neutral' | 'unclassified';
  confidence: number;
  activity_description?: string;
  signal?: string;
  source?: string;
  classification_status?: string;
  evidence_quality?: string | null;
  evidence_quality_score?: number | null;
};

export type DailySummary = {
  date: string;
  timezone: string;
  range: string;
  total_time: number;
  productive_time: number;
  distractive_time: number;
  neutral_time: number;
  unclassified_time: number;
  unclassified_seconds?: number;
  classified_time?: number;
  classified_seconds?: number;
  afk_time?: number;
  afk_seconds?: number;
  active_time?: number;
  active_seconds?: number;
  coverage_percent?: number | null;
  productivity_score: number | null;
  applications: { name: string; seconds: number }[];
  distractions: { name: string; seconds: number }[];
  timeline: DailySegment[];
};

export type QuotaBucket = { limit: number; used: number | null; left: number | null };
export type QuotaRow = {
  model: string;
  provider?: string;
  rpm: QuotaBucket;
  tpm: QuotaBucket;
  rpd: QuotaBucket;
  tokens_today?: number;
  last_latency_ms?: number | null;
  cooldown_seconds?: number;
  consecutive_failures?: number;
  last_error?: string | null;
  last_success?: string | null;
  status: string;
  context_window?: number | null;
  role?: string | null;
  purpose?: string | null;
  capabilities?: string[] | null;
  attempts_today?: number | null;
  attempts_this_minute?: number | null;
  rate_limited_count?: number | null;
  last_rate_limited_at?: string | null;
  fast_path?: boolean | null;
  excludes?: string[] | null;
  display_name?: string | null;
  vendor?: string | null;
  max_output?: number | null;
  normal_classification?: boolean | null;
  enabled?: boolean | null;
  priority?: number | null;
  quota_scope?: string | null;
};
export type QuotaPayload = {
  day: string;
  provider?: string | null;
  ollama_fallback?: boolean | null;
  openrouter_configured?: boolean | null;
  openrouter_shared_quota?: boolean | null;
  openrouter_quota_note?: string | null;
  fast_distraction_model?: string | null;
  openrouter_registry?: Record<string, unknown>[];
  quota_day_timezone?: string | null;
  provider_reset_timezone?: string | null;
  rpd_policy?: string | null;
  models: QuotaRow[];
  embeddings: QuotaRow[];
  note?: string;
};
export type FastCheck = {
  checked_at?: string;
  state?: string;
  verdict?: string;
  confidence?: number;
  reason?: string;
  model?: string | null;
  model_calls?: number;
  latency_ms?: number | null;
  session_id?: string | null;
  quota?: { used?: number | null; limit?: number | null } | null;
  advisory?: boolean;
  stored?: boolean;
};
export type Detection = {
  detection_id?: string;
  timestamp?: string;
  session_id?: string | null;
  window_minutes?: number;
  candidate_score?: number;
  tracker_state?: string;
  model_verdict?: string | null;
  severity?: number | null;
  recommended_intervention?: string | null;
  provider?: string | null;
  model?: string | null;
  latency_ms?: number | null;
  decision?: string;
  reason?: string;
};
export type RealtimeStatus = {
  tracker?: {
    state?: string;
    state_since?: string | null;
    last_score?: number;
    enter_threshold?: number;
    exit_threshold?: number;
  } | null;
  last_detection?: Detection | null;
  detector?: {
    enter_threshold?: number;
    exit_threshold?: number;
    min_active_seconds?: number;
  } | null;
  fast_verifier_configured?: boolean;
};
export type LatencySummary = {
  n?: number;
  min?: number | null;
  max?: number | null;
  mean?: number | null;
  p50?: number | null;
  p95?: number | null;
  p99?: number | null;
  insufficient_samples?: boolean;
};
export type ModelPerfRow = {
  provider?: string;
  model?: string | null;
  purpose?: string;
  requests?: number;
  attempts?: number;
  successful_requests?: number;
  failed_requests?: number;
  success_rate?: number | null;
  retries?: number;
  fallbacks?: number;
  latency?: LatencySummary | null;
  tokens_exact_in?: number;
  tokens_exact_out?: number;
  tokens_estimated_in?: number;
  tokens_unknown_calls?: number;
  mean_tps?: number | null;
  tps_basis?: string | null;
  tps_samples?: number;
  errors?: Record<string, number>;
  timeout_rate?: number | null;
  rate_limit_rate?: number | null;
  server_error_rate?: number | null;
  malformed_rate?: number | null;
};
export type TokenBucket = {
  exact?: number;
  estimated?: number;
  unknown_calls?: number;
};
export type TokenWindows = {
  timezone?: string;
  windows?: Record<string, { input?: TokenBucket; output?: TokenBucket; total?: { exact?: number } }>;
  efficiency?: {
    tokens_per_classified_session?: number | null;
    classification_tokens_total?: number;
    classified_sessions?: number;
    tokens_per_verification?: number | null;
    verification_tokens_total?: number;
    verifications?: number;
    tokens_per_meme?: number | null;
    meme_tokens_total?: number;
    memes?: number;
    interventions?: number;
  };
  batches?: {
    batch_requests?: number;
    sessions_per_request?: number | null;
    tokens_per_request?: number | null;
    tokens_per_session?: number | null;
    single_requests?: number;
    single_tokens_per_session?: number | null;
    utilization?: LatencySummary | null;
    pack_events?: number;
    tail_sheds?: number;
    split_events?: number;
  };
  retry_fallback?: {
    initial_attempts?: { calls?: number; tokens?: number; latency_ms?: number };
    retries?: { calls?: number; tokens?: number; latency_ms?: number };
    fallbacks?: { calls?: number; tokens?: number; latency_ms?: number };
    fallback_edges?: { edge?: string; count?: number; reasons?: Record<string, number> }[];
  };
};
export type ProviderHealthRow = {
  provider?: string;
  models?: (string | null)[];
  requests?: number;
  successful_requests?: number;
  failed_requests?: number;
  success_rate?: number | null;
  retries?: number;
  fallbacks?: number;
};
export type QuotaModelRow = {
  model?: string;
  requests_today?: number;
  requests_limit?: number;
  request_utilization?: number | null;
  tokens_this_minute?: number;
  tokens_limit?: number;
  cooling_down?: boolean;
  consecutive_failures?: number;
  last_error?: string | null;
};
export type RealtimeMetrics = {
  evaluations?: number;
  evaluation_latency?: LatencySummary | null;
  candidates?: number;
  verifications?: number;
  verification_latency?: LatencySummary | null;
  verification_tokens_total?: number;
  verification_tokens_mean?: number | null;
  verification_success_rate?: number | null;
  confirmed?: number;
  confirmation_rate?: number | null;
  interventions_triggered?: number;
  intervention_rate?: number | null;
  recovered?: number;
  continued_distraction?: number;
  unknown_outcome?: number;
  recovery_rate?: number | null;
  by_intervention_type?: Record<string, { interventions?: number; recovered?: number; recovery_rate?: number | null }>;
  detection_to_intervention_ms?: number | null;
};
export type WorkerMetricsRow = {
  status?: string;
  run_count?: number;
  error_count?: number;
  last_started_at?: string | null;
  last_success_at?: string | null;
  last_error?: string | null;
  measured_runs?: number;
  duration?: LatencySummary | null;
};
export type BenchmarkResultRow = {
  provider?: string;
  model?: string | null;
  fixture?: string;
  iterations?: number;
  success_count?: number;
  schema_valid_count?: number;
  failure_count?: number;
  mean_latency_ms?: number | null;
  p50_latency_ms?: number | null;
  p95_latency_ms?: number | null;
  mean_out_tokens?: number | null;
  mean_tps?: number | null;
};
export type BenchmarkRunInfo = {
  run_id?: string;
  timestamp?: string;
  environment?: Record<string, unknown>;
  test_set?: string;
  purpose?: string;
  iterations?: number;
  duration_ms?: number | null;
  status?: string;
  note?: string;
  results?: BenchmarkResultRow[];
};
export type ResultRow = {
  id?: string;
  session_id?: string;
  timestamp?: string;
  application?: string;
  title?: string;
  domain?: string;
  url?: string;
  duration_seconds?: number;
  category?: string | null;
  productivity?: string | null;
  confidence?: number;
  signal?: string | null;
  classification_status?: string;
  classification_source?: string | null;
  service?: string | null;
  topic?: string | null;
  activity_type?: string | null;
  evidence_quality?: string | null;
  evidence_quality_score?: number | null;
  provider?: string | null;
  model?: string | null;
  inherited_from?: string | null;
};
export type PresenceSnapshot = {  state?: string;
  last_input_at?: string | null;
  idle_seconds?: number | null;
  as_of?: string;
  stale?: boolean;
  current_session?: {
    session_id?: string;
    start?: string;
    end?: string;
    active_duration_seconds?: number;
    afk_duration_seconds?: number;
    presence?: string;
    application?: string | null;
    context?: string | null;
    classification?: { category?: string; confidence?: number; source?: string; status?: string; evidence_quality?: string | null; provider?: string | null; model?: string | null } | null;
  } | null;
  afk_since?: string | null;
  last_activity?: { app?: string | null; title?: string | null; domain?: string | null; at?: string } | null;
};
export type MeaningfulVerdict = {
  session_id?: string;
  start?: string;
  end?: string;
  duration_seconds?: number;
  active_duration_seconds?: number;
  applications?: string[];
  topic?: string | null;
  category?: string | null;
  confidence?: number;
  source?: string | null;
  classification_status?: string;
  evidence_quality?: string | null;
  evidence_quality_score?: number | null;
  provider?: string | null;
  model?: string | null;
};
export type ClassificationStatus = {
  enabled?: boolean;
  intervalMinutes?: number;
  status?: string;
  presence?: PresenceSnapshot | null;
  lastRunAt?: string | null;
  lastRunDurationSeconds?: number | null;
  nextRunAt?: string | null;
  processedLastRun?: number;
  failedLastRun?: number;
  lastError?: string | null;
  pendingCount?: number;
  processingCount?: number;
  failedCount?: number;
  classifiedCount?: number;
  totalEligibleCount?: number;
  coveragePercent?: number | null;
  latestClassificationAt?: string | null;
  provider?: string | null;
  model?: string | null;
  quota?: { model?: string | null; requestsUsed?: number | null; requestsLimit?: number | null; requestsRemaining?: number | null } | null;
};

export type Feedback = {
  id?: number;
  session_id?: string;
  verdict?: string;
  note?: string | null;
  source?: string;
  model?: string | null;
  classifier_version?: string | null;
  prompt_version?: string | null;
  created_at?: string;
};

export type FeedbackQuality = {
  model?: string | null;
  feedback_count?: number;
  correct_count?: number;
  incorrect_count?: number;
  accuracy?: number | null;
  per_model?: FeedbackQuality[];
};

export type Belief = {  session_id?: string;
  state?: string;
  started_at?: string;
  confidence?: number;
  distraction_score?: number;
  actionable?: boolean;
  reason?: string;
  previous_state?: string | null;
};

export type BrowserRegistration = {
  browser?: string;
  device_id?: string;
  extension_instance_id?: string;
  current_window_id?: string | null;
  current_tab_id?: string | null;
};

export type DashboardData = {
  health: Health;
  events: ActivityEvent[];
  sessions: Record<string, unknown>[];
  meaningful: Record<string, unknown>[];
  classifications: Record<string, unknown>[];
  behavior: Behavior[];
  interventions: Record<string, unknown>[];
  outcomes: Record<string, unknown>[];
  memories: Record<string, unknown>[];
  registrations: BrowserRegistration[];
  daily: DailySummary;
};

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, { cache: 'no-store', ...init });
  const text = await response.text();
  let payload: unknown;
  try { payload = text ? JSON.parse(text) : {}; } catch { throw new Error('Noema returned invalid JSON'); }
  if (!response.ok) throw new Error((payload as { error?: string }).error || `Local API returned ${response.status}`);
  return payload as T;
}

export const api = {
  health: () => request<Health>('/api/daemon/health'),
  summary: (range = 'today') => request<{ daily: DailySummary }>(`/api/dashboard/summary?range=${range}`),
  events: () => request<{ events: ActivityEvent[] }>('/api/events?limit=100'),
  sessions: () => request<{ sessions: Record<string, unknown>[] }>('/api/sessions?limit=1000'),
  meaningful: () => request<{ sessions: Record<string, unknown>[] }>('/api/meaningful-sessions?limit=1000'),
  classifications: () => request<{ classifications: Record<string, unknown>[] }>('/api/classifications?limit=1000'),
  behavior: () => request<{ observations: Behavior[] }>('/api/behavior?limit=10'),
  interventions: () => request<{ interventions: Record<string, unknown>[] }>('/api/interventions?limit=1000'),
  outcomes: () => request<{ outcomes: Record<string, unknown>[] }>('/api/outcomes?limit=1000'),
  memories: () => request<{ memories: Record<string, unknown>[] }>('/api/memories?limit=1000'),
  registrations: () => request<{ registrations: BrowserRegistration[] }>('/api/browser/registrations'),
  cycle: () => request<Record<string, unknown>>('/api/autonomous/cycle', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' }),
  reload: () => request<Record<string, unknown>>('/api/daemon/reload', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' }),
  intent: (text: string) => request<Record<string, unknown>>('/api/intents', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text }) }),
  categorize: (registration: BrowserRegistration) => request<Record<string, unknown>>('/api/browser/category', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ device: registration.device_id, browser: registration.browser, window_id: registration.current_window_id, tab_id: registration.current_tab_id }) }),
  quotas: () => request<QuotaPayload>('/api/debug/quotas'),
  metricsSummary: (days = 30) => request<{ window_days?: number; recording?: boolean; invocations?: number; requests?: number; attempts?: number; success_rate?: number | null; tokens?: TokenWindows['windows']; efficiency?: TokenWindows['efficiency']; quota?: { models?: QuotaModelRow[]; quota_denied_calls?: number }; cost?: { free_tier_tokens?: number; unknown_cost_calls?: number }; realtime?: RealtimeMetrics; workers?: Record<string, WorkerMetricsRow>; retention?: Record<string, number> }>(`/api/metrics/summary?days=${days}`),
  metricsModels: (days = 30) => request<{ window_days?: number; models: ModelPerfRow[] }>(`/api/metrics/models?days=${days}`),
  metricsTokens: (days = 30) => request<{ window_days?: number; tokens: TokenWindows; cost?: { free_tier_tokens?: number; unknown_cost_calls?: number } }>(`/api/metrics/tokens?days=${days}`),
  metricsRealtime: (days = 30) => request<{ window_days?: number; realtime: RealtimeMetrics }>(`/api/metrics/realtime?days=${days}`),
  benchmarks: (limit = 10) => request<{ runs: BenchmarkRunInfo[] }>(`/api/benchmarks?limit=${limit}`),
  benchmark: (id: string) => request<{ run: BenchmarkRunInfo }>(`/api/benchmarks/${id}`),
  fastCheck: () => request<FastCheck>('/api/distraction/fast-check'),
  realtime: () => request<RealtimeStatus>('/api/realtime/status'),
  detections: (limit = 20) => request<{ detections: Detection[] }>(`/api/detections?limit=${limit}`),
  evaluateRealtime: () => request<Record<string, unknown>>('/api/realtime/evaluate', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' }),
  classificationStatus: () => request<ClassificationStatus>('/api/classification/status'),
  runAi: () => request<Record<string, unknown>>('/api/ai/run', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' }),
  evaluateBeliefs: () => request<{ observations: Belief[] }>('/api/behavior/evaluate', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' }),
  beliefs: () => request<{ observations: Belief[] }>('/api/behavior?limit=50'),
  recent: (range = '24h', limit = 100) => request<{ range: string; timezone: string; activities: ResultRow[] }>(`/api/dashboard/recent-activity?range=${range}&limit=${limit}`),
  recentMeaningful: (limit = 12) => request<{ classifications: MeaningfulVerdict[] }>(`/api/dashboard/recent-classifications?limit=${limit}`),
  feedback: (session_id: string, verdict: 'correct' | 'incorrect', note?: string) => request<{ feedback: Feedback }>('/api/feedback', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ session_id, verdict, note }) }),
  feedbackFor: (session_id: string) => request<{ feedback: Feedback | null }>(`/api/feedback?session_id=${encodeURIComponent(session_id)}`),
  feedbackQuality: (model?: string) => request<FeedbackQuality>(`/api/feedback/quality${model ? `?model=${encodeURIComponent(model)}` : ''}`),
  presence: () => request<PresenceSnapshot>('/api/presence/current'),
};

export type CommandCenterData = DashboardData & {
  classification: ClassificationStatus | null;
  recent: ResultRow[];
  recentMeaningful: MeaningfulVerdict[];
  presence: PresenceSnapshot | null;
};

export async function loadCommandCenter(range = 'today'): Promise<CommandCenterData> {
  const [dashboard, classification, recent, meaningful, presence] = await Promise.all([
    loadDashboard(range),
    api.classificationStatus().catch(() => null),
    api.recent('today', 60).catch(() => ({ activities: [] as ResultRow[] })),
    api.recentMeaningful(12).catch(() => ({ classifications: [] as MeaningfulVerdict[] })),
    api.presence().catch(() => null),
  ]);
  return {
    ...dashboard,
    classification,
    recent: recent.activities || [],
    recentMeaningful: meaningful.classifications || [],
    presence,
  };
}

export async function loadDashboard(range = 'today'): Promise<DashboardData> {
  const [health, summary, events, sessions, meaningful, classifications, behavior, interventions, outcomes, memories, registrations] = await Promise.all([
    api.health(), api.summary(range), api.events(), api.sessions(), api.meaningful(), api.classifications(), api.behavior(), api.interventions(), api.outcomes(), api.memories(), api.registrations(),
  ]);
  return { health, daily: summary.daily, events: events.events || [], sessions: sessions.sessions || [], meaningful: meaningful.sessions || [], classifications: classifications.classifications || [], behavior: behavior.observations || [], interventions: interventions.interventions || [], outcomes: outcomes.outcomes || [], memories: memories.memories || [], registrations: registrations.registrations || [] };
}

export { API_BASE };

// --- Meme Center (V3 Phase 10) ------------------------------------------

export type MemeAsset = {
  id: string;
  source?: string;
  source_ref?: string;
  filename?: string;
  ocr_text?: string | null;
  corrected_text?: string | null;
  display_text?: string | null;
  sentiment?: string | null;
  tags?: string[];
  favorite?: boolean;
  enabled?: boolean;
  width?: number | null;
  height?: number | null;
  byte_size?: number | null;
  image_format?: string | null;
  created_at?: string | null;
  response_count?: number;
  curated?: boolean;
  responses?: { id: string; kind: string; tone: string; enabled: boolean }[];
};

export type MemeAssetList = { items: MemeAsset[]; total: number };

export type MemeAssetStats = {
  assets: number;
  favorites: number;
  by_sentiment: Record<string, number>;
  curated_assets: number;
  active_responses: number;
};

export type CuratedResponse = {
  id: string;
  kind: string;
  tone: string;
  title: string;
  body_template: string;
  asset_id?: string | null;
  severity_min: number;
  severity_max: number;
  context_tags: string[];
  cooldown_seconds: number;
  enabled: boolean;
  times_shown: number;
  times_clicked: number;
  recovery_count: number;
  recovery_rate?: number | null;
  last_shown_at?: string | null;
};

export type ResponseEffectivenessRow = {
  id: string;
  kind: string;
  tone: string;
  times_shown: number;
  times_clicked: number;
  recovery_count: number;
  recovery_rate?: number | null;
};

export type IngestStats = {
  csv_rows: number;
  images_found: number;
  matched: number;
  missing: number;
  invalid: number;
  duplicates: number;
  imported: number;
  refreshed: number;
  thumbnails: number;
  ocr_coverage: number;
  corrected_coverage: number;
  sentiment_counts: Record<string, number>;
  problem_count: number;
  problems: string[];
};

export type TestDeliveryReceipt = {
  test: boolean;
  persisted: boolean;
  delivery: Record<string, unknown>;
  payload: Record<string, unknown>;
  action: Record<string, unknown>;
  toast_fallback?: Record<string, unknown>;
};

export type InterventionOutcomeRow = {
  id: string;
  intervention_id: string;
  intervention_time: string;
  recovery_status: 'RECOVERED' | 'NOT_RECOVERED' | 'PENDING' | string;
  recovery_time?: string | null;
  recovery_session_id?: string | null;
  recovery_duration_seconds?: number | null;
  intervention_type?: string | null;
  meme_id?: string | null;
  detection_id?: string | null;
  response_id?: string | null;
  delivery_state?: string | null;
  user_action?: string | null;
  attribution?: 'direct' | 'ambient' | 'none' | string | null;
  break_context?: boolean;
};

export type InterventionDecisionSummary = {
  should_intervene?: boolean | null;
  intervention_type?: string | null;
  severity?: number | null;
  tone?: string | null;
  response_class?: string | null;
  use_meme?: boolean | null;
  confidence?: number | null;
};

export type InterventionFeedRow = {
  id: string;
  session_id?: string | null;
  mode?: string | null;
  status?: string | null;
  reason?: string | null;
  created_at?: string | null;
  response_id?: string | null;
  response_kind?: string | null;
  message?: { title?: string; body?: string } | null;
  intervention_decision?: InterventionDecisionSummary | null;
  meme_asset?: { id: string; filename?: string } | null;
  delivered?: boolean;
  interacted?: boolean;
  actions?: string[];
  outcome?: InterventionOutcomeRow | null;
  feedback?: Record<string, unknown>[];
};

export function memeAssetImageUrl(assetId: string, thumb = true): string {
  return `${API_BASE}/api/meme-assets/${encodeURIComponent(assetId)}/${thumb ? 'thumb' : 'image'}`;
}

export const memeCenterApi = {
  assets: (params: { q?: string; sentiment?: string; status?: string; limit?: number; offset?: number } = {}) => {
    const query = new URLSearchParams();
    if (params.q) query.set('q', params.q);
    if (params.sentiment) query.set('sentiment', params.sentiment);
    if (params.status) query.set('status', params.status);
    query.set('limit', String(params.limit ?? 60));
    query.set('offset', String(params.offset ?? 0));
    return request<MemeAssetList>(`/api/meme-assets?${query.toString()}`);
  },
  asset: (id: string) => request<{ asset: MemeAsset }>(`/api/meme-assets/${encodeURIComponent(id)}`),
  assetResponses: (id: string) => request<{ asset_id: string; responses: NonNullable<MemeAsset['responses']> }>(`/api/meme-assets/${encodeURIComponent(id)}/responses`),
  favorite: (id: string, favorite: boolean) =>
    request<{ asset: MemeAsset }>(`/api/meme-assets/${encodeURIComponent(id)}/favorite`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ favorite }),
    }),
  tags: (id: string, tags: string[]) =>
    request<{ asset: MemeAsset }>(`/api/meme-assets/${encodeURIComponent(id)}/tags`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ tags }),
    }),
  setEnabled: (id: string, enabled: boolean) =>
    request<{ asset: MemeAsset }>(`/api/meme-assets/${encodeURIComponent(id)}/enabled`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ enabled }),
    }),
  stats: () => request<MemeAssetStats>('/api/meme-assets/stats'),
  ingest: (csv_path: string, images_dir: string, make_thumbs = true) =>
    request<IngestStats>('/api/meme-assets/ingest', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ csv_path, images_dir, make_thumbs }),
    }),
  createResponse: (body: Record<string, unknown>) =>
    request<{ response: CuratedResponse }>('/api/responses', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    }),
  updateResponse: (id: string, body: Record<string, unknown>) =>
    request<{ response: CuratedResponse }>(`/api/responses/${encodeURIComponent(id)}`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    }),
  testResponse: (id: string, context?: Record<string, unknown>) =>
    request<TestDeliveryReceipt>(`/api/responses/${encodeURIComponent(id)}/test`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ context: context ?? {} }),
    }),
  effectiveness: () => request<{ effectiveness: ResponseEffectivenessRow[] }>('/api/responses/effectiveness'),
  registry: () => request<{ responses: CuratedResponse[] }>('/api/responses'),
  interventionFeed: (limit = 50) =>
    request<{ interventions: InterventionFeedRow[] }>(`/api/interventions/feed?limit=${limit}`),
};
