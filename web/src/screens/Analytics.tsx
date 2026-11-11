import { useMemo, useState } from 'react';
import { liveApi, request } from '../api/noema';
import { useFetch } from '../hooks/useFetch';
import { axisFor, fmtClock, fmtMin, friendlyApp, hourly, place, tally, toSegs, totals, type Placed, type Tally } from '../lib/adapt';
import { Empty, Hatch, Title } from '../components/ui';

// Neutral palette: colors only tell categories apart. Nothing here is "good" or "bad".
const PALETTE = ['var(--intent)', 'var(--drift)', 'var(--recovered)', 'var(--ink)', 'var(--intent-soft)', 'var(--muted)', 'var(--hatch)'];

function isoDate(offset: number): string {
  const d = new Date(); d.setDate(d.getDate() + offset);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

export default function Analytics() {
  const [offset, setOffset] = useState(0);
  const { data, error, loading } = useFetch(() => liveApi.summary('today', isoDate(offset)), offset);
  const segs = useMemo(() => toSegs(data?.daily.timeline), [data]);
  const axis = useMemo(() => axisFor(segs, offset === 0 ? new Date() : new Date(segs[0]?.start ?? Date.now())), [segs, offset]);
  const placed = useMemo(() => place(segs, axis), [segs, axis]);
  const hours = useMemo(() => hourly(segs), [segs]);
  const acts = useFetch(() => liveApi.activity(isoDate(offset)), `a${offset}`);
  // Real categories (coding, studying, messaging, …) come from the classifier, not from window titles.
  const cats = useMemo(() => {
    const m = new Map<string, number>();
    for (const r of acts.data?.activities ?? []) {
      if (r.productivity === 'afk') continue;
      const k = (r.activity_type || '').trim().toLowerCase() || 'unsorted';
      m.set(k, (m.get(k) ?? 0) + (r.duration_seconds ?? 0) / 60);
    }
    return [...m.entries()].map(([name, minutes]) => ({ name: name.charAt(0).toUpperCase() + name.slice(1), minutes }))
      .sort((a, b) => b.minutes - a.minutes).slice(0, 7);
  }, [acts.data]);
  const apps = useMemo(() => tally(segs, (s) => s.app, 6), [segs]);
  const titles = useMemo(() => tally(segs, (s) => s.title, 6), [segs]);
  const domains = useMemo(() => tally(segs, (s) => s.domain, 6), [segs]);
  const { active, afk } = totals(segs);
  const dateLabel = new Date(Date.now() + offset * 86_400_000).toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric' });
  const total = (h: (typeof hours)[number]) => h.aligned + h.unknown + h.drift;
  const hourMax = Math.max(60, ...hours.map(total));
  const catTotal = cats.reduce((s, c) => s + c.minutes, 0) || 1;

  return (
    <div className="page-body" style={{ paddingTop: 50, gap: 40 }}>
      <div style={{ display: 'flex', alignItems: 'flex-end', justifyContent: 'space-between', gap: 60 }}>
        <Title aside="Where your time went.">Today</Title>
        <div className="card" style={{ display: 'flex', alignItems: 'center', padding: 0, boxShadow: '8px 8px 0 var(--ink)', borderWidth: 4 }}>
          <button className="btn" style={{ width: 80, boxShadow: 'none', border: 0, borderRight: '4px solid var(--ink)', fontSize: 40 }} aria-label="Previous day" onClick={() => setOffset((o) => o - 1)}>‹</button>
          <div style={{ minWidth: 440, textAlign: 'center', fontSize: 32 }}>{offset === 0 ? 'Today · ' : ''}{dateLabel}</div>
          <button className="btn" style={{ width: 80, boxShadow: 'none', border: 0, borderLeft: '4px solid var(--ink)', fontSize: 40, opacity: offset < 0 ? 1 : 0.3 }} aria-label="Next day" disabled={offset >= 0} onClick={() => setOffset((o) => Math.min(0, o + 1))}>›</button>
        </div>
      </div>

      {error && <div role="alert" className="stamp drifting" style={{ alignSelf: 'flex-start', fontSize: 30 }}>! {error}</div>}
      {loading && !data && <><Hatch w="100%" h={400} /><div style={{ fontSize: 32 }}>loading…</div></>}
      {data && !segs.length && <Empty title="Nothing yet." body="Nothing was recorded for this day." />}

      {segs.length > 0 && (
        <>
          <div style={{ display: 'flex', gap: 60, height: 600 }}>
            <div className="card" style={{ flex: 1, minWidth: 0, padding: '34px 40px', display: 'flex', flexDirection: 'column', gap: 20 }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline' }}>
                <div className="label">ACTIVE MINUTES PER HOUR</div>
                <div style={{ display: 'flex', gap: 40, alignItems: 'baseline' }}>
                  <div><span style={{ fontSize: 64, fontWeight: 500 }}>{fmtMin(active)}</span> <span style={{ fontSize: 24 }}>active</span></div>
                  <div><span style={{ fontSize: 40 }}>{fmtMin(afk)}</span> <span style={{ fontSize: 24 }}>away</span></div>
                </div>
              </div>
              <div style={{ flex: 1, display: 'flex', alignItems: 'flex-end', gap: 14, borderBottom: '5px solid var(--ink)' }}>
                {hours.map((b) => (
                  <div key={b.hour} title={`${b.hour}:00 — ${Math.round(total(b))} min`} style={{ flex: 1, height: '100%', display: 'flex', alignItems: 'flex-end' }}>
                    <div style={{ width: '100%', height: `${(total(b) / hourMax) * 100}%`, background: 'var(--intent)', border: '3px solid var(--ink)', borderBottom: 0 }} />
                  </div>
                ))}
              </div>
              <div style={{ display: 'flex', gap: 14 }}>{hours.map((b) => <div key={b.hour} style={{ flex: 1, textAlign: 'center', fontSize: 22 }}>{String(b.hour).padStart(2, '0')}</div>)}</div>
            </div>
            <div className="card" style={{ width: 1100, padding: '34px 40px', display: 'flex', flexDirection: 'column', gap: 24 }}>
              <div className="label">CATEGORIES</div>
              <div style={{ display: 'flex', height: 70, border: '4px solid var(--ink)' }}>
                {cats.map((c, i) => <div key={c.name} style={{ width: `${(c.minutes / catTotal) * 100}%`, background: PALETTE[i % PALETTE.length], borderRight: '3px solid var(--ink)' }} />)}
              </div>
              {cats.map((c, i) => (
                <div key={c.name} style={{ display: 'flex', alignItems: 'center', gap: 20, fontSize: 30 }}>
                  <div style={{ width: 34, height: 34, border: '4px solid var(--ink)', background: PALETTE[i % PALETTE.length] }} />
                  <div style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{c.name}</div>
                  <div style={{ width: 150, textAlign: 'right', fontWeight: 500 }}>{fmtMin(c.minutes)}</div>
                </div>
              ))}
            </div>
          </div>

          <Lanes placed={placed} axis={axis} />

          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3,minmax(0,1fr))', gap: 60 }}>
            <Top name="TOP APPS" rows={apps} />
            <Top name="TOP WINDOW TITLES" rows={titles} />
            <Top name="TOP SITES" rows={domains} />
          </div>
        </>
      )}
      {offset === 0 && <BackgroundCard />}
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

function Lanes({ placed, axis }: { placed: Placed[]; axis: { start: number; end: number } }) {
  const lane = (name: string, items: { p: Placed; label: string; bg: string; fg?: string }[]) => (
    <div key={name} style={{ display: 'flex', alignItems: 'center', gap: 30 }}>
      <div className="label" style={{ width: 220, fontSize: 24 }}>{name}</div>
      <div style={{ flex: 1, position: 'relative', height: 58, border: '3px solid var(--ink)', background: 'var(--paper)' }}>
        {items.map(({ p, label, bg, fg }, i) => (
          <div key={i} style={{ position: 'absolute', top: 0, bottom: 0, left: `${p.left}%`, width: `${p.width}%`, background: bg, color: fg ?? 'var(--paper)', borderRight: '3px solid var(--ink)', fontSize: 22, lineHeight: '52px', paddingLeft: 10, overflow: 'hidden', whiteSpace: 'nowrap' }}>{p.width > 3 ? label : ''}</div>
        ))}
      </div>
    </div>
  );
  const ticks: number[] = [];
  for (let m = axis.start; m <= axis.end; m += Math.max(60, Math.ceil((axis.end - axis.start) / 6 / 60) * 60)) ticks.push(m);
  return (
    <div className="card" style={{ padding: '28px 40px', display: 'flex', flexDirection: 'column', gap: 14 }}>
      <div className="label" style={{ display: 'flex', justifyContent: 'space-between' }}>
        <div>TIMELINE</div><div>{fmtClock(new Date(2000, 0, 1, Math.floor(axis.start / 60)))} — {fmtClock(new Date(2000, 0, 1, Math.floor(axis.end / 60) % 24))}</div>
      </div>
      {lane('APP', placed.filter((p) => p.kind !== 'afk').map((p) => ({ p, label: p.app, bg: 'var(--ink)' })))}
      {lane('BROWSER TAB', placed.filter((p) => p.domain).map((p) => ({ p, label: p.domain, bg: 'var(--paper-hi)', fg: 'var(--ink)' })))}
      {lane('AWAY', placed.filter((p) => p.kind === 'afk').map((p) => ({ p, label: 'away', bg: 'var(--hatch)', fg: 'var(--ink)' })))}
      <div style={{ display: 'flex', gap: 30 }}><div style={{ width: 220 }} /><div style={{ flex: 1, position: 'relative', height: 30, fontSize: 22 }}>{ticks.map((m) => <span key={m} style={{ position: 'absolute', left: `${((m - axis.start) / (axis.end - axis.start)) * 100}%`, transform: 'translateX(-50%)' }}>{String(Math.floor(m / 60) % 24).padStart(2, '0')}</span>)}</div></div>
    </div>
  );
}

type Background = {
  enabled: boolean;
  now: { open: string[]; audio: string[]; audio_supported: boolean | null };
  today: { app: string; open_minutes: number; audio_minutes: number }[];
  error: string | null;
};

/** Apps open behind the foreground window and apps playing audio. App names and minutes only. */
function BackgroundCard() {
  const { data } = useFetch(() => request<Background>('/api/background'), 'bg');
  const rows = (data?.today ?? []).filter((r) => r.open_minutes >= 1 || r.audio_minutes >= 1).slice(0, 8);
  const max = Math.max(1, ...rows.map((r) => r.open_minutes));
  return (
    <div className="card" style={{ padding: '30px 40px', display: 'flex', flexDirection: 'column', gap: 16 }}>
      <div className="label" style={{ display: 'flex', justifyContent: 'space-between' }}>
        <div>IN THE BACKGROUND · TODAY</div>
        <div>{data?.now.audio.length ? `playing now: ${data.now.audio.map(friendlyApp).join(', ')}` : 'nothing playing'}</div>
      </div>
      {data && !data.enabled && <div style={{ fontSize: 28 }}>Background watching is off (NOEMA_BACKGROUND_WATCH).</div>}
      {data?.enabled && data.now.audio_supported === false && (
        <div style={{ fontSize: 26 }}>Audio detection needs pycaw: <code>pip install pycaw</code>. Open apps are still tracked.</div>
      )}
      {data?.enabled && rows.length === 0 && <div style={{ fontSize: 28 }}>Nothing has run behind your work yet. It fills in as you use the computer.</div>}
      {rows.map((r) => (
        <div key={r.app} style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', gap: 20, fontSize: 28 }}>
            <span>{friendlyApp(r.app)}</span>
            <span style={{ fontWeight: 500 }}>{r.audio_minutes >= 1 ? `audio ${fmtMin(r.audio_minutes)} · ` : ''}open {fmtMin(r.open_minutes)}</span>
          </div>
          <div style={{ height: 20, border: '3px solid var(--ink)', background: 'var(--paper)', position: 'relative' }}>
            <div style={{ height: '100%', width: `${(r.open_minutes / max) * 100}%`, background: 'var(--muted)' }} />
            <div style={{ position: 'absolute', top: 0, left: 0, height: '100%', width: `${(r.audio_minutes / max) * 100}%`, background: 'var(--intent)' }} />
          </div>
        </div>
      ))}
    </div>
  );
}
