import { useCallback, useEffect, useMemo, useState } from 'react';
import type { ReactNode } from 'react';
import { Area, AreaChart, CartesianGrid, Cell, Pie, PieChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';
import { Activity, AlertTriangle, ArrowUpRight, BarChart3, BrainCircuit, CalendarClock, CheckCircle2, ChevronRight, CircleDot, Clock, Command, ExternalLink, Gauge, Globe2, Layers3, Menu, Moon, Play, RefreshCw, Send, ShieldCheck, Sparkles, Sun, Target, Zap } from 'lucide-react';
import { api, loadCommandCenter, type CommandCenterData } from '../../api/noema';

const colors = ['#7c6cff', '#36d6ff', '#48e0a4', '#ffb86b', '#ff6b9d', '#a2a9bd'];

function formatDuration(value?: number) {
  const seconds = Number(value || 0);
  if (seconds < 60) return `${Math.round(seconds)}s`;
  return `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`;
}

function formatTime(value?: string) {
  if (!value) return '—';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value.slice(0, 16).replace('T', ' ') : date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function formatDateTime(value?: string | null) {
  if (!value) return '—';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}

function num(value: unknown, fallback = 0): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function str(value: unknown, fallback = '—'): string {
  return value == null || value === '' ? fallback : String(value);
}



function StatCard({ label, value, detail, icon: Icon, tone }: { label: string; value: string | number; detail: string; icon: typeof Activity; tone: string }) {
  return <article className="stat-card"><div className={`stat-icon ${tone}`}><Icon size={17} /></div><div className="stat-label">{label}</div><strong>{value}</strong><span>{detail}<ArrowUpRight size={12} /></span></article>;
}

function PanelTitle({ kicker, title, action }: { kicker: string; title: string; action?: ReactNode }) {
  return <div className="panel-title"><div><div className="kicker">{kicker}</div><h2>{title}</h2></div>{action}</div>;
}

function Empty({ children }: { children: ReactNode }) { return <div className="empty"><CircleDot size={18} /><span>{children}</span></div>; }

function StatusDot({ ok }: { ok: boolean | null }) {
  return <span className={`health-light ${ok ? 'online' : ''}`} aria-hidden="true" />;
}

function StateBadge({ status, productivity }: { status?: string | null; productivity?: string | null }) {
  if (status === 'classification_failed') return <span className="badge is-failed">FAILED</span>;
  if (!productivity || productivity === 'unclassified' || status === 'pending') return <span className="badge is-pending">PENDING</span>;
  return <span className="badge is-ok">CLASSIFIED</span>;
}

function VerdictBadge({ productivity, status }: { productivity?: string | null; status?: string | null }) {
  if (status === 'classification_failed' || status === 'failed') return <span className="badge is-failed">FAILED</span>;
  if (productivity === 'afk' || status === 'afk') return <span className="badge is-afk">AFK</span>;
  if (!productivity || productivity === 'unclassified' || status === 'pending') return <span className="badge is-pending">PENDING</span>;
  const label = String(productivity).toUpperCase();
  const tone = productivity === 'productive' ? 'is-productive' : productivity === 'distractive' ? 'is-distractive' : 'is-neutral';
  return <span className={`badge ${tone}`}>{label}</span>;
}

function EvidenceBadge({ quality }: { quality?: string | null }) {
  if (!quality) return null;
  const level = String(quality).toLowerCase();
  const tone = level === 'strong' ? 'is-productive' : level === 'moderate' ? 'is-neutral' : level === 'weak' ? 'is-pending' : 'is-afk';
  return <span className={`badge ${tone}`} title={`Evidence quality: ${level}`}>{level.toUpperCase()}</span>;
}

export default function AIDashboard({ onOpenResults }: { onOpenResults?: () => void }) {
  const [data, setData] = useState<CommandCenterData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);
  const [dark, setDark] = useState(true);
  const [mobileNav, setMobileNav] = useState(false);
  const [busy, setBusy] = useState('');
  const [notice, setNotice] = useState('');
  const [intent, setIntent] = useState('');
  const [category, setCategory] = useState<Record<string, unknown> | null>(null);
  const [statusFilter, setStatusFilter] = useState('all');
  const [appFilter, setAppFilter] = useState('all');

  const refresh = useCallback(async (quiet = false) => {
    if (!quiet) setLoading(true);
    try { setData(await loadCommandCenter('today')); setError(''); setLastUpdated(new Date()); } catch (err) { setError(err instanceof Error ? err.message : 'Local API unavailable'); } finally { setLoading(false); }
  }, []);
  useEffect(() => { void refresh(); const timer = window.setInterval(() => void refresh(true), 30000); return () => window.clearInterval(timer); }, [refresh]);

  const health = data?.health;
  const summary = data?.daily;
  const cls = data?.classification;
  const behaviors = useMemo(() => data?.behavior || [], [data]);
  const latestBehavior = behaviors[0];

  const providerName = cls?.provider || health?.provider || '—';
  const modelName = cls?.model || health?.provider_model || '—';
  const chainLabels = useMemo(() => {
    const names: string[] = [];
    for (const entry of health?.provider_chain || []) {
      const label = entry.provider === 'openrouter' ? 'OpenRouter' : entry.provider === 'gemini' ? 'Gemini' : entry.provider === 'ollama' ? 'Ollama' : null;
      if (label && !names.includes(label)) names.push(label);
    }
    return names;
  }, [health]);
  const autoOn = cls ? cls.enabled !== false : true;
  const intervalMinutes = cls?.intervalMinutes ?? 20;

  const totalTracked = num(summary?.total_time);
  const productiveTime = num(summary?.productive_time);
  const distractiveTime = num(summary?.distractive_time);
  const neutralTime = num(summary?.neutral_time);
  const unclassifiedTime = num(summary?.unclassified_time);
  const afkTime = num(summary?.afk_time);
  const activeTime = num(summary?.active_time, totalTracked - afkTime);
  const classifiedTime = num(summary?.classified_time, productiveTime + distractiveTime + neutralTime);
  const coverage = totalTracked > 0 ? Math.round((classifiedTime / totalTracked) * 100) : 0;
  const share = (value: number) => (classifiedTime > 0 ? Math.round((value / classifiedTime) * 100) : 0);

  const stats = [
    ["Tracked", formatDuration(totalTracked), summary?.date || "selected range", Activity, 'blue'],
    ["Active", formatDuration(activeTime), "interacted time", CheckCircle2, 'green'],
    ["AFK", formatDuration(afkTime), afkTime > 0 ? "away from keyboard" : "no idle time", Moon, 'orange'],
    ["Classified", `${coverage}%`, `${formatDuration(classifiedTime)} verdicts`, CheckCircle2, 'green'],
    ["Productive", `${share(productiveTime)}%`, formatDuration(productiveTime), Target, 'cyan'],
    ["Distractive", `${share(distractiveTime)}%`, formatDuration(distractiveTime), AlertTriangle, 'pink'],
    ["Neutral", `${share(neutralTime)}%`, formatDuration(neutralTime), CircleDot, 'violet'],
    ["Unclassified", formatDuration(unclassifiedTime), unclassifiedTime > 0 ? "awaiting AI classification" : "all activity classified", Layers3, 'orange'],
  ] as const;

  const pendingCount = num(cls?.pendingCount);
  const failedCount = num(cls?.failedCount);
  const processingCount = num(cls?.processingCount);
  const classifiedCount = num(cls?.classifiedCount);
  const totalEligible = num(cls?.totalEligibleCount);
  const queueTotal = pendingCount + processingCount + failedCount;
  const pace = num(cls?.processedLastRun) > 0 && num(cls?.lastRunDurationSeconds) > 0
    ? num(cls?.lastRunDurationSeconds) / num(cls?.processedLastRun) : 0;
  const eta = pendingCount > 0 && pace > 0 ? formatDuration(pendingCount * pace) : '—';

  const recentClassified = useMemo(() => (data?.recentMeaningful || []).filter((row) => row.classification_status === 'classified').slice(0, 10), [data]);
  const meaningfulApps = (row: { applications?: string[] }) =>
    (row.applications || []).length > 1
      ? `${row.applications?.[0]} +${(row.applications?.length || 1) - 1}`
      : (row.applications?.[0] || '—');

  const timeline = useMemo(() => summary?.timeline || [], [summary]);
  const appOptions = useMemo(() => Array.from(new Set(timeline.map((segment) => segment.application).filter(Boolean))).sort(), [timeline]);
  const segmentState = (segment: { productivity?: string | null; classification_status?: string }) =>
    segment.classification_status === 'classification_failed' ? 'failed'
      : segment.productivity === 'afk' ? 'afk'
        : (!segment.productivity || segment.productivity === 'unclassified') ? 'pending' : segment.productivity;
  const visibleSegments = timeline.filter((segment) => {
    const status = segmentState(segment);
    if (statusFilter !== 'all' && status !== statusFilter) return false;
    if (appFilter !== 'all' && segment.application !== appFilter) return false;
    return true;
  }).slice().reverse();

  const behaviorByState = useMemo(() => {
    const counts: Record<string, number> = {};
    for (const observation of behaviors) {
      const key = String(observation.state || 'UNKNOWN');
      counts[key] = (counts[key] || 0) + 1;
    }
    return counts;
  }, [behaviors]);
  const behaviorEvidence = Object.entries(behaviorByState).map(([state, count]) => `${count} ${state}`).join(' · ');

  const pulse = useMemo(() => (summary?.timeline || []).map((segment, index) => ({ index, time: formatTime(segment.timestamp), activity: Math.max(3, Math.min(100, segment.seconds * 2 + 8)), signal: segment.productivity === 'unclassified' ? 3 : Math.max(5, Math.min(100, segment.confidence * 100)) })), [summary?.timeline]);
  const categoryData = [
    { name: 'Productive', value: productiveTime },
    { name: 'Neutral', value: neutralTime },
    { name: 'Distractive', value: distractiveTime },
    { name: 'Unclassified', value: unclassifiedTime },
    { name: 'AFK', value: afkTime },
  ].filter(item => item.value > 0);

  const presence = data?.presence || cls?.presence || null;
  const presenceState = str(presence?.state, 'unknown');
  const idleSeconds = presence?.idle_seconds;
  const idleText = idleSeconds == null ? 'unknown idle'
    : idleSeconds < 60 ? `${Math.round(idleSeconds)}s ago` : `${Math.floor(idleSeconds / 60)}m ${Math.round(idleSeconds % 60)}s ago`;
  const currentSession = presence?.current_session || null;

  const collectorOk = health?.source_connected !== false;
  const classifierOk = cls ? cls.status !== 'error' : null;
  const providerQuota = cls?.quota;
  const providerOk = providerQuota && num(providerQuota.requestsRemaining) === 0 ? false : null;
  const dbOk = !error;

  async function runAction(name: string, action: () => Promise<unknown>, success: string) { setBusy(name); setNotice(''); try { await action(); setNotice(success); await refresh(true); } catch (err) { setNotice(err instanceof Error ? err.message : 'Action failed'); } finally { setBusy(''); } }
  async function runNow() { await runAction('run', api.runAi, 'Manual classification pass finished. The scheduler continues automatically.'); }
  async function categorize() { const registration = data?.registrations?.[0]; if (!registration?.current_window_id || !registration.current_tab_id) { setNotice('Firefox bridge has not reported an active tab yet.'); return; } setBusy('category'); try { setCategory(await api.categorize(registration)); setNotice('Tab categorized.'); } catch (err) { setNotice(err instanceof Error ? err.message : 'Categorization failed'); } finally { setBusy(''); } }
  async function saveIntent() { const value = intent.trim(); if (!value) return; await runAction('intent', () => api.intent(value), 'Intent saved locally and available to the alignment engine.'); setIntent(''); }

  return <div className={`ai-shell ${dark ? 'dark' : 'light'} ${mobileNav ? 'nav-open' : ''}`}>
    <main className="ai-main" id="overview"><div className="topbar"><div className="ai-wordmark"><strong>NOEMA</strong><small>PERSONAL BEHAVIORAL INTELLIGENCE</small></div><button className="menu-button topbar-toggle" onClick={() => setMobileNav(value => !value)} aria-label="Toggle navigation" aria-expanded={mobileNav}><Menu size={19} /></button><nav className="topbar-nav" aria-label="Dashboard sections"><a href="#overview" onClick={() => setMobileNav(false)}><Command size={14} /><span>Command Center</span></a><a href="#pipeline" onClick={() => setMobileNav(false)}><BrainCircuit size={14} /><span>AI Pipeline</span></a><a href="#behavior" onClick={() => setMobileNav(false)}><Gauge size={14} /><span>Behavior</span></a><a href="#activity" onClick={() => setMobileNav(false)}><Activity size={14} /><span>Activity</span></a><a href="#results" onClick={(event) => { event.preventDefault(); setMobileNav(false); onOpenResults?.(); }}><BarChart3 size={14} /><span>Results &amp; Models</span></a></nav></div><header className="ai-header"><div className="header-left"><button className="menu-button" onClick={() => setMobileNav(true)} aria-label="Open navigation"><Menu size={19} /></button><div><div className="breadcrumb">NOEMA <span>/</span> COMMAND CENTER</div><h1>Activity, <em>made legible.</em></h1><p>{summary?.date || 'Today'} · {summary?.timezone || 'Asia/Kolkata'} · <span className={`auto-pill ${autoOn ? 'on' : 'off'}`}>{autoOn ? `AUTOMATIC · every ${intervalMinutes} min` : 'AUTOMATIC PAUSED'}</span></p></div></div><div className="header-actions"><div className={`connection ${error ? 'offline' : ''}`}><i />{error ? 'API offline' : loading ? 'Connecting' : 'Local API connected'}</div><button className="round-button" onClick={() => void refresh()} disabled={loading} title="Refresh" aria-label="Refresh dashboard"><RefreshCw size={17} className={loading ? 'spin' : ''} /></button><button className="round-button" onClick={() => setDark(value => !value)} title="Toggle theme" aria-label="Toggle theme">{dark ? <Sun size={17} /> : <Moon size={17} />}</button><div className="user-avatar">W</div></div></header>
      {error && <div className="error-banner"><AlertTriangle size={17} /><span>{error}. Start the daemon at <code>python -m noema</code>.</span><button onClick={() => void refresh()}>Retry</button></div>}

      <section className="stats-grid" aria-label="Today summary">{stats.map(([label, value, detail, Icon, tone]) => <StatCard key={label} label={label} value={value} detail={detail} icon={Icon} tone={tone} />)}</section>

      <section className="dashboard-grid top-grid" id="pipeline">
        <article className="panel"><PanelTitle kicker="AI CLASSIFICATION" title="Autonomous pipeline" action={<span className={`auto-pill ${autoOn ? 'on' : 'off'}`}>{autoOn ? '● AUTOMATIC' : '○ MANUAL ONLY'}</span>} />
          <div className="classify-progress"><div className="classify-counts"><strong>{classifiedCount} / {totalEligible} classified</strong><span>{cls?.coveragePercent ?? coverage}%</span></div><div className="progress-track" role="progressbar" aria-valuenow={cls?.coveragePercent ?? coverage} aria-valuemin={0} aria-valuemax={100} aria-label="Classification coverage"><i style={{ width: `${cls?.coveragePercent ?? coverage}%` }} /></div></div>
          <div className="queue-lines"><span><b>{pendingCount} pending</b> · {processingCount} processing · {failedCount} failed</span><span>ETA next completion: {eta}</span></div>
          <div className="run-meta"><span><Clock size={12} /> Last run {formatDateTime(cls?.lastRunAt)}{num(cls?.lastRunDurationSeconds) > 0 ? ` · took ${formatDuration(num(cls?.lastRunDurationSeconds))}` : ''}</span><span><CalendarClock size={12} /> Next run {formatDateTime(cls?.nextRunAt)}</span></div>
          <div className="run-meta"><span>Interval {intervalMinutes} min</span><span>Model {modelName}</span><span>Provider {providerName}</span></div>
          {chainLabels.length > 0 && <div className="run-meta"><span>Chain {chainLabels.join(' → ')}</span></div>}
          {cls?.lastError && <div className="quota-error">Last error: {cls.lastError}</div>}
          <div className="hero-actions" style={{ marginTop: 12 }}><button className="ghost-button" onClick={() => void runNow()} disabled={busy === 'run'}><Play size={14} />{busy === 'run' ? 'Running…' : 'Run now (manual override)'}</button>{onOpenResults && <button className="ghost-button" onClick={() => onOpenResults()}>Queue details <ChevronRight size={13} /></button>}</div>
          {notice && <div className="action-notice">{notice}</div>}
        </article>
        <article className="panel"><PanelTitle kicker="SYSTEM STATUS" title="Runtime health" action={<span className={`health-light ${health?.status === 'running' ? 'online' : ''}`} />} />
          <div className="status-list">
            <div><StatusDot ok={collectorOk} /><span>Collector</span><b>{collectorOk ? 'Healthy · Telemetry source connected' : 'Unhealthy · Telemetry source unreachable'}</b></div>
            <div><StatusDot ok={classifierOk} /><span>Classifier</span><b>{cls?.status === 'error' ? `Error · ${cls?.lastError || 'see logs'}` : cls?.status === 'running' ? 'Running a cycle now' : autoOn ? `Healthy · automatic every ${intervalMinutes} min` : 'Paused · automatic scheduling off'}</b></div>
            <div><StatusDot ok={dbOk} /><span>Database</span><b>{dbOk ? 'Healthy · local SQLite responding' : `Unhealthy · ${error}`}</b></div>
            <div><StatusDot ok={providerOk} /><span>Provider</span><b>{providerQuota && num(providerQuota.requestsRemaining) === 0 ? `Exhausted · ${providerQuota.model} daily quota spent, work stays pending` : `Healthy · ${modelName} via ${providerName}`}</b></div>
          </div>
          <div className="health-metrics"><span><b>{queueTotal}</b> queue depth</span><span><b>{formatDateTime(cls?.latestClassificationAt)}</b> latest verdict</span><span><b>{Object.values(health?.workers || {}).filter(worker => worker.status === 'ok').length}</b> workers healthy</span></div>
        </article>
      </section>

      <section className="dashboard-grid top-grid"><article className="panel"><PanelTitle kicker="USER PRESENCE" title={presenceState === 'active' ? 'Active now' : presenceState === 'afk' ? 'Away' : 'Unknown'} action={<StatusDot ok={presenceState === 'active' ? true : presenceState === 'afk' ? false : null} />} /><div className="focus-body"><div className="focus-copy"><strong>{presenceState === 'active' ? '● ACTIVE' : presenceState === 'afk' ? '◐ AFK' : '○ UNKNOWN'}</strong><p>{presenceState === 'active' ? `Last input ${idleText}. Input timing only — window focus is never presence.` : presenceState === 'afk' ? `No input for ${idleText}. Idle time is excluded from classification.` : 'No presence signal right now.'}</p><div className="signal-bar"><i style={{ width: presenceState === 'active' ? '100%' : presenceState === 'afk' ? '8%' : '0%' }} /></div><small>{presence?.stale ? 'Presence data stale' : `As of ${formatTime(presence?.as_of)}`}</small></div></div></article><article className="panel"><PanelTitle kicker={presenceState === 'afk' ? 'CURRENT STATE' : 'CURRENT SESSION'} title={presenceState === 'afk' ? 'Away from keyboard' : 'What you are doing'} action={<span className="live-label"><i />{presenceState === 'afk' ? 'IDLE' : 'LIVE'}</span>} /><div className="focus-body"><div className="focus-copy">{currentSession ? <><strong>{currentSession.application || 'Session'}</strong><p>{currentSession.context || 'No topic yet'}</p><div className="signal-bar"><i style={{ width: `${Math.round(num(currentSession.classification?.confidence) * 100)}%` }} /></div><small>Active {formatDuration(currentSession.active_duration_seconds)} · {currentSession.classification ? `${String(currentSession.classification.category).toUpperCase()} · ${Math.round(num(currentSession.classification.confidence) * 100)}%${currentSession.classification.evidence_quality ? ` · ${currentSession.classification.evidence_quality}` : ''}` : 'not classified yet'}</small></> : presenceState === 'afk' ? <><strong>AFK{presence?.afk_since ? ` since ${formatTime(presence.afk_since)}` : ''}</strong><p>Last activity: {presence?.last_activity?.app || '—'}{presence?.last_activity?.title ? ` — ${presence.last_activity.title}` : ''}</p><div className="signal-bar"><i style={{ width: '8%' }} /></div><small>Idle {idleText} · idleness is never labeled productive</small></> : <><strong>No active session</strong><p>Waiting for activity.</p></>}</div></div></article></section>

      <section className="panel"><PanelTitle kicker="RECENT AI CLASSIFICATIONS" title="Latest verdicts" action={<span className="last-updated">{lastUpdated ? `Updated ${lastUpdated.toLocaleTimeString()}` : 'Updating…'}</span>} />
        <div className="activity-table"><div className="table-head"><span>TIME</span><span>APPLICATION</span><span>DOMAIN / TITLE</span><span>VERDICT</span><span>SOURCE</span><span>CONFIDENCE</span></div>
          {recentClassified.map((row, index) => <div className="table-row" key={`${row.session_id}-${index}`}><span>{formatTime(row.start)}–{formatTime(row.end)}</span><strong>{meaningfulApps(row)}</strong><span>{row.topic || '—'}</span><span><VerdictBadge productivity={row.category} status={row.classification_status} /></span><span>{row.source || '—'} <EvidenceBadge quality={row.evidence_quality} /></span><span><i className="table-signal" />{Math.round(num(row.confidence) * 100)}%</span></div>)}
          {!recentClassified.length && <Empty>No classifications yet — the scheduler picks up pending activity automatically.</Empty>}
        </div>
      </section>

      <section className="dashboard-grid top-grid"><article className="panel focus-panel" id="behavior"><PanelTitle kicker="BEHAVIORAL SIGNAL" title="Focus state" action={<span className="live-label"><i />EVIDENCE</span>} /><div className="focus-body"><div className="focus-gauge"><div className="gauge-track"><div className="gauge-fill" style={{ transform: 'rotate(-120deg)' }} /></div><div className="gauge-center"><strong>{latestBehavior?.state || '—'}</strong><small>{latestBehavior ? 'ESTIMATED' : 'NOT ESTIMATED'}</small></div></div><div className="focus-copy"><strong>{latestBehavior ? String(latestBehavior.state) : 'Not enough evidence'}</strong><p>{latestBehavior?.reason || 'Focus is not estimated until behavior evidence exists.'}</p><div className="signal-bar"><i style={{ width: `${Math.round(num(latestBehavior?.confidence) * 100)}%` }} /></div><small>Tracked activity: {formatDuration(totalTracked)}{behaviorEvidence ? ` · ${behaviorEvidence}` : ''}</small></div></div></article><article className="panel browser-panel"><PanelTitle kicker="BROWSER BRIDGE" title="Current tab intelligence" action={<button className="panel-action" onClick={() => void categorize()} disabled={busy === 'category'}><Sparkles size={13} />{busy === 'category' ? 'Thinking…' : 'Categorize'}</button>} />{data?.registrations?.[0]?.current_tab_id ? <div className="browser-card"><div className="browser-icon"><Globe2 size={20} /></div><div className="browser-details"><strong>{data.registrations[0].browser || 'Firefox'} active tab</strong><span>Device {data.registrations[0].device_id} · Window {data.registrations[0].current_window_id} · Tab {data.registrations[0].current_tab_id}</span>{category && <b>{String(category.category || category.topic || category.status || 'categorized')} <small>· {String(category.provider || providerName || 'hosted_chain')}</small></b>}</div><ChevronRight size={17} className="muted-icon" /></div> : <Empty>Reload the Firefox bridge to stream exact tab identity.</Empty>} {notice && <div className="action-notice">{notice}</div>}</article></section>

      <section className="dashboard-grid charts-grid"><article className="panel chart-panel"><PanelTitle kicker="ACTIVITY TELEMETRY" title="Today's pulse" action={<div className="chart-legend"><span><i className="blue-dot" />captured</span><span><i className="violet-dot" />signal</span><b>LIVE ↗</b></div>} /><div className="chart-box">{pulse.length ? <ResponsiveContainer width="100%" height="100%"><AreaChart data={pulse}><defs><linearGradient id="activityFill" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stopColor="#36d6ff" stopOpacity={.35} /><stop offset="100%" stopColor="#36d6ff" stopOpacity={0} /></linearGradient><linearGradient id="signalFill" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stopColor="#7c6cff" stopOpacity={.3} /><stop offset="100%" stopColor="#7c6cff" stopOpacity={0} /></linearGradient></defs><CartesianGrid stroke="#ffffff08" vertical={false} /><XAxis dataKey="time" tick={{ fill: '#68758e', fontSize: 9 }} tickLine={false} axisLine={false} minTickGap={30} /><YAxis hide domain={[0, 100]} /><Tooltip contentStyle={{ background: '#161d2d', border: '1px solid #2b3854', borderRadius: 10, color: '#fff', fontSize: 11 }} /><Area type="monotone" dataKey="activity" stroke="#36d6ff" strokeWidth={2} fill="url(#activityFill)" /><Area type="monotone" dataKey="signal" stroke="#7c6cff" strokeWidth={2} fill="url(#signalFill)" /></AreaChart></ResponsiveContainer> : <Empty>No activity pulse yet.</Empty>}</div></article><article className="panel distribution-panel"><PanelTitle kicker="SEMANTIC LENS" title="Activity mix" action={<BarChart3 size={17} className="muted-icon" />} /><div className="donut-wrap">{categoryData.length ? <><ResponsiveContainer width="55%" height={160}><PieChart><Pie data={categoryData} dataKey="value" nameKey="name" innerRadius={48} outerRadius={70} paddingAngle={4} stroke="none">{categoryData.map((entry, index) => <Cell key={entry.name} fill={colors[index % colors.length]} />)}</Pie><Tooltip contentStyle={{ background: '#161d2d', border: '1px solid #2b3854', borderRadius: 10, color: '#fff', fontSize: 11 }} /></PieChart></ResponsiveContainer><div className="category-legend">{categoryData.map((entry, index) => <div key={entry.name}><i style={{ background: colors[index % colors.length] }} /><span>{entry.name}</span><b>{formatDuration(entry.value)}</b></div>)}</div></> : <Empty>No classified activity yet.</Empty>}</div></article></section>

      <section className="panel activity-panel" id="activity"><PanelTitle kicker="DATE-SCOPED EVIDENCE" title="Today's activity" action={<span className="last-updated">{lastUpdated ? `Updated ${lastUpdated.toLocaleTimeString()}` : 'Updating…'}</span>} />
        <div className="filter-row"><label>State <select aria-label="Filter by classification state" value={statusFilter} onChange={(event) => setStatusFilter(event.target.value)}><option value="all">All states</option><option value="productive">Productive</option><option value="distractive">Distractive</option><option value="neutral">Neutral</option><option value="pending">Pending</option><option value="failed">Failed</option><option value="afk">AFK</option></select></label><label>Application <select aria-label="Filter by application" value={appFilter} onChange={(event) => setAppFilter(event.target.value)}><option value="all">All applications</option>{appOptions.map((name) => <option key={name} value={name}>{name}</option>)}</select></label></div>
        <div className="activity-table"><div className="table-head"><span>TIME</span><span>APPLICATION</span><span>DOMAIN / TITLE</span><span>DURATION</span><span>CLASSIFICATION</span><span>CONFIDENCE</span><span>STATUS</span></div>{visibleSegments.map((segment, index) => <div className="table-row" key={`${segment.timestamp}-${segment.application}-${index}`}><span>{formatTime(segment.timestamp)}</span><strong>{segment.application}</strong><span>{segment.domain || segment.title || segment.activity || '—'}</span><span>{formatDuration(segment.seconds)}</span><span><VerdictBadge productivity={segment.productivity} status={segment.classification_status} /></span><span><i className="table-signal" />{Math.round(segment.confidence * 100)}%{segment.evidence_quality ? ` · ${segment.evidence_quality}` : ''}</span><span><StateBadge productivity={segment.productivity} status={segment.classification_status} /></span></div>)}{!visibleSegments.length && <Empty>No activity matches these filters.</Empty>}</div></section>

      <section className="dashboard-grid mid-grid"><article className="panel ai-panel" id="intelligence"><PanelTitle kicker="INTELLIGENCE LAYER" title="Provider stack" action={<span className="model-pill">{modelName}</span>} /><div className="ai-layers"><div><BrainCircuit size={18} /><span>Semantic classification<small>Meaningful sessions → categories · automatic every {intervalMinutes} min</small></span><b>{autoOn ? 'ACTIVE' : 'PAUSED'}</b></div><div><Target size={18} /><span>Intent alignment<small>Goal-aware behavior signals</small></span><b>READY</b></div><div><Zap size={18} /><span>Intervention policy<small>AI never triggers action directly</small></span><b>GUARDED</b></div></div><div className="ai-footer"><div className="ai-wave"><i /><i /><i /><i /><i /><i /><i /></div><span>Verdicts: {providerName}{providerName === 'hosted_chain' ? ' · cloud quota-guarded' : ''}</span><ShieldCheck size={15} /></div></article><article className="panel security-panel"><PanelTitle kicker="TRUST CENTER" title="Privacy & safeguards" action={<ShieldCheck size={18} className="safe-icon" />} /><div className="safe-list"><div><CheckCircle2 /><span>SQLite data store<small>Stored on this computer</small></span></div><div><CheckCircle2 /><span>{providerName === 'hosted_chain' ? 'Hybrid inference' : 'Local Ollama inference'}<small>{providerName === 'hosted_chain' ? 'Hosted models with quota guardrails' : 'No API key or cloud request'}</small></span></div><div><CheckCircle2 /><span>Exact tab targeting<small>Device + window + tab identity</small></span></div><div><CheckCircle2 /><span>Policy gate<small>Classification cannot trigger actions</small></span></div></div></article></section>

      <section className="panel intent-panel" id="intent"><div className="intent-symbol"><Sparkles size={20} /></div><div className="intent-copy"><div className="kicker">DIRECT THE ENGINE</div><h2>What are you working toward?</h2><p>Your goal is stored locally and used for alignment.</p></div><div className="intent-form"><input value={intent} aria-label="Work intent" onChange={(event) => setIntent(event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter') void saveIntent(); }} placeholder="e.g. Finish the project documentation" /><button className="primary-button" onClick={() => void saveIntent()} disabled={busy === 'intent' || !intent.trim()}>{busy === 'intent' ? 'Saving…' : 'Set intent'}<Send size={14} /></button></div></section>
      <footer className="ai-footer-global"><span>NOEMA / LOCAL-FIRST</span><span><a href="http://127.0.0.1:5600/" target="_blank" rel="noreferrer">COLLECTOR <ExternalLink size={11} /></a>{providerName === 'ollama' ? <a href="http://127.0.0.1:11434" target="_blank" rel="noreferrer">OLLAMA <ExternalLink size={11} /></a> : <span>{str(providerName).toUpperCase()} · {str(modelName)}</span>}<span>LOCALHOST ONLY</span></span></footer>
    </main>
  </div>;
}
