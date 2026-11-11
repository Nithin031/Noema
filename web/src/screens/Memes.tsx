import { useEffect, useMemo, useState } from 'react';
import { memeAssetImageUrl, memeCenterApi, type MemeAsset, type ResponseEffectivenessRow } from '../api/noema';
import { useFetch } from '../hooks/useFetch';
import { assetStatus, memeStats, type MemeStat } from '../lib/adapt';
import { Dots, Empty, Title } from '../components/ui';

const STATUS: { key: string; label: string }[] = [
  { key: 'all', label: 'all' }, { key: 'curated', label: 'active' },
  { key: 'disabled', label: 'benched' }, { key: 'not_curated', label: 'untested' },
];
const MIN_SHOWS = 5;
const ROT = ['-1.5deg', '1deg', '-0.8deg', '1.6deg', '-1.2deg', '0.8deg', '-1.8deg', '1.2deg'];

function useDebounced<T>(v: T, ms = 300): T {
  const [d, setD] = useState(v);
  useEffect(() => { const t = window.setTimeout(() => setD(v), ms); return () => window.clearTimeout(t); }, [v, ms]);
  return d;
}

export default function Memes() {
  const [q, setQ] = useState('');
  const [sentiment, setSentiment] = useState('');
  const [status, setStatus] = useState('all');
  const [favOnly, setFavOnly] = useState(false);
  const [sel, setSel] = useState<string | null>(null);
  const dq = useDebounced(q);
  const mode = favOnly ? 'favorite' : status;

  const stats = useFetch(() => memeCenterApi.stats());
  const eff = useFetch(() => memeCenterApi.effectiveness());
  const registry = useFetch(() => memeCenterApi.registry());
  const list = useFetch(() => memeCenterApi.assets({ q: dq, sentiment, status: mode, limit: 40 }), `${dq}|${sentiment}|${mode}`);

  const items = list.data?.items ?? [];
  const effRows = useMemo(() => eff.data?.effectiveness ?? [], [eff.data]);
  const selId = sel ?? items[0]?.id ?? null;
  const used = useMemo(() => new Set(effRows.filter((r) => r.times_shown > 0).map((r) => r.id)).size, [effRows]);
  const sentiments = Object.keys(stats.data?.by_sentiment ?? {});

  // Hall of fame / graveyard: response-level effectiveness joined to its asset.
  const { fame, grave } = useMemo(() => {
    const byAsset = new Map<string, { title: string; shown: number; rec: number }>();
    for (const r of registry.data?.responses ?? []) {
      if (!r.asset_id) continue;
      const e = effRows.find((x) => x.id === r.id);
      if (!e) continue;
      const cur = byAsset.get(r.asset_id) ?? { title: r.title, shown: 0, rec: 0 };
      cur.shown += e.times_shown; cur.rec += e.recovery_count;
      byAsset.set(r.asset_id, cur);
    }
    const rows = [...byAsset.entries()].filter(([, v]) => v.shown >= MIN_SHOWS).map(([id, v]) => ({ id, ...v, rate: v.rec / v.shown }));
    return {
      fame: [...rows].sort((a, b) => b.rate - a.rate).filter((r) => r.rate > 0).slice(0, 3),
      grave: rows.filter((r) => r.rec === 0).slice(0, 3),
    };
  }, [registry.data, effRows]);

  return (
    <div className="page-body" style={{ paddingTop: 50, gap: 40 }}>
      <div style={{ display: 'flex', alignItems: 'flex-end', justifyContent: 'space-between', gap: 60 }}>
        <div style={{ display: 'flex', alignItems: 'baseline', gap: 36 }}>
          <Title orange>Meme Center</Title>
          <div style={{ fontSize: 32 }}>{stats.data ? `${stats.data.assets.toLocaleString()} memes · ${used} used · ${stats.data.favorites} ★` : '…'}</div>
        </div>
        <label className="card" style={{ width: 1100, minHeight: 88, display: 'flex', alignItems: 'center', gap: 20, padding: '0 28px', boxShadow: '8px 8px 0 var(--ink)', fontSize: 34 }}>
          <span aria-hidden="true">⌕</span>
          <input
            value={q} onChange={(e) => setQ(e.target.value)} placeholder="search OCR text, tags…" aria-label="Search memes"
            onKeyDown={(e) => { if (e.key === 'Escape') setQ(''); }}
            style={{ flex: 1, border: 0, background: 'transparent', font: 'inherit', outline: 'none', color: 'var(--ink)' }}
          />
        </label>
      </div>

      <div style={{ display: 'flex', gap: 16, alignItems: 'center', flexWrap: 'wrap', fontSize: 28 }}>
        <div className="label" style={{ marginRight: 10 }}>SENTIMENT</div>
        {['', ...sentiments].map((s) => (
          <button key={s || 'all'} className={`chip round${sentiment === s ? ' on' : ''}`} aria-pressed={sentiment === s} onClick={() => setSentiment(s)}>{s ? s.replace(/_/g, ' ') : 'any'}</button>
        ))}
        <div style={{ width: 4, height: 56, background: 'var(--ink)', margin: '0 20px' }} />
        <div className="label" style={{ marginRight: 10 }}>STATUS</div>
        {STATUS.map((s) => (
          <button key={s.key} className={`chip${status === s.key && !favOnly ? ' on' : ''}`} aria-pressed={status === s.key && !favOnly} onClick={() => { setStatus(s.key); setFavOnly(false); }}>{s.label}</button>
        ))}
        <div style={{ width: 4, height: 56, background: 'var(--ink)', margin: '0 20px' }} />
        <button className={`chip${favOnly ? ' on' : ''}`} aria-pressed={favOnly} onClick={() => setFavOnly((v) => !v)}>★ favorites</button>
      </div>

      {list.error && <div role="alert" className="stamp drifting" style={{ alignSelf: 'flex-start' }}>! {list.error}</div>}
      {!list.loading && !items.length && !list.error && (
        <Empty title="No memes here." body={stats.data?.assets ? 'Nothing matches those filters.' : 'No meme corpus is ingested yet. Run: python -m noema memes ingest'} />
      )}

      {items.length > 0 && (
        <div style={{ display: 'flex', gap: 70, height: 1130 }}>
          <div style={{ flex: 1, minWidth: 0, columns: 4, columnGap: 56, overflow: 'auto', padding: '20px 10px 0' }}>
            {items.map((m, i) => <Print key={m.id} m={m} rot={ROT[i % ROT.length]} on={m.id === selId} stat={memeStats(m, effRows)} onPick={() => setSel(m.id)} />)}
          </div>
          {selId && <Drawer id={selId} effRows={effRows} onChanged={() => { list.reload(); stats.reload(); }} />}
        </div>
      )}

      <div style={{ display: 'flex', gap: 60, minHeight: 250 }}>
        <div style={{ flex: 1, display: 'flex', alignItems: 'center', gap: 36, border: '5px solid var(--ink)', background: 'var(--intent)', color: 'var(--paper)', padding: '30px 40px' }}>
          <div className="display" style={{ fontSize: 64, width: 300, flex: 'none', letterSpacing: 0 }}>Hall of Fame</div>
          {fame.length === 0 && <div style={{ fontSize: 28 }}>needs {MIN_SHOWS}+ shows and a recovery</div>}
          {fame.map((f, i) => <Mini key={f.id} t={f.title} id={f.id} s={`${Math.round(f.rate * 10)}/10 direct`} rot={i % 2 ? '1deg' : '-1deg'} />)}
        </div>
        <div style={{ flex: 1, display: 'flex', alignItems: 'center', gap: 36, border: '5px dashed var(--ink)', background: 'var(--paper-lo)', padding: '30px 40px' }}>
          <div className="display" style={{ fontSize: 64, width: 300, flex: 'none', letterSpacing: 0 }}>Graveyard</div>
          {grave.length === 0 && <div style={{ fontSize: 28 }}>nothing has died yet</div>}
          {grave.map((f, i) => <Mini key={f.id} t={f.title} id={f.id} s={`0/${f.shown} · R.I.P.`} rot={i % 2 ? '1deg' : '-1.2deg'} dead />)}
        </div>
      </div>
    </div>
  );
}

