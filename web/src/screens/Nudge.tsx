import { useEffect, useMemo, useState } from 'react';
import { liveApi, memeAssetImageUrl, memeCenterApi, api, type InterventionFeedRow } from '../api/noema';
import { useLive } from '../hooks/useLive';
import { useFetch } from '../hooks/useFetch';
import { attrOf, clamp, driftView, fmtClock, recoveryByDay, overallRecovery, toSegs, verdictOf } from '../lib/adapt';
import type { NudgeAction } from '../lib/contracts';
import { Empty, Title } from '../components/ui';

// ---- the nudge overlay ---------------------------------------------------

const ACTIONS: { n: 1 | 2 | 3; key: NudgeAction; label: string; sub: string; style: React.CSSProperties }[] = [
  { n: 1, key: 'LOCKING_IN', label: "I'M LOCKING IN", sub: 'back to your goal', style: { background: 'var(--intent)', color: 'var(--paper)', border: '6px solid var(--paper)', boxShadow: '0 18px 0 var(--paper)' } },
  { n: 2, key: 'GIVE_ME_5', label: 'GIVE ME 5 MIN', sub: 'nudges pause · 5:00', style: { background: 'var(--paper)', color: 'var(--ink)', border: '6px solid var(--paper)', boxShadow: '0 18px 0 var(--muted)' } },
  { n: 3, key: 'INTENTIONAL', label: 'THIS IS INTENTIONAL', sub: 'teaches Noema', style: { background: 'var(--ink)', color: 'var(--paper)', border: '6px solid var(--paper)', boxShadow: '0 18px 0 var(--paper)' } },
];

