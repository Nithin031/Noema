import { useCallback, useEffect, useRef, useState } from 'react';
import { Activity, BrainCircuit, CheckCircle2, Gauge, Play, RefreshCw, ShieldCheck, TriangleAlert, Zap } from 'lucide-react';
import { api, type Belief, type BenchmarkRunInfo, type ClassificationStatus, type Detection, type FastCheck, type FeedbackQuality, type MeaningfulVerdict, type ModelPerfRow, type QuotaPayload, type QuotaRow, type RealtimeMetrics, type RealtimeStatus, type ResultRow, type TokenWindows } from '../../api/noema';

function fmtMs(value?: number | null): string {
  if (value == null || !Number.isFinite(value)) return 'n<5';
  return value >= 1000 ? `${(value / 1000).toFixed(1)}s` : `${Math.round(value)}ms`;
}

function fmtRate(value?: number | null): string {
  if (value == null || !Number.isFinite(value)) return '—';
  return `${Math.round(value * 1000) / 10}%`;
}

function ModelPerfTable({ rows }: { rows: ModelPerfRow[] }) {
  if (!rows.length) return <div className="empty">No model calls recorded yet — telemetry starts with the next classification.</div>;
  return (
    <div className="activity-table"><div className="table-head"><span>MODEL</span><span>PURPOSE</span><span>REQ</span><span>SUCCESS</span><span>P50</span><span>P95</span><span>TOKENS IN</span><span>TOK/S</span><span>FAIL</span></div>
      {rows.map((row, index) => <div className="table-row" key={`${row.provider}-${row.model}-${row.purpose}-${index}`}><span><strong>{str(row.model, row.provider)}</strong></span><span>{str(row.purpose)}</span><span>{num(row.requests)}</span><span>{fmtRate(row.success_rate)}</span><span>{fmtMs(row.latency?.p50)}</span><span>{fmtMs(row.latency?.p95)}</span><span>{num(row.tokens_exact_in).toLocaleString()}{num(row.tokens_estimated_in) > 0 ? ` (+${num(row.tokens_estimated_in).toLocaleString()} est)` : ''}{num(row.tokens_unknown_calls) > 0 ? ` · ${num(row.tokens_unknown_calls)} unknown` : ''}</span><span>{row.mean_tps != null ? `${num(row.mean_tps).toFixed(1)}${row.tps_basis === 'generation_time' ? '' : '~'}` : '—'}</span><span>{num(row.failed_requests)}{num(row.retries) > 0 ? ` · ${num(row.retries)} retry` : ''}{num(row.fallbacks) > 0 ? ` · ${num(row.fallbacks)} fb` : ''}</span></div>)}
    </div>
  );
}

function pct(used: number | null, limit: number): number {
  if (used == null || !limit) return 0;
  return Math.max(0, Math.min(100, (used / limit) * 100));
}

function num(value: unknown, fallback = 0): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function formatDuration(value?: number): string {
  const seconds = Math.round(Number(value || 0));
  if (seconds < 60) return `${seconds}s`;
  return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
}

function str(value: unknown, fallback = '—'): string {
  return value == null || value === '' ? fallback : String(value);
}

