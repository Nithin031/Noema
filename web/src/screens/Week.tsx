import { useMemo } from 'react';
import { liveApi } from '../api/noema';
import { useFetch } from '../hooks/useFetch';
import { fmtMin, tally, toSegs, weekRows, type Tally } from '../lib/adapt';
import { Empty, Hatch, Title } from '../components/ui';

export default function Week() {
  const week = useFetch(() => liveApi.summary('7d'));
  const segs = useMemo(() => toSegs(week.data?.daily.timeline), [week.data]);
  const rows = useMemo(() => weekRows(week.data?.daily), [week.data]);
  const apps = useMemo(() => tally(segs, (s) => s.app, 8), [segs]);
  const sites = useMemo(() => tally(segs, (s) => s.domain, 8), [segs]);
  const activeH = rows.reduce((s, r) => s + r.activeH, 0);
  const max = Math.max(1, ...rows.map((r) => r.activeH));
  const busiest = [...rows].sort((a, b) => b.activeH - a.activeH)[0];

  return (
    <div className="page-body">
      <Title aside={activeH ? `${fmtMin(activeH * 60)} at the computer in 7 days.` : 'Nothing recorded this week yet.'}>This week</Title>
      {week.error && <div role="alert" className="stamp drifting" style={{ alignSelf: 'flex-start' }}>! {week.error}</div>}
      {week.loading && !week.data && <Hatch w="100%" h={560} />}
      {week.data && !activeH && <Empty title="A quiet week." body="No activity was recorded in the last seven days." />}

      {activeH > 0 && (
        <div className="card" style={{ padding: '40px 50px', display: 'flex', flexDirection: 'column', gap: 24 }}>
          <div className="label" style={{ display: 'flex', justifyContent: 'space-between' }}>
            <div>HOURS AT THE COMPUTER, PER DAY</div>
            {busiest && busiest.activeH > 0 && <div>busiest: {busiest.label} · {busiest.activeH.toFixed(1)} h</div>}
          </div>
          <div style={{ display: 'flex', alignItems: 'flex-end', gap: 80, height: 520, borderBottom: '5px solid var(--ink)', padding: '0 40px' }}>
            {rows.map((r) => (
              <div key={r.key} style={{ flex: 1, display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 14 }}>
                <div style={{ fontSize: 28, fontWeight: 500 }}>{r.activeH.toFixed(1)} h</div>
                <div style={{ width: '60%', height: (r.activeH / max) * 400, background: 'var(--intent)', border: '5px solid var(--ink)', borderBottom: 0 }} />
              </div>
            ))}
          </div>
          <div style={{ display: 'flex', gap: 80, padding: '0 40px' }}>{rows.map((r) => <div key={r.key} className="display" style={{ flex: 1, textAlign: 'center', fontSize: 44, letterSpacing: 0 }}>{r.label}</div>)}</div>
        </div>
      )}

      {activeH > 0 && (
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 60 }}>
          <Top name="TOP APPS · 7 DAYS" rows={apps} />
          <Top name="TOP SITES · 7 DAYS" rows={sites} />
        </div>
      )}
    </div>
  );
}

function Top({ name, rows }: { name: string; rows: Tally[] }) {
  const max = rows[0]?.minutes || 1;
  return (
    <div className="card" style={{ padding: '30px 40px', display: 'flex', flexDirection: 'column', gap: 16 }}>
      <div className="label">{name}</div>
      {rows.length === 0 && <div style={{ fontSize: 28 }}>none recorded</div>}
      {rows.map((r) => (
        <div key={r.name} style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', gap: 20, fontSize: 28 }}>
            <span style={{ overflow: 'hidden', whiteSpace: 'nowrap', textOverflow: 'ellipsis' }}>{r.name}</span>
            <span style={{ fontWeight: 500 }}>{fmtMin(r.minutes)}</span>
          </div>
          <div style={{ height: 20, border: '3px solid var(--ink)', background: 'var(--paper)' }}>
            <div style={{ height: '100%', width: `${(r.minutes / max) * 100}%`, background: 'var(--intent)' }} />
          </div>
        </div>
      ))}
    </div>
  );
}