export function NudgeOverlay({ nudge, onAnswered, onHide }: { nudge: InterventionFeedRow; onAnswered: (a: NudgeAction) => void; onHide: () => void }) {
  const live = useLive();
  const [busy, setBusy] = useState<NudgeAction | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const eff = useFetch(() => memeCenterApi.effectiveness(), nudge.id);

  const drift = driftView(live.realtime);
  const newest = [...live.detections].sort((a, b) => String(b.timestamp).localeCompare(String(a.timestamp)))[0];
  const { verdict } = verdictOf(newest);
  const conf = nudge.intervention_decision?.confidence;
  const segs = useMemo(() => toSegs(live.today?.timeline), [live.today]);
  const last = [...segs].reverse().find((s) => s.kind !== 'afk');
  const worked = eff.data?.effectiveness.find((r) => r.id === nudge.response_id);
  const outOf10 = worked && worked.times_shown ? Math.round((worked.recovery_count / worked.times_shown) * 10) : null;

  const answer = async (a: NudgeAction) => {
    if (busy) return;
    setBusy(a); setErr(null);
    try {
      if (a === 'LOCKING_IN') await liveApi.interventionAction(nudge.id, 'LOCK_IN');
      else if (a === 'GIVE_ME_5') await liveApi.startBreak(nudge.id, 5);
      else { await liveApi.interventionAction(nudge.id, 'INTENTIONAL'); await liveApi.interventionFeedback(nudge.id, 'INTERPRETATION', 'WRONG'); }
      await live.refresh();
      onAnswered(a);
    } catch (e) { setErr(e instanceof Error ? e.message : 'could not send answer'); }
    finally { setBusy(null); }
  };

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null;
      if (t && (t.isContentEditable || /^(INPUT|TEXTAREA)$/.test(t.tagName))) return;
      if (e.key === 'Escape') { onHide(); return; }
      const hit = ACTIONS.find((x) => String(x.n) === e.key);
      if (hit) void answer(hit.key);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  });

  const title = nudge.message?.title || 'Noema noticed.';
  const body = nudge.message?.body || '';
  const memeId = nudge.meme_asset?.id;

  return (
    <div role="dialog" aria-modal="true" aria-label="Noema nudge" className="on-dark" style={{
      position: 'fixed', inset: 0, zIndex: 50, overflow: 'auto',
      background: 'var(--ink) radial-gradient(rgba(255,91,31,.28) 3px,transparent 3.6px) 0 0/22px 22px', color: 'var(--paper)',
    }}>
      <div style={{ width: 3840, minHeight: 2160, position: 'relative' }}>
        <div style={{ position: 'absolute', left: 220, top: 260, width: 1260 }}>
          <div style={{ position: 'absolute', inset: 0, transform: 'translate(26px,26px) rotate(-3deg)', background: 'radial-gradient(#FF5B1F 3px,transparent 3.6px) 0 0/12px 12px' }} />
          <div style={{ position: 'relative', background: 'var(--paper-hi)', border: '6px solid var(--ink)', padding: '44px 44px 0', transform: 'rotate(-3deg)', color: 'var(--ink)', animation: 'slap .35s cubic-bezier(.34,1.56,.64,1) both' }}>
            <div style={{ height: 1180, background: '#3A3631', display: 'flex', alignItems: 'center', justifyContent: 'center', overflow: 'hidden' }}>
              {memeId
                ? <img src={memeAssetImageUrl(memeId, false)} alt={nudge.message?.title || 'meme'} style={{ width: '100%', height: '100%', objectFit: 'contain' }} />
                : <div style={{ fontSize: 26, color: 'var(--paper)', letterSpacing: '.12em', border: '3px dashed rgba(243,236,221,.6)', padding: '12px 20px' }}>no meme attached to this nudge</div>}
            </div>
            <div style={{ height: 150, display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
              <div style={{ fontFamily: 'var(--font-hand)', fontSize: 58 }}>{memeId ? `#${memeId.slice(0, 6)}` : 'plain nudge'}</div>
              <div style={{ fontSize: 26 }}>{outOf10 !== null ? `worked ${outOf10} / 10` : 'not enough history yet'}</div>
            </div>
          </div>
          <div className="tape" style={{ top: -30, left: 80, width: 260, height: 70, transform: 'rotate(-12deg)' }} />
          <div className="tape" style={{ top: -10, right: 30, width: 240, height: 66, transform: 'rotate(14deg)' }} />
        </div>

        <div style={{ position: 'absolute', left: 1720, top: 250, right: 200, display: 'flex', flexDirection: 'column', gap: 56 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 30 }}>
            <div className="stamp drifting tilt" style={{ border: '5px solid var(--paper)', fontSize: 34, padding: '14px 26px' }}>! SNAPPED</div>
            <div style={{ border: '4px solid var(--paper)', fontSize: 30, letterSpacing: '.1em', padding: '14px 24px' }}>
              GEMINI VERDICT · {verdict ?? '—'}{typeof conf === 'number' ? ` · ${conf.toFixed(2)}` : ''}
            </div>
          </div>
          <div className="aside" style={{ fontSize: 220, lineHeight: 0.95, letterSpacing: '-0.02em', textWrap: 'balance' }}>{title}</div>
          <div style={{ display: 'flex', gap: 20, fontSize: 42, flexWrap: 'wrap' }}>
            {last && <div style={{ background: 'var(--paper)', color: 'var(--ink)', padding: '16px 28px' }}>{last.app}</div>}
            {last && <div style={{ background: 'var(--paper)', color: 'var(--ink)', padding: '16px 28px' }}>{Math.max(1, Math.round(last.minutes))} min</div>}
            {live.intent && <div style={{ background: 'var(--intent)', color: 'var(--paper)', padding: '16px 28px' }}>goal: {live.intent.text}</div>}
          </div>
          {body && <div className="aside" style={{ fontSize: 60 }}>{body}</div>}
          <div style={{ display: 'flex', gap: 44, marginTop: 20 }}>
            {ACTIONS.map((a) => (
              <button key={a.key} disabled={!!busy} onClick={() => void answer(a.key)} style={{
                flex: 1, minHeight: 230, display: 'flex', flexDirection: 'column', alignItems: 'flex-start', justifyContent: 'space-between',
                padding: '28px 34px', cursor: 'pointer', textAlign: 'left', opacity: busy && busy !== a.key ? 0.5 : 1, ...a.style,
              }}>
                <span style={{ fontSize: 26, border: '3px solid currentColor', padding: '2px 12px' }}>{a.n}</span>
                <span className="display" style={{ fontSize: 62, letterSpacing: 0 }}>{a.label}</span>
                <span style={{ fontSize: 26 }}>{busy === a.key ? 'sending…' : a.sub}</span>
              </button>
            ))}
          </div>
          {err && <div role="alert" className="stamp drifting" style={{ alignSelf: 'flex-start', fontSize: 30 }}>! {err}</div>}
          <div style={{ fontSize: 28, lineHeight: 1.6, borderTop: '3px dashed rgba(243,236,221,.6)', paddingTop: 30, marginTop: 30 }}>
            why now: drift {drift.score === null ? '—' : drift.score.toFixed(2)} vs enter {drift.enter.toFixed(2)} · no break active · delivered, so the policy gate passed
          </div>
          <div style={{ fontSize: 24, opacity: 0.8 }}>Esc hides this without answering. It will not come back until the next nudge.</div>
        </div>
      </div>
    </div>
  );
}

// ---- break mode ------------------------------------------------------------

function useCountdown(untilIso: string | null | undefined): number {
  const [left, setLeft] = useState(() => (untilIso ? Math.max(0, (new Date(untilIso).getTime() - Date.now()) / 1000) : 0));
  useEffect(() => {
    const tick = () => setLeft(untilIso ? Math.max(0, (new Date(untilIso).getTime() - Date.now()) / 1000) : 0);
    tick();
    const id = window.setInterval(tick, 1000);
    return () => window.clearInterval(id);
  }, [untilIso]);
  return left;
}

export function BreakScreen() {
  const live = useLive();
  const b = live.breakStatus;
  const left = useCountdown(b?.active ? b.active_until : null);
  const starter = live.feed.find((f) => f.actions?.includes('break_started')) ?? live.feed[0];
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  if (!b?.active || left <= 0) {
    return (
      <div className="page-body"><Empty title="No break running." body="Press GIVE ME 5 MIN on a nudge and Noema will look away for five minutes." /></div>
    );
  }
  const mm = Math.floor(left / 60), ss = String(Math.floor(left % 60)).padStart(2, '0');
  const total = Math.max(left, ((new Date(b.active_until as string).getTime() - new Date(b.started_at ?? Date.now()).getTime()) / 1000) || 300);
  const frac = clamp(left / total);
  const R = 560, C = 2 * Math.PI * R;
  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true); setErr(null);
    try { await fn(); await live.refresh(); } catch (e) { setErr(e instanceof Error ? e.message : 'failed'); } finally { setBusy(false); }
  };
  return (
    <div className="page-body" style={{ paddingTop: 120, flexDirection: 'row', gap: 120 }}>
      <div style={{ position: 'relative', width: 1500, height: 1500, flex: 'none' }}>
        <svg width="1500" height="1500" viewBox="0 0 1500 1500" role="img" aria-label={`${mm} minutes ${ss} seconds of break left`}>
          <circle cx="768" cy="768" r="640" fill="#161411" />
          <circle cx="750" cy="750" r="640" fill="#FFFDF6" stroke="#161411" strokeWidth="8" />
          <circle cx="750" cy="750" r={R} fill="none" stroke="#2B4BFF" strokeWidth="70" strokeDasharray={`${C * frac} ${C}`} transform="rotate(-90 750 750)" />
          <circle cx="750" cy="750" r={R} fill="none" stroke="#161411" strokeWidth="6" strokeDasharray="4 54.6" />
          <circle cx="750" cy="750" r="470" fill="none" stroke="#161411" strokeWidth="5" />
        </svg>
        <div style={{ position: 'absolute', inset: 0, display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', gap: 20 }}>
          <div className="mono-num" style={{ fontSize: 300, fontWeight: 500, letterSpacing: '-0.04em', lineHeight: 1 }}>{mm}:{ss}</div>
          <div className="stamp tilt" style={{ background: 'var(--intent)', color: 'var(--paper)', border: '5px solid var(--ink)', fontSize: 40, padding: '12px 28px' }}>NUDGES PAUSED</div>
        </div>
      </div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 56, paddingTop: 200 }}>
        <div className="label" style={{ fontSize: 30 }}>BREAK MODE · YOU PRESSED "GIVE ME 5 MIN"</div>
        <div className="aside" style={{ fontSize: 170, lineHeight: 0.95 }}>Five minutes. Noema is looking away.</div>
        <div style={{ fontSize: 36, lineHeight: 1.7 }}>
          Nudges suppressed until {fmtClock(b.active_until)}.<br />Drift is still measured, never acted on.<br />Ends early if you lock back in.
        </div>
        <div style={{ display: 'flex', gap: 40 }}>
          <button className="btn chunky btn-blue" style={{ minHeight: 150, fontSize: 54, padding: '0 56px' }} disabled={busy || !starter}
            onClick={() => void act(() => liveApi.interventionAction(starter!.id, 'LOCK_IN'))}>BACK EARLY</button>
          <button className="btn chunky" style={{ minHeight: 150, fontSize: 54, padding: '0 56px' }} disabled={busy || !starter}
            onClick={() => void act(() => liveApi.startBreak(starter!.id, Math.ceil(left / 60) + 5))}>+5 MIN</button>
        </div>
        {err && <div role="alert" className="stamp drifting">! {err}</div>}
        <div className="hand" style={{ fontSize: 64 }}>it asks once when time's up. once.</div>
      </div>
    </div>
  );
}

