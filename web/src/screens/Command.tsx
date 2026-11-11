import { useMemo, useRef, useState } from 'react';
import { liveApi } from '../api/noema';
import { useLive } from '../hooks/useLive';
import { useAnimClock } from '../hooks/useAnimClock';
import {
  axisFor, fmtClock, place, STAMP, toSegs, driftView, type Kind,
} from '../lib/adapt';
import { RopeViz, type RopeEpisode } from '../components/viz';
import { demoDrive, useLiveDrive, type Drive } from '../components/drive';
import { Banner, Empty, Hatch, Stamp } from '../components/ui';
import { DaemonOffline } from './States';

const BG: Record<Kind, string> = {
  aligned: 'var(--intent)',
  drifting: 'repeating-linear-gradient(135deg,#FF5B1F 0 18px,#C9420E 18px 26px)',
  unknown: 'radial-gradient(#161411 2px,transparent 2.6px) 0 0/12px 12px,#F3ECDD',
  afk: 'repeating-linear-gradient(45deg,#C9C0AE 0 10px,#F3ECDD 10px 20px)',
};

const DEMO_EPISODES: RopeEpisode[] = [
  { app: 'Notion', title: 'lecture notes', kind: 'aligned' }, { app: 'VS Code', title: 'train.py', kind: 'aligned' },
  { app: 'Slack', title: '#study-group', kind: 'unknown' }, { app: 'Chrome', title: 'sklearn docs', kind: 'aligned' },
  { app: 'YouTube', title: 'F1 overtakes', kind: 'drifting' }, { app: 'YouTube', title: 'autoplay ×3', kind: 'drifting' },
];

function ago(iso: string, now = Date.now()): string {
  const mins = Math.max(0, Math.round((now - new Date(iso).getTime()) / 60000));
  return mins < 60 ? `${mins}m ago` : `${Math.floor(mins / 60)}h ${String(mins % 60).padStart(2, '0')}m ago`;
}