function Mini({ t, id, s, rot, dead }: { t: string; id: string; s: string; rot: string; dead?: boolean }) {
  return (
    <div style={{ flex: 1, minWidth: 0, background: dead ? 'var(--paper)' : 'var(--paper-hi)', color: 'var(--ink)', border: '4px solid var(--ink)', padding: '18px 22px', transform: `rotate(${rot})` }}>
      <div className="display" style={{ fontSize: 34, letterSpacing: 0, textDecoration: dead ? 'line-through' : 'none', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{t}</div>
      <div style={{ fontSize: 24, marginTop: 6 }}>#{id.slice(0, 6)} · {s}</div>
    </div>
  );
}

function Print({ m, rot, on, stat, onPick }: { m: MemeAsset; rot: string; on: boolean; stat: MemeStat; onPick: () => void }) {
  return (
    <button onClick={onPick} aria-pressed={on} aria-label={`Meme ${m.id.slice(0, 6)}: ${m.display_text ?? ''}`} style={{
      display: 'block', width: '100%', breakInside: 'avoid', marginBottom: 56, position: 'relative', background: 'var(--paper-hi)', textAlign: 'left',
      border: on ? '6px solid var(--drift)' : '4px solid var(--ink)', padding: '18px 18px 0', transform: `rotate(${rot})`, boxShadow: '10px 10px 0 var(--ink)', cursor: 'pointer',
    }}>
      <div style={{ position: 'absolute', top: -16, left: '50%', width: 34, height: 34, marginLeft: -17, borderRadius: '50%', background: 'var(--drift)', border: '4px solid var(--ink)' }} />
      <img src={memeAssetImageUrl(m.id, true)} alt="" loading="lazy" style={{ display: 'block', width: '100%', height: 'auto', minHeight: 180, background: '#3A3631' }} />
      <div style={{ height: 64, display: 'flex', alignItems: 'center', justifyContent: 'space-between', fontSize: 22 }}>
        <span>#{m.id.slice(0, 6)} {m.favorite ? '★' : ''}</span><span>{stat.outOf10 === null ? '—' : `${stat.outOf10}/10`}</span>
      </div>
    </button>
  );
}

function Drawer({ id, effRows, onChanged }: { id: string; effRows: ResponseEffectivenessRow[]; onChanged: () => void }) {
  const [tick, setTick] = useState(0);
  const { data, error } = useFetch(() => memeCenterApi.asset(id), `${id}|${tick}`);
  const [busy, setBusy] = useState(false);
  const a = data?.asset;
  const stat = a ? memeStats(a, effRows) : { shown: 0, recovered: 0, outOf10: null };
  const st = a ? assetStatus(a, stat) : 'untested';
  const act = async (fn: () => Promise<unknown>) => { setBusy(true); try { await fn(); setTick((t) => t + 1); onChanged(); } finally { setBusy(false); } };
  return (
    <aside className="card" style={{ width: 1000, flex: 'none', padding: 40, display: 'flex', flexDirection: 'column', gap: 30, fontSize: 28, boxShadow: '16px 16px 0 var(--ink)', overflow: 'auto' }} aria-label="Meme details">
      {error && <div role="alert" className="stamp drifting">! {error}</div>}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline' }}>
        <div className="display" style={{ fontSize: 76 }}>#{id.slice(0, 6)}</div>
        <div style={{ fontSize: 26 }}>{st}{a?.favorite ? ' · ★' : ''}</div>
      </div>
      <div><div className="label" style={{ fontSize: 22, marginBottom: 8 }}>OCR TEXT</div>
        <div className="aside" style={{ fontSize: 44, lineHeight: 1.15 }}>"{a?.display_text || a?.corrected_text || a?.ocr_text || '—'}"</div></div>
      <div style={{ display: 'grid', gridTemplateColumns: '240px 1fr', gap: '16px 20px', borderTop: '3px dashed var(--ink)', paddingTop: 26 }}>
        <div className="label" style={{ fontSize: 22 }}>SENTIMENT</div><div>{a?.sentiment?.replace(/_/g, ' ') ?? '—'}</div>
        <div className="label" style={{ fontSize: 22 }}>PROVENANCE</div><div>{a?.source ?? '—'}{a?.source_ref ? ` · ${a.source_ref}` : ''}</div>
        <div className="label" style={{ fontSize: 22 }}>TAGS</div><div>{a?.tags?.length ? a.tags.join(', ') : '—'}</div>
        <div className="label" style={{ fontSize: 22 }}>RESPONSES</div><div>{a?.responses?.length ? a.responses.map((r) => `${r.kind}/${r.tone}${r.enabled ? '' : ' (off)'}`).join(' · ') : 'not curated yet'}</div>
      </div>
      <div style={{ borderTop: '3px dashed var(--ink)', paddingTop: 26, display: 'flex', flexDirection: 'column', gap: 16 }}>
        <div className="label" style={{ fontSize: 22 }}>EFFECTIVENESS · DIRECT RECOVERIES</div>
        <div style={{ display: 'flex', gap: 14, alignItems: 'center' }}>
          <Dots filled={stat.outOf10 ?? 0} size={52} />
          <div style={{ marginLeft: 16, fontSize: 44, fontWeight: 500 }}>{stat.outOf10 === null ? '—' : `${stat.outOf10} / 10`}</div>
        </div>
        <div style={{ fontSize: 24 }}>{stat.shown ? `${stat.recovered} recoveries in ${stat.shown} showings` : 'never shown yet'}</div>
      </div>
      <div style={{ marginTop: 'auto', display: 'flex', gap: 20 }}>
        <button className="btn btn-blue" style={{ flex: 1, minHeight: 88, border: '5px solid var(--ink)', boxShadow: '0 10px 0 var(--ink)' }} disabled={busy || !a} onClick={() => void act(() => memeCenterApi.favorite(id, !a?.favorite))}>{a?.favorite ? '★ UNFAVORITE' : '★ FAVORITE'}</button>
        <button className="btn" style={{ flex: 1, minHeight: 88, border: '5px solid var(--ink)', boxShadow: '0 10px 0 var(--ink)' }} disabled={busy || !a} onClick={() => void act(() => memeCenterApi.setEnabled(id, a?.enabled === false))}>{a?.enabled === false ? 'RESTORE' : 'BENCH IT'}</button>
      </div>
    </aside>
  );
}