function QuotaCard({ row }: { row: QuotaRow }) {
  const tone = row.status === 'ok' ? 'good' : row.status === 'cooldown' ? 'warn' : 'bad';
  return (
    <div className="quota-card">
      <div className="quota-head"><strong>{row.model}</strong><span className={`status-pill ${tone}`}>{row.status}</span>{row.fast_path ? <span className="status-pill warn">FAST PATH</span> : null}{row.role && row.role !== 'fallback' ? <span className="status-pill">{row.role.toUpperCase()}</span> : null}{row.role === 'fallback' ? <span className="status-pill">FALLBACK</span> : null}</div>
      {row.purpose ? <div style={{ color: '#8b96ad', fontSize: 12, margin: '2px 0 8px' }}>{row.purpose}</div> : null}
      <div className="quota-row"><span>RPD</span><div className="quota-track"><i style={{ width: `${pct(row.rpd.used, row.rpd.limit)}%` }} /></div><b>{row.rpd.used ?? '—'}/{row.rpd.limit} · left {row.rpd.left ?? '—'}</b></div>
      <div className="quota-row"><span>RPM</span><div className="quota-track"><i style={{ width: `${pct(row.rpm.used, row.rpm.limit)}%` }} /></div><b>{row.rpm.used ?? '—'}/{row.rpm.limit} · left {row.rpm.left ?? '—'}</b></div>
      <div className="quota-row"><span>TPM</span><div className="quota-track"><i style={{ width: `${pct(row.tpm.used, row.tpm.limit)}%` }} /></div><b>{row.tpm.used ?? '—'}/{row.tpm.limit} · left {row.tpm.left ?? '—'}</b></div>
      <div className="quota-meta"><span>tokens today: {num(row.tokens_today).toLocaleString()}</span><span>attempts today: {num(row.attempts_today)}</span>{num(row.rate_limited_count) > 0 && <span>429s: {num(row.rate_limited_count)}</span>}{num(row.context_window) > 0 && <span>ctx {num(row.context_window).toLocaleString()}</span>}{row.capabilities && row.capabilities.length > 0 && <span>{row.capabilities.join(' · ')}</span>}{num(row.last_latency_ms) > 0 && <span>last {num(row.last_latency_ms).toLocaleString()}ms</span>}{num(row.cooldown_seconds) > 0 && <span>cooldown: {num(row.cooldown_seconds)}s</span>}{num(row.consecutive_failures) > 0 && <span>failures: {num(row.consecutive_failures)}</span>}</div>
      {row.last_error && <div className="quota-error">{row.last_error}</div>}
    </div>
  );
}