export default function Command({ demo = false }: { demo?: boolean }) {
  const live = useLive();
  const nowMs = useAnimClock();

  const drift = driftView(live.realtime);
  const lastNudge = live.feed[0];
  const freshNudge = lastNudge?.created_at && Date.now() - new Date(lastNudge.created_at).getTime() < 120_000 ? lastNudge.id : null;
  const liveDrive = useLiveDrive(drift.score, freshNudge);
  const drive: Drive = demo ? demoDrive(nowMs) : liveDrive;

  const segs = useMemo(() => toSegs(live.today?.timeline), [live.today]);
  const axis = useMemo(() => axisFor(segs), [segs]);
  const placed = useMemo(() => place(segs, axis), [segs, axis]);
  const ropeEpisodes: RopeEpisode[] = demo
    ? DEMO_EPISODES.filter((e) => e.kind !== 'drifting' || drive.t >= 7.6)
    : segs.filter((s) => s.kind !== 'afk').slice(-6).map((s) => ({ app: s.app, title: s.title, kind: s.kind }));

  const modelFailure = !!live.health && (live.health.provider_health === 'unavailable' || live.health.gemini_configured === false);

  if (!demo && live.offline) return <DaemonOffline since={live.offlineSince} onRetry={() => void live.refresh()} />;
  if (!demo && !live.loaded) return <div className="page-body"><Hatch w="70%" h={90} /><Hatch w="100%" h={300} /><div style={{ fontSize: 32 }}>listening to the daemon…</div></div>;
  if (!demo && !live.intent && !segs.length) {
    return (
      <div className="page-body">
        <IntentRow intent={null} onSave={async (t) => { await liveApi.setIntent(t); await live.refresh(); }} />
        <Empty title="No rope yet." body="Noema starts listening once you tell it what you're here to do." />
      </div>
    );
  }

  const score = demo ? drive.d : drift.score;
  // One plain word. Thresholds come from the daemon (enter = off task, release = back on task).
  const enterAt = demo ? 0.6 : drift.enter, exitAt = demo ? 0.45 : drift.exit;
  const level = score === null ? 'on' : score >= enterAt ? 'off' : score >= exitAt ? 'wander' : 'on';
  const recent = segs.filter((x) => x.kind === 'drifting' && Date.now() - x.end.getTime() < 20 * 60_000);
  const offMin = Math.round(recent.reduce((t, x) => t + x.minutes, 0));
  const offApp = recent.length ? [...recent].sort((x, y) => y.minutes - x.minutes)[0].app : '';
  const status = demo ? demoStatus(drive.d, drive.rec) : {
    on: { label: 'ON TASK', style: { background: 'var(--intent)', color: 'var(--paper)' }, bar: 'var(--intent)',
      line: score === null ? 'Watching. Nothing to judge yet.' : 'You look like you are doing what you said.' },
    wander: { label: 'WANDERING', style: { background: 'var(--paper-hi)', color: 'var(--ink)' }, bar: 'var(--muted)',
      line: offMin ? `${offMin} min off-task lately${offApp ? `, mostly ${offApp}` : ''}. Still okay.` : 'Drifting a little. Still okay.' },
    off: { label: 'OFF TASK', style: { background: 'var(--drift)', color: 'var(--ink)' }, bar: 'var(--drift)',
      line: offMin ? `${offMin} min off-task${offApp ? `, mostly ${offApp}` : ''}. Back to: ${live.intent?.text ?? 'your goal'}.` : `Back to: ${live.intent?.text ?? 'your goal'}.` },
  }[level];

  const ticker = buildTicker(segs, live.feed);
  const nudgeMarks = live.feed.filter((f) => f.delivered && f.created_at).map((f) => ({
    id: f.id, left: ((new Date(f.created_at as string).getHours() * 60 + new Date(f.created_at as string).getMinutes() - axis.start) / (axis.end - axis.start)) * 100,
    at: fmtClock(f.created_at),
  })).filter((m) => m.left >= 0 && m.left <= 100);
  const nowLeft = ((new Date().getHours() * 60 + new Date().getMinutes() - axis.start) / (axis.end - axis.start)) * 100;

  return (
    <div className="page-body" style={{ paddingBottom: 220 }}>
      {demo && <Banner>DEMO LOOP — synthetic data. Open Command for your live rope.</Banner>}
      {modelFailure && !demo && <Banner>Gemini unavailable, showing deterministic results only</Banner>}
      <IntentRow
        intent={demo ? { text: 'finish the ML assignment', created_at: new Date(Date.now() - 5.8 * 3600_000).toISOString() } : live.intent}
        onSave={async (t) => { await liveApi.setIntent(t); await live.refresh(); }}
      />

      <div style={{ display: 'flex', gap: 60, height: 1000 }}>
        <div style={{ position: 'relative', flex: 1, minWidth: 0 }}>
          <div className="halftone-shadow" />
          <div style={{ position: 'relative', height: '100%', background: '#FBF6EA', border: '5px solid var(--ink)', padding: '36px 40px', display: 'flex', flexDirection: 'column', transform: 'rotate(-0.4deg)' }}>
            <div style={{ display: 'flex', alignItems: 'baseline', justifyContent: 'space-between' }}>
              <div style={{ display: 'flex', alignItems: 'baseline', gap: 30 }}>
                <div className="label" style={{ fontSize: 30 }}>THE ROPE · TODAY</div>
                <div className="aside" style={{ fontSize: 48 }}>Slack is good. Taut is not.</div>
              </div>
              <div style={{ display: 'flex', gap: 30, fontSize: 24, letterSpacing: '.08em' }}>
                <Stamp kind="aligned" /><Stamp kind="unknown">? UNKNOWN · never drift</Stamp><Stamp kind="drifting" />
              </div>
            </div>
            <div style={{ flex: 1, minHeight: 0, display: 'flex', alignItems: 'center' }}>
              <RopeViz drive={drive} episodes={ropeEpisodes} />
            </div>
          </div>
          <div className="tape" style={{ top: -26, left: 300, width: 200, height: 58, transform: 'rotate(-4deg)' }} />
          <div className="tape" style={{ top: -22, right: 260, width: 170, height: 54, transform: 'rotate(3deg)' }} />
        </div>

        <div style={{ width: 1040, display: 'flex', flexDirection: 'column', gap: 40 }}>
          <div className="card" style={{ flex: 1, minHeight: 0, padding: '44px 48px', display: 'flex', flexDirection: 'column', gap: 28, transform: 'rotate(0.5deg)' }} aria-live="polite">
            <div className="label">RIGHT NOW</div>
            <div className="display" style={{ alignSelf: 'flex-start', ...status.style, border: '6px solid var(--ink)', fontSize: 120, letterSpacing: 0, padding: '6px 36px', transform: 'rotate(-2deg)', boxShadow: '10px 10px 0 var(--ink)' }}>{status.label}</div>
            <div className="aside" style={{ fontSize: 52, lineHeight: 1.1 }}>{status.line}</div>
            <div style={{ marginTop: 'auto' }}>
              <div className="label" style={{ fontSize: 22, display: 'flex', justifyContent: 'space-between', marginBottom: 10 }}><span>ON TASK</span><span>OFF TASK</span></div>
              <div style={{ position: 'relative', height: 44, border: '5px solid var(--ink)', background: 'var(--paper)' }} role="img" aria-label={`Distraction ${Math.round((score ?? 0) * 100)} percent`}>
                <div style={{ height: '100%', width: `${Math.round((score ?? 0) * 100)}%`, background: status.bar, transition: 'width .6s' }} />
                <div style={{ position: 'absolute', top: -14, bottom: -14, left: `${Math.round((demo ? 0.6 : drift.enter) * 100)}%`, width: 6, background: 'var(--ink)' }} />
              </div>
            </div>
          </div>
        </div>
      </div>

      <div className="card flat" style={{ padding: '30px 40px 26px', display: 'flex', flexDirection: 'column', gap: 18, boxShadow: 'none' }}>
        <div className="label" style={{ display: 'flex', justifyContent: 'space-between' }}>
          <div>TODAY'S EPISODES</div>
          <div>{fmtClock(new Date(2000, 0, 1, Math.floor(axis.start / 60)))} — {fmtClock(new Date(2000, 0, 1, Math.floor(axis.end / 60) % 24))} · now {fmtClock(new Date())}</div>
        </div>
        <div style={{ position: 'relative', height: 96, border: '4px solid var(--ink)', background: 'var(--paper)' }}>
          {placed.map((s, i) => (
            <div key={i} title={`${s.app} ${s.title}`} style={{ position: 'absolute', top: 0, bottom: 0, left: `${s.left}%`, width: `${s.width}%`, background: BG[s.kind], borderRight: '3px solid var(--ink)' }} />
          ))}
          {nowLeft >= 0 && nowLeft <= 100 && <div style={{ position: 'absolute', top: -24, bottom: -24, left: `${nowLeft}%`, width: 6, background: 'var(--ink)' }} />}
          {nudgeMarks.map((m) => (
            <div key={m.id} className="stamp drifting" style={{ position: 'absolute', top: -34, left: `${m.left}%`, transform: 'translateX(-50%) rotate(-4deg)', fontSize: 22, padding: '4px 10px' }}>NUDGE {m.at}</div>
          ))}
        </div>
        <div style={{ position: 'relative', height: 60 }}>
          {placed.filter((s) => s.width > 4).map((s, i) => (
            <div key={i} style={{ position: 'absolute', left: `${s.left}%`, width: `${s.width}%`, fontSize: 22, lineHeight: 1.3, overflow: 'hidden', whiteSpace: 'nowrap', paddingLeft: 8 }}>
              {s.app}<br />{STAMP[s.kind]}
            </div>
          ))}
        </div>
      </div>

      <Ticker items={ticker} />
    </div>
  );
}