// ---- recovery ----------------------------------------------------------------

export function RecoveryScreen() {
  const live = useLive();
  const dets = useFetch(() => api.detections(200));
  // the latest nudge that has a measured outcome, else the latest nudge
  const row = live.feed.find((f) => f.outcome && f.outcome.recovery_status !== 'PENDING') ?? live.feed[0];
  const outcome = row?.outcome ?? null;
  const attr = attrOf(outcome);
  const rate14 = useMemo(() => recoveryByDay(live.outcomes, 14), [live.outcomes]);
  const overall = overallRecovery(live.outcomes);

  if (!row) return <div className="page-body"><Empty title="Nothing to recover from." body="No nudge has fired yet. When one does, this page shows whether the rope came back." /></div>;

  const nudgeAt = row.created_at ? new Date(row.created_at).getTime() : 0;
  const pts = (dets.data?.detections ?? [])
    .filter((d) => d.timestamp && typeof d.candidate_score === 'number')
    .map((d) => ({ t: new Date(d.timestamp as string).getTime(), v: d.candidate_score as number }))
    .filter((p) => Math.abs(p.t - nudgeAt) <= 31 * 60_000)
    .sort((a, b) => a.t - b.t);
  const t0 = nudgeAt - 30 * 60_000, span = 60 * 60_000;
  const X = (t: number) => ((t - t0) / span) * 2200, Y = (v: number) => 900 - clamp(v) * 760 - 40;
  const poly = (list: typeof pts) => list.map((p) => `${X(p.t).toFixed(0)},${Y(p.v).toFixed(0)}`).join(' ');
  const before = pts.filter((p) => p.t <= nudgeAt), after = pts.filter((p) => p.t >= nudgeAt);
  const drift = driftView(live.realtime);
  const recovered = outcome?.recovery_status === 'RECOVERED';
  const answered = !!outcome?.user_action;
  const secs = outcome?.recovery_duration_seconds;

  return (
    <div className="page-body" style={{ flexDirection: 'row', gap: 80, paddingTop: 80 }}>
      <div style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', gap: 40 }}>
        <Title>{recovered ? 'The rope came back.' : outcome?.recovery_status === 'PENDING' || !outcome ? 'Waiting on the rope.' : 'The rope stayed taut.'}</Title>
        <div className="card" style={{ padding: 40, transform: 'rotate(-0.4deg)', boxShadow: '16px 16px 0 var(--ink)' }}>
          <div className="label" style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 20 }}>
            <div>DRIFT SCORE · ±30 MIN</div><div>nudge {fmtClock(row.created_at)}{answered ? ' · answered' : ''}</div>
          </div>
          {pts.length < 2 ? (
            <div style={{ fontSize: 34, padding: '120px 0', textAlign: 'center' }}>Not enough drift readings were recorded around this nudge to draw the curve.</div>
          ) : (
            <svg width="100%" viewBox="0 0 2200 900" role="img" aria-label="Drift score around the nudge">
              <rect x="0" y="0" width="2200" height={Y(drift.enter)} fill="#FF5B1F" opacity=".12" />
              <line x1="0" y1={Y(drift.enter)} x2="2200" y2={Y(drift.enter)} stroke="#FF5B1F" strokeWidth="6" strokeDasharray="24 14" />
              <line x1="0" y1={Y(drift.exit)} x2="2200" y2={Y(drift.exit)} stroke="#161411" strokeWidth="3" strokeDasharray="8 10" />
              <text x="12" y={Y(drift.enter) - 10} fontFamily="DM Mono" fontSize="28" fill="#161411">{`intervene ≥ ${drift.enter.toFixed(2)}`}</text>
              <text x="12" y={Y(drift.exit) + 40} fontFamily="DM Mono" fontSize="28" fill="#161411">{`release < ${drift.exit.toFixed(2)} (hysteresis)`}</text>
              <polyline points={poly(pts)} fill="none" stroke="#161411" strokeWidth="18" strokeLinejoin="round" strokeLinecap="round" />
              {before.length > 1 && <polyline points={poly(before)} fill="none" stroke="#FF5B1F" strokeWidth="8" strokeLinejoin="round" />}
              {after.length > 1 && <polyline points={poly(after)} fill="none" stroke={recovered ? '#3E7B4F' : '#FF5B1F'} strokeWidth="8" strokeLinejoin="round" />}
              <line x1={X(nudgeAt)} y1="40" x2={X(nudgeAt)} y2="900" stroke="#161411" strokeWidth="5" />
              <g transform={`translate(${X(nudgeAt) + 20} 50) rotate(-5)`}><rect width="340" height="74" fill="#FF5B1F" stroke="#161411" strokeWidth="5" /><text x="22" y="50" fontFamily="DM Mono" fontSize="34" fontWeight="500" fill="#161411">{`! SNAP ${fmtClock(row.created_at)}`}</text></g>
            </svg>
          )}
        </div>
        <div className="card flat" style={{ display: 'flex', alignItems: 'center', gap: 60, padding: '30px 40px', boxShadow: 'none' }}>
          <div className="label" style={{ width: 420, lineHeight: 1.5 }}>RECOVERY RATE<br />LAST 14 DAYS</div>
          <div style={{ flex: 1, display: 'flex', alignItems: 'flex-end', gap: 16, height: 150 }}>
            {rate14.map((v, i) => (
              <div key={i} title={v === null ? 'no nudges' : `${v}%`} style={{ flex: 1, height: v === null ? 6 : v * 1.5, background: v !== null && v >= 70 ? 'var(--recovered)' : 'var(--paper-hi)', border: '3px solid var(--ink)', opacity: v === null ? 0.4 : 1 }} />
            ))}
          </div>
          <div style={{ textAlign: 'right' }}><div style={{ fontSize: 110, fontWeight: 500, lineHeight: 1 }}>{overall === null ? '—' : `${overall}%`}</div><div style={{ fontSize: 24 }}>of nudges recovered</div></div>
        </div>
      </div>
      <div style={{ width: 1180, display: 'flex', flexDirection: 'column', gap: 46, paddingTop: 40 }}>
        <div className="display" style={{ fontSize: 110 }}>Did it work?</div>
        <div className="aside" style={{ fontSize: 260, lineHeight: 0.85, color: recovered ? 'var(--recovered)' : 'var(--ink)' }}>{!outcome || outcome.recovery_status === 'PENDING' ? 'Not yet.' : recovered ? 'Yes.' : 'No.'}</div>
        <div style={{ display: 'flex', gap: 26, alignItems: 'center' }}>
          <div className="stamp" style={{ fontSize: 44, padding: '16px 30px', ...(attr === 'DIRECT' ? { background: 'var(--intent)', color: 'var(--paper)', transform: 'rotate(-4deg)', boxShadow: '8px 8px 0 var(--ink)' } : { opacity: 0.35 }) }}>DIRECT</div>
          <div className="stamp" style={{ fontSize: 36, padding: '12px 24px', ...(attr === 'AMBIENT' ? { background: 'var(--intent)', color: 'var(--paper)' } : { opacity: 0.35 }) }}>AMBIENT</div>
          <div className="stamp unknown" style={{ fontSize: 36, padding: '12px 24px', ...(attr === 'NONE' ? { background: 'var(--ink)', color: 'var(--paper)' } : { opacity: 0.35 }) }}>NONE</div>
        </div>
        <div style={{ fontSize: 36, lineHeight: 1.6 }}>
          {outcome
            ? `${answered ? `You answered ${String(outcome.user_action).toUpperCase().replace('_', ' ')}. ` : 'You did not answer the nudge. '}${recovered ? `Noema saw you back on task${typeof secs === 'number' ? ` ${Math.round(secs)} seconds after the nudge` : ''}.` : outcome.recovery_status === 'PENDING' ? 'The recovery window is still open.' : 'No recovery was seen in the window.'}`
            : 'No outcome has been measured for this nudge yet.'}
        </div>
        <div style={{ borderTop: '4px solid var(--ink)', paddingTop: 30, fontSize: 28, lineHeight: 1.8 }}>
          DIRECT = you came back after answering the nudge<br />AMBIENT = you came back, but later or without answering<br />NONE = drift continued past the next check
        </div>
        {row.meme_asset?.id && <div className="hand" style={{ fontSize: 60 }}>meme #{row.meme_asset.id.slice(0, 6)} gets the credit{attr === 'DIRECT' ? '.' : '… maybe.'}</div>}
      </div>
    </div>
  );
}