export default function ResultsPage() {
  const [quotas, setQuotas] = useState<QuotaPayload | null>(null);
  const [results, setResults] = useState<ResultRow[]>([]);
  const [episodes, setEpisodes] = useState<MeaningfulVerdict[]>([]);
  const [feedback, setFeedback] = useState<Record<string, string>>({});
  const [quality, setQuality] = useState<FeedbackQuality | null>(null);
  const [beliefs, setBeliefs] = useState<Belief[]>([]);
  const [runInfo, setRunInfo] = useState<Record<string, unknown> | null>(null);
  const [scheduler, setScheduler] = useState<ClassificationStatus | null>(null);
  const [fast, setFast] = useState<FastCheck | null>(null);
  const [fastBusy, setFastBusy] = useState(false);
  const [realtime, setRealtime] = useState<RealtimeStatus | null>(null);
  const [detections, setDetections] = useState<Detection[]>([]);
  const [rtBusy, setRtBusy] = useState(false);
  const [modelPerf, setModelPerf] = useState<ModelPerfRow[]>([]);
  const [tokenUsage, setTokenUsage] = useState<TokenWindows | null>(null);
  const [rtMetrics, setRtMetrics] = useState<RealtimeMetrics | null>(null);
  const [benchRuns, setBenchRuns] = useState<BenchmarkRunInfo[]>([]);
  const [benchDetail, setBenchDetail] = useState<BenchmarkRunInfo | null>(null);
  const benchDetailLoaded = useRef(false);
  const [activeSeconds, setActiveSeconds] = useState(0);
  const [afkSeconds, setAfkSeconds] = useState(0);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');

  const refresh = useCallback(async (quiet = false) => {
    if (!quiet) setLoading(true);
    try {
      const [quotaPayload, recent, beliefPayload, schedulerPayload, today, rt, dets, perf, tokens, rtm, benches, eps, qual] = await Promise.all([
        api.quotas(), api.recent('24h', 100), api.beliefs(),
        api.classificationStatus().catch(() => null),
        api.summary('today').catch(() => ({ daily: null as unknown as { active_seconds?: number; afk_seconds?: number } })),
        api.realtime().catch(() => null),
        api.detections(10).catch(() => ({ detections: [] as Detection[] })),
        api.metricsModels(30).catch(() => ({ models: [] as ModelPerfRow[] })),
        api.metricsTokens(30).catch(() => null),
        api.metricsRealtime(30).catch(() => null),
        api.benchmarks(5).catch(() => ({ runs: [] as BenchmarkRunInfo[] })),
        api.recentMeaningful(25).catch(() => ({ classifications: [] as MeaningfulVerdict[] })),
        api.feedbackQuality().catch(() => null),
      ]);
      setQuotas(quotaPayload);
      setResults(recent.activities || []);
      setEpisodes(eps.classifications || []);
      setQuality(qual);
      setBeliefs(beliefPayload.observations || []);
      setScheduler(schedulerPayload);
      setRealtime(rt);
      setDetections(dets.detections || []);
      setModelPerf(perf.models || []);
      setTokenUsage(tokens?.tokens || null);
      setRtMetrics(rtm?.realtime || null);
      setBenchRuns(benches.runs || []);
      if ((benches.runs || []).length > 0 && !benchDetailLoaded.current) {
        benchDetailLoaded.current = true;
        try { setBenchDetail((await api.benchmark(benches.runs[0].run_id || '')).run || null); } catch { /* keep list only */ }
      }
      setActiveSeconds(Number(today.daily?.active_seconds ?? 0) || 0);
      setAfkSeconds(Number(today.daily?.afk_seconds ?? 0) || 0);
      setError('');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Local API unavailable');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void refresh(); }, [refresh]);

  async function runFullCheck() {
    setBusy('run');
    setNotice('');
    try {
      const run = await api.runAi();
      setRunInfo(run);
      const evaluated = await api.evaluateBeliefs();
      setBeliefs(evaluated.observations || []);
      await refresh(true);
      setNotice('Run finished: classifications refreshed and beliefs updated.');
    } catch (err) {
      setNotice(err instanceof Error ? err.message : 'Run failed');
    } finally {
      setBusy('');
    }
  }

  async function runFastCheck() {
    setFastBusy(true);
    try {
      setFast(await api.fastCheck());
    } catch (err) {
      setFast({ verdict: 'unclear', reason: err instanceof Error ? err.message : 'Fast check failed' });
    } finally {
      setFastBusy(false);
    }
  }

  async function runRealtimePass() {
    setRtBusy(true);
    try {
      await api.evaluateRealtime();
      const [rt, dets] = await Promise.all([
        api.realtime().catch(() => null),
        api.detections(10).catch(() => ({ detections: [] as Detection[] })),
      ]);
      setRealtime(rt);
      setDetections(dets.detections || []);
    } finally {
      setRtBusy(false);
    }
  }

  async function vote(session_id: string, verdict: 'correct' | 'incorrect') {
    try {
      const saved = await api.feedback(session_id, verdict);
      setFeedback((prev) => ({ ...prev, [session_id]: saved.feedback?.verdict || verdict }));
      const qual = await api.feedbackQuality().catch(() => null);
      setQuality(qual);
    } catch (err) {
      setNotice(err instanceof Error ? err.message : 'Feedback failed');
    }
  }

  const classified = results.filter((row) => row.classification_status === 'classified');
  const bySource: Record<string, number> = {};
  for (const row of classified) {
    const key = str(row.classification_source, 'unknown');
    bySource[key] = (bySource[key] || 0) + 1;
  }

  return (
    <div className="ai-shell dark">
      <main className="ai-main results-main">
        <header className="ai-header">
          <div className="header-left"><div><div className="breadcrumb">NOEMA <span>/</span> RESULTS &amp; MODELS</div><h1>Prove it <em>works.</em></h1><p>Live pipeline verification · last 24 hours · {quotas?.day || ''}</p></div></div>
          <div className="header-actions">
            <span className={`auto-pill ${(scheduler?.enabled ?? true) ? 'on' : 'off'}`}>{(scheduler?.enabled ?? true) ? `AUTOMATIC · every ${scheduler?.intervalMinutes ?? 20} min` : 'AUTOMATIC PAUSED'}</span>
            <button className="primary-button" onClick={() => void runFullCheck()} disabled={busy === 'run'}><Play size={15} fill="currentColor" />{busy === 'run' ? 'Running…' : 'Run full check'}</button>
            <button className="ghost-button" onClick={() => void refresh()} disabled={loading}><RefreshCw size={15} className={loading ? 'spin' : ''} /> Refresh</button>
          </div>
        </header>
        {error && <div className="error-banner"><TriangleAlert size={17} /><span>{error}. Start the daemon at <code>python -m noema</code>.</span><button onClick={() => void refresh()}>Retry</button></div>}
        {notice && <div className="action-notice">{notice}</div>}

        <section className="stats-grid">
          <article className="stat-card"><div className="stat-icon blue"><BrainCircuit size={17} /></div><div className="stat-label">Provider chain</div><strong>{str(quotas?.provider, '—')}</strong><span>Ollama fallback {quotas?.ollama_fallback ? 'on' : 'off'}</span></article>
          <article className="stat-card"><div className="stat-icon green"><CheckCircle2 size={17} /></div><div className="stat-label">Classified (24h)</div><strong>{classified.length}/{results.length}</strong><span>{Object.entries(bySource).map(([key, value]) => `${key}: ${value}`).join(' · ') || 'no verdicts yet'}</span></article>
          <article className="stat-card"><div className="stat-icon violet"><Gauge size={17} /></div><div className="stat-label">Beliefs held</div><strong>{beliefs.length}</strong><span>{beliefs.filter((belief) => belief.actionable).length} actionable</span></article>
          <article className="stat-card"><div className="stat-icon cyan"><Activity size={17} /></div><div className="stat-label">Last run</div><strong>{runInfo ? `${num(runInfo.classifications)} classified` : 'not run yet'}</strong><span>{runInfo ? `pending left: ${num(runInfo.remaining_pending)}` : 'press Run full check'}</span></article>
          <article className="stat-card"><div className="stat-icon orange"><Activity size={17} /></div><div className="stat-label">Active / AFK today</div><strong>{formatDuration(activeSeconds)}</strong><span>AFK {formatDuration(afkSeconds)}</span></article>
        </section>

        <section className="panel"><div className="panel-title"><div><div className="kicker">MODEL QUOTAS · {quotas?.day || ''}</div><h2>What is left today</h2></div></div>
          {!quotas && <div className="empty">Loading quotas…</div>}
          {quotas?.note && <div className="empty">{quotas.note}</div>}
          {quotas?.openrouter_quota_note && <div className="empty">{quotas.openrouter_quota_note}</div>}
          <div className="empty">Quotas are local counters (Asia/Kolkata day{quotas?.quota_day_timezone ? `, ${quotas.quota_day_timezone}` : ''}); provider RPD resets {quotas?.provider_reset_timezone || 'on provider time'}. RPD counts successful local requests; attempts and 429s are tracked separately. Provider remaining: unavailable.</div>
          <div className="quota-grid">{(quotas?.models || []).map((row) => <QuotaCard key={row.model} row={row} />)}</div>
          {(quotas?.embeddings?.length || 0) > 0 && <><div className="panel-title" style={{ marginTop: 18 }}><div><div className="kicker">EMBEDDINGS</div><h2>Embedding quota</h2></div></div><div className="quota-grid">{(quotas?.embeddings || []).map((row) => <QuotaCard key={row.model} row={row} />)}</div></>}
        </section>

        <section className="panel"><div className="panel-title"><div><div className="kicker">MODEL PERFORMANCE · LAST 30D</div><h2>Which model, how fast, how much</h2></div></div>
          <ModelPerfTable rows={modelPerf} />
          {tokenUsage && <div className="quota-meta" style={{ marginTop: 12 }}>
            <span>Today in: {num(tokenUsage.windows?.today?.input?.exact).toLocaleString()} exact{num(tokenUsage.windows?.today?.input?.estimated) > 0 ? ` + ${num(tokenUsage.windows?.today?.input?.estimated).toLocaleString()} est` : ''}</span>
            <span>out: {num(tokenUsage.windows?.today?.output?.exact).toLocaleString()}</span>
            <span>unknown calls: {num(tokenUsage.windows?.today?.input?.unknown_calls)}</span>
            {tokenUsage.efficiency?.tokens_per_classified_session != null && <span>{num(tokenUsage.efficiency.tokens_per_classified_session).toFixed(0)} tok/classified session</span>}
            {tokenUsage.efficiency?.tokens_per_verification != null && <span>{num(tokenUsage.efficiency.tokens_per_verification).toFixed(0)} tok/verification</span>}
            {tokenUsage.batches?.batch_requests != null && num(tokenUsage.batches.batch_requests) > 0 && <span>{num(tokenUsage.batches.tokens_per_session).toFixed(0)} tok/batch session · {num((tokenUsage.batches.utilization?.mean ?? 0) * 100).toFixed(0)}% packing</span>}
            {tokenUsage.retry_fallback && (num(tokenUsage.retry_fallback.retries?.calls) + num(tokenUsage.retry_fallback.fallbacks?.calls) > 0) && <span>overhead: {num(tokenUsage.retry_fallback.retries?.tokens).toLocaleString()} retry + {num(tokenUsage.retry_fallback.fallbacks?.tokens).toLocaleString()} fallback tok</span>}
          </div>}
          {rtMetrics && num(rtMetrics.evaluations) > 0 && <div className="quota-meta" style={{ marginTop: 8 }}>
            <span>{num(rtMetrics.evaluations)} evals</span>
            <span>{num(rtMetrics.candidates)} candidates</span>
            <span>{num(rtMetrics.verifications)} verifications</span>
            <span>{num(rtMetrics.interventions_triggered)} interventions</span>
            <span>recovery {fmtRate(rtMetrics.recovery_rate)}</span>
            <span>verify {fmtMs(rtMetrics.verification_latency?.p50)}</span>
          </div>}
          <div className="empty">P95 shows n&lt;5 below 5 samples. tok/s marked ~ uses total latency (approximation); unmarked uses provider-reported generation time. Estimates are labeled est, unknowns counted, never zero-filled.</div>
        </section>

        <section className="panel"><div className="panel-title"><div><div className="kicker">BENCHMARKS</div><h2>Before vs after</h2></div></div>
          {!benchRuns.length && <div className="empty">No benchmark runs yet — run <code>python -m noema benchmark full --mock</code> (offline) or without <code>--mock</code> for live models.</div>}
          {benchRuns.length > 0 && <div className="activity-table"><div className="table-head"><span>RUN</span><span>WHEN</span><span>PURPOSE</span><span>RESULTS</span><span>DURATION</span></div>
            {benchRuns.map((run) => <div className="table-row" key={run.run_id}><span>{str(run.run_id).slice(0, 8)}</span><span>{run.timestamp ? run.timestamp.slice(0, 16).replace('T', ' ') : '—'}</span><span>{str(run.purpose)}</span><span>{num(run.results?.length)} models×fixtures</span><span>{run.duration_ms != null ? fmtMs(run.duration_ms) : '—'}</span></div>)}
          </div>}
          {benchDetail && benchRuns.length > 1 && <div className="empty">Latest {str(benchDetail.run_id).slice(0, 8)} vs previous {str(benchRuns[1]?.run_id).slice(0, 8)} — per-model deltas computed server-side from stored runs; re-run to compare revisions.</div>}
          {benchDetail && <ModelPerfTable rows={(benchDetail.results || []).map((r) => ({ provider: r.provider, model: r.model, purpose: benchDetail.purpose, requests: r.iterations, successful_requests: r.success_count, failed_requests: r.failure_count, success_rate: num(r.iterations) ? num(r.success_count) / num(r.iterations) : null, latency: { n: r.iterations, p50: r.p50_latency_ms, p95: r.p95_latency_ms }, tokens_exact_out: undefined, mean_tps: r.mean_tps }))} />}
        </section>

        <section className="panel"><div className="panel-title"><div><div className="kicker">LIVE DISTRACTION CHECK · ADVISORY ONLY</div><h2>Am I drifting right now?</h2></div><button className="ghost-button" onClick={() => void runFastCheck()} disabled={fastBusy}><Zap size={15} />{fastBusy ? 'Checking…' : 'Check now'}</button></div>
          <div className="empty">One cheap call to {str(quotas?.fast_distraction_model, 'the fast model')} on the current session. Never stored, never touches the 20-minute scheduler.</div>
          {fast && <div className="action-notice"><strong>{str(fast.verdict, 'unclear')}</strong>{fast.confidence != null && <span> · {Math.round(num(fast.confidence) * 100)}%</span>}{fast.reason && <span> — {str(fast.reason)}</span>}{fast.model && <span> · {str(fast.model)}</span>}{fast.latency_ms != null && <span> · {num(fast.latency_ms).toLocaleString()}ms</span>}</div>}
        </section>

        <section className="panel"><div className="panel-title"><div><div className="kicker">REAL-TIME DETECTOR · AUTONOMOUS</div><h2>Drift watch</h2></div><button className="ghost-button" onClick={() => void runRealtimePass()} disabled={rtBusy}><Zap size={15} />{rtBusy ? 'Evaluating…' : 'Evaluate now'}</button></div>
          <div className="empty">Rolling 60-minute behavior → local candidate score → fast-model verification only on entry → intervention only on confirmation + evidence. Fast verifier {realtime?.fast_verifier_configured ? 'configured' : 'not configured'}.</div>
          {realtime?.tracker && <div className="action-notice"><strong>{str(realtime.tracker.state, '—')}</strong>{realtime.tracker.last_score != null && <span> · score {num(realtime.tracker.last_score).toFixed(2)} (enter {num(realtime.tracker.enter_threshold).toFixed(2)} / exit {num(realtime.tracker.exit_threshold).toFixed(2)})</span>}{realtime.last_detection && <span> — last: {str(realtime.last_detection.decision)} · {str(realtime.last_detection.reason)}</span>}</div>}
          {detections.length > 0 && <div className="activity-table"><div className="table-head"><span>TIME</span><span>DECISION</span><span>SCORE</span><span>DETAIL</span><span>MODEL</span></div>
            {detections.slice(0, 10).map((row, index) => <div className="table-row" key={`${row.detection_id}-${index}`}><span>{row.timestamp ? row.timestamp.slice(11, 16) : '—'}</span><strong>{str(row.decision)}</strong><span>{row.candidate_score != null ? num(row.candidate_score).toFixed(2) : '—'}</span><span>{str(row.reason).slice(0, 90)}</span><span>{str(row.model, '—')}</span></div>)}
          </div>}
          {!detections.length && <div className="empty">No detections recorded yet.</div>}
        </section>

        <section className="panel"><div className="panel-title"><div><div className="kicker">LIVE RESULTS · EPISODES</div><h2>Semantic episodes</h2></div><span className="last-updated">{episodes.length} episodes · one model judgment each</span></div>
          <div className="activity-table episodes-7"><div className="table-head"><span>TIME</span><span>DURATION</span><span>ACTIVITY</span><span>VERDICT</span><span>EVIDENCE</span><span>MODEL</span><span>CORRECT?</span></div>
            {episodes.map((row, index) => <div className="table-row" key={`${row.session_id}-${index}`}><span>{row.start ? row.start.slice(11, 16) : '—'}</span><span>{formatDuration(row.duration_seconds)}</span><strong>{str((row.applications || [])[0])}{((row.applications || []).length || 0) > 1 ? ` +${(row.applications || []).length - 1}` : ''} · {str(row.topic)}</strong><span className="category-text">{str(row.category, row.classification_status)}</span><span>{str(row.evidence_quality)} · {Math.round(num(row.confidence) * 100)}%</span><span>{str(row.model, row.source ?? undefined)}</span><span>{row.classification_status === 'classified' && row.session_id ? <span className="feedback-pair"><button className={`mini-vote${feedback[row.session_id ?? ''] === 'correct' ? ' on' : ''}`} onClick={() => void vote(row.session_id || '', 'correct')} title="Mark correct">✓</button><button className={`mini-vote${feedback[row.session_id ?? ''] === 'incorrect' ? ' on' : ''}`} onClick={() => void vote(row.session_id || '', 'incorrect')} title="Mark incorrect">✗</button></span> : <span>—</span>}</span></div>)}
            {!episodes.length && <div className="empty">No episodes yet — the scheduler picks up pending activity automatically.</div>}
          </div>
        </section>

        <section className="panel"><div className="panel-title"><div><div className="kicker">MODEL QUALITY · HUMAN LABELS</div><h2>Was Noema right?</h2></div></div>
          {!quality || num(quality.feedback_count) === 0 ? <div className="empty">Not enough labeled data — mark episodes above as correct or incorrect.</div> :
          <div className="activity-table"><div className="table-head"><span>MODEL</span><span>LABELS</span><span>CORRECT</span><span>WRONG</span><span>ACCURACY</span></div>
            {(quality.per_model && quality.per_model.length ? quality.per_model : [{ model: quality.model || 'all', feedback_count: quality.feedback_count, correct_count: quality.correct_count, incorrect_count: quality.incorrect_count, accuracy: quality.accuracy }]).map((row, index) => <div className="table-row" key={`${row.model}-${index}`}><span><strong>{str(row.model, 'all')}</strong></span><span>{num(row.feedback_count)}</span><span>{num(row.correct_count)}</span><span>{num(row.incorrect_count)}</span><span>{row.accuracy != null ? `${Math.round(num(row.accuracy) * 100)}%` : 'not enough labeled data'}</span></div>)}
          </div>}
        </section>

        <section className="panel"><div className="panel-title"><div><div className="kicker">LIVE RESULTS · LAST 24H</div><h2>Raw rows (evidence, not verdicts)</h2></div><span className="last-updated">{classified.length} classified</span></div>
          <div className="activity-table"><div className="table-head"><span>TIME</span><span>APPLICATION</span><span>DOMAIN / TITLE</span><span>VERDICT</span><span>SOURCE</span><span>CONFIDENCE</span></div>
            {results.map((row, index) => <div className="table-row" key={`${row.session_id || row.id}-${index}`}><span>{row.timestamp ? row.timestamp.slice(11, 16) : '—'}</span><strong>{str(row.application)}</strong><span>{str(row.domain || row.title)}</span><span className="category-text">{str(row.category, row.classification_status)}{row.inherited_from ? ' · inherited' : ''}</span><span>{str(row.classification_source)}{row.evidence_quality ? ` · ${str(row.evidence_quality)}` : ''}</span><span><i className="table-signal" />{Math.round(num(row.confidence) * 100)}%</span></div>)}
            {!results.length && <div className="empty">No activity in the last 24 hours.</div>}
          </div>
        </section>

        <section className="panel"><div className="panel-title"><div><div className="kicker">BELIEFS AFTER PREDICTION</div><h2>Updated behavior state</h2></div><ShieldCheck size={18} className="safe-icon" /></div>
          <div className="activity-table"><div className="table-head"><span>SESSION</span><span>STATE</span><span>REASON</span><span>ACTIONABLE</span><span>CONFIDENCE</span></div>
            {beliefs.slice(0, 25).map((belief, index) => <div className="table-row" key={`${belief.session_id}-${index}`}><span>{str(belief.session_id).slice(0, 8)}</span><strong>{str(belief.state)}</strong><span>{str(belief.reason)}</span><span>{belief.actionable ? 'yes' : 'no'}</span><span><i className="table-signal" />{Math.round(num(belief.confidence) * 100)}%</span></div>)}
            {!beliefs.length && <div className="empty">No beliefs yet — run the full check.</div>}
          </div>
        </section>
      </main>
    </div>
  );
}