function IntentRow({ intent, onSave }: { intent: { text: string; created_at: string } | null; onSave: (t: string) => Promise<void> }) {
  const ref = useRef<HTMLSpanElement>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const save = async () => {
    const text = (ref.current?.textContent ?? '').trim();
    if (!text || text === intent?.text) return;
    setBusy(true); setErr(null);
    try { await onSave(text); } catch (e) { setErr(e instanceof Error ? e.message : 'could not save'); } finally { setBusy(false); }
  };
  return (
    <div style={{ display: 'flex', alignItems: 'flex-end', justifyContent: 'space-between', gap: 60 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 44 }}>
        <svg width="150" height="170" viewBox="0 0 150 170" aria-hidden="true"><circle cx="75" cy="28" r="18" fill="none" stroke="#161411" strokeWidth="10" /><path d="M75 46 V150 M40 76 H110 M18 110 C22 150 60 160 75 160 C90 160 128 150 132 110" fill="none" stroke="#2B4BFF" strokeWidth="12" strokeLinecap="round" /><path d="M75 46 V150 M18 110 C22 150 60 160 75 160 C90 160 128 150 132 110" fill="none" stroke="#161411" strokeWidth="4" strokeLinecap="round" transform="translate(5 4)" /></svg>
        <div className="display" style={{ fontSize: 156, letterSpacing: '-0.045em' }}>
          I'm here to{' '}
          <span
            ref={ref} key={intent?.text ?? 'none'} contentEditable suppressContentEditableWarning role="textbox" aria-label="Today's intent"
            onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); void save(); (e.target as HTMLElement).blur(); } }}
            style={{ color: 'var(--intent)', borderBottom: '10px dashed var(--intent)', textShadow: 'var(--misregister-orange)', outline: 'none', opacity: busy ? 0.5 : 1 }}
          >{intent?.text ?? 'set an intent…'}</span>
        </div>
      </div>
      <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-end', gap: 18, paddingBottom: 12 }}>
        <div className="hand" style={{ fontSize: 60 }}>{err ?? (intent ? `set ${fmtClock(intent.created_at)} · ${ago(intent.created_at)}` : 'press Enter to save')}</div>
        <button className="btn" onClick={() => { ref.current?.focus(); }}>EDIT INTENT</button>
      </div>
    </div>
  );
}

