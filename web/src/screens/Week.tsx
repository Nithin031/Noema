import { useMemo } from 'react';
import { liveApi, memeCenterApi } from '../api/noema';
import { useFetch } from '../hooks/useFetch';
import { hotspots, weekRows, fmtMin } from '../lib/adapt';
import { Dots, Empty, Hatch, Title } from '../components/ui';

export default function Week() {
  const week = useFetch(() => liveApi.summary('7d'));
  const eff = useFetch(() => memeCenterApi.effectiveness());
  const rows = useMemo(() => weekRows(week.data?.daily), [week.data]);
  const spots = useMemo(() => hotspots(week.data?.daily), [week.data]);
  const activeH = rows.reduce((s, r) => s + r.activeH, 0);
  const alignedH = rows.reduce((s, r) => s + r.alignedH, 0);
  const max = Math.max(1, ...rows.map((r) => r.activeH));
  const helped = (eff.data?.effectiveness ?? [])
    .filter((r) => r.times_shown >= 1)
    .map((r) => ({ k: `${r.tone} ${r.kind.toLowerCase()}`, n: Math.round((r.recovery_count / r.times_shown) * 10), shown: r.times_shown }))
    .sort((a, b) => b.n - a.n).slice(0, 4);
  const start = rows[0] ? new Date(Date.now() - 6 * 86_400_000).toLocaleDateString('en-US', { month: 'short', day: 'numeric' }) : '';

  return (
    <div className="page-body">
      <Title aside={activeH ? `${fmtMin(activeH * 60)} at the computer. ${fmtMin(alignedH * 60)} of it on intent.` : 'Nothing recorded this week yet.'}>
        {start ? `Week of ${start}` : 'This week'}
      </Title>
      {week.error && <div role="alert" className="stamp drifting" style={{ alignSelf: 'flex-start' }}>! {week.error}</div>}
      {week.loading && !week.data && <Hatch w="100%" h={560} />}
      {week.data && !activeH && <Empty title="A quiet week." body="No activity was recorded in the last seven days." />}

      {activeH > 0 && (
        <div className="card" style={{ padding: '40px 50px', display: 'flex', flexDirection: 'column', gap: 24 }}>
          <div className="label" style={{ display: 'flex', justifyContent: 'space-between' }}>
            <div>ACTIVE VS ON-INTENT · HOURS</div>
            <div style={{ display: 'flex', gap: 40, alignItems: 'center' }}>
              <span style={{ display: 'flex', gap: 12, alignItems: 'center' }}><span style={{ width: 34, height: 34, border: '5px solid var(--intent)' }} />active at the computer</span>
              <span style={{ display: 'flex', gap: 12, alignItems: 'center' }}><span style={{ width: 34, height: 34, background: 'var(--ink)' }} />aligned with intent</span>
            </div>
          </div>
          <div style={{ display: 'flex', alignItems: 'flex-end', gap: 80, height: 560, borderBottom: '5px solid var(--ink)', padding: '0 40px' }}>
            {rows.map((r) => (
              <div key={r.key} style={{ flex: 1, display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 14 }}>
                <div style={{ fontSize: 26 }}>{r.alignedH.toFixed(1)} / {r.activeH.toFixed(1)} h</div>
                <div style={{ width: '100%', display: 'flex', alignItems: 'flex-end', gap: 14, justifyContent: 'center' }}>
                  <div style={{ width: '42%', height: (r.activeH / max) * 440, border: '6px solid var(--intent)', background: 'repeating-linear-gradient(135deg,rgba(43,75,255,.12) 0 8px,transparent 8px 16px)' }} />
                  <div style={{ width: '42%', height: (r.alignedH / max) * 440, background: 'var(--ink)' }} />
                </div>
              </div>
            ))}
          </div>
          <div style={{ display: 'flex', gap: 80, padding: '0 40px' }}>{rows.map((r) => <div key={r.key} className="display" style={{ flex: 1, textAlign: 'center', fontSize: 44, letterSpacing: 0 }}>{r.label}</div>)}</div>
        </div>
      )}

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 80 }}>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
          <div className="label" style={{ marginBottom: 10 }}>WHERE YOU DRIFT</div>
          {spots.length === 0 && <div style={{ borderTop: '4px solid var(--ink)', padding: '18px 0', fontSize: 32 }}>No drift recorded this week.</div>}
          {spots.map((r) => (
            <div key={r.k} style={{ display: 'flex', alignItems: 'baseline', gap: 40, borderTop: '4px solid var(--ink)', padding: '18px 0' }}>
              <div className="display" style={{ fontSize: 120, minWidth: 330, flex: 'none', whiteSpace: 'nowrap', textShadow: '5px 4px 0 rgba(255,91,31,.85)' }}>{r.n}</div>
              <div><div className="label" style={{ fontSize: 24 }}>{r.k}</div><div className="aside" style={{ fontSize: 56, lineHeight: 1.1 }}>{r.t}</div></div>
            </div>
          ))}
        </div>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
          <div className="label" style={{ marginBottom: 10 }}>WHAT ACTUALLY HELPED · DIRECT RECOVERIES OUT OF 10</div>
          {helped.length === 0 && <div style={{ borderTop: '4px solid var(--ink)', padding: '26px 0', fontSize: 32 }}>No nudge has been shown yet, so nothing has earned a score.</div>}
          {helped.map((r) => (
            <div key={r.k} style={{ display: 'flex', alignItems: 'center', gap: 30, borderTop: '4px solid var(--ink)', padding: '26px 0' }}>
              <div style={{ width: 520, fontSize: 32 }}>{r.k}<div style={{ fontSize: 22 }}>shown {r.shown}×</div></div>
              <Dots filled={r.n} />
              <div style={{ marginLeft: 'auto', fontSize: 40, fontWeight: 500 }}>{r.n}/10</div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