type TickerItem = { at: number; t: string; x: string; s: string };

function buildTicker(segs: ReturnType<typeof toSegs>, feed: ReturnType<typeof useLive>['feed']): TickerItem[] {
  const items: TickerItem[] = segs.filter((s) => s.kind !== 'afk').map((s) => ({
    at: s.start.getTime(), t: fmtClock(s.start), x: `${s.app}${s.title ? ' — ' + s.title : ''}`, s: STAMP[s.kind],
  }));
  for (const f of feed) {
    if (!f.created_at) continue;
    const at = new Date(f.created_at).getTime();
    const v = f.intervention_decision?.confidence;
    items.push({ at, t: fmtClock(f.created_at), x: `Noema — nudge fired${v ? ` · conf ${v.toFixed(2)}` : ''}`, s: '! SNAP' });
    if (f.outcome?.attribution) items.push({ at: at + 1, t: fmtClock(f.created_at), x: 'Noema — outcome measured', s: String(f.outcome.attribution).toUpperCase() });
  }
  return items.sort((a, b) => b.at - a.at).slice(0, 6);
}

function Ticker({ items }: { items: TickerItem[] }) {
  return (
    <div style={{
      position: 'absolute', left: -40, right: -40, bottom: 36, height: 120, background: 'var(--paper-hi)', transform: 'rotate(-0.5deg)',
      clipPath: 'polygon(0 12%,3% 0,6% 10%,9% 2%,12% 12%,15% 0,18% 9%,21% 3%,24% 11%,27% 0,30% 10%,33% 2%,36% 12%,39% 1%,42% 9%,45% 0,48% 11%,51% 2%,54% 10%,57% 0,60% 12%,63% 3%,66% 9%,69% 0,72% 11%,75% 2%,78% 10%,81% 0,84% 12%,87% 2%,90% 9%,93% 0,96% 11%,100% 2%,100% 100%,0 100%)',
      display: 'flex', alignItems: 'center', gap: 70, padding: '14px 160px 0', fontSize: 28, whiteSpace: 'nowrap', overflow: 'hidden',
    }} aria-label="Recent activity">
      {items.length === 0 && <span>nothing recorded yet today</span>}
      {items.map((k, i) => (
        <div key={i} style={{ display: 'flex', gap: 18 }}><span style={{ fontWeight: 500 }}>{k.t}</span><span>{k.x}</span><span style={{ letterSpacing: '.1em' }}>{k.s}</span></div>
      ))}
    </div>
  );
}

function demoStatus(d: number, recovering: boolean) {
  if (recovering) return { label: 'BACK ON TASK', style: { background: 'var(--recovered)', color: 'var(--paper)' }, bar: 'var(--recovered)', line: 'Demo loop: back on track.' };
  if (d >= 0.6) return { label: 'OFF TASK', style: { background: 'var(--drift)', color: 'var(--ink)' }, bar: 'var(--drift)', line: 'Demo loop: 12 min of F1 clips. Back to the assignment.' };
  if (d >= 0.45) return { label: 'WANDERING', style: { background: 'var(--paper-hi)', color: 'var(--ink)' }, bar: 'var(--muted)', line: 'Demo loop: drifting a little.' };
  return { label: 'ON TASK', style: { background: 'var(--intent)', color: 'var(--paper)' }, bar: 'var(--intent)', line: 'Demo loop: all good.' };
}
