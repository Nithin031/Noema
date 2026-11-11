import { useMemo } from 'react';
import { api, liveApi, request, type ModelPerfRow, type QuotaRow } from '../api/noema';

type Lane = { lane: string; model: string; state: string; queued: number; calls: number; failures: number; yields: number; last_error: string | null; last_latency_ms: number | null; last_rival: string | null };
type Lanes = {
  order: string[]; lanes: Lane[]; chain: { name: string; model: string }[];
  tiers: { classification: string[]; reasoning: string[] };
  settings: { ollama_enabled: boolean; strict_alignment: boolean; webllm_enabled: boolean; webllm_provider: string; claude_code_enabled: boolean; claude_code_model: string };
};
import { useFetch } from '../hooks/useFetch';
import { useLive } from '../hooks/useLive';
import { Hatch, Title } from '../components/ui';

const TIERS: { name: string; models: string[] }[] = [
  { name: 'Classification', models: [] }, // models always come from the daemon (/api/llm/lanes)
  { name: 'Reasoning', models: [] },
];

const ms = (v: number | null | undefined) => (v === null || v === undefined ? '—' : v >= 1000 ? `${(v / 1000).toFixed(1)} s` : `${Math.round(v)} ms`);

export default function Engine() {
  const live = useLive();
  const { data, error, loading } = useFetch(async () => {
    const settle = async <T,>(p: Promise<T>) => { try { return await p; } catch { return null; } };
    const [quotas, models, providers, bench, lanes] = await Promise.all([
      settle(api.quotas()), settle(api.metricsModels(30)), settle(liveApi.providers(30)), settle(api.benchmarks(1)),
      settle(request<Lanes>('/api/llm/lanes')),
    ]);
    return { quotas, models: (models?.models ?? []).filter((m) => m.provider !== 'ollama'), providers: (providers?.providers ?? []).filter((p) => p.provider !== 'ollama'), bench: bench?.runs?.[0] ?? null, lanes };
  });

  const perf = useMemo(() => {
    const m = new Map<string, ModelPerfRow>();
    for (const r of data?.models ?? []) if (r.model) m.set(r.model, r);
    return m;
  }, [data]);
  const quotaOf = (model: string): QuotaRow | undefined => data?.quotas?.models.find((q) => q.model === model);

  const tiers = [
    { name: TIERS[0].name, models: data?.lanes?.tiers.classification ?? TIERS[0].models },
    { name: TIERS[1].name, models: data?.lanes?.tiers.reasoning ?? TIERS[1].models },
  ];
  const chains = tiers.map((t) => {
    const reqs = t.models.map((m) => perf.get(m)?.requests ?? 0);
    const liveIdx = Math.max(...reqs) > 0 ? reqs.indexOf(Math.max(...reqs)) : -1;
    const total = reqs.reduce((a, b) => a + b, 0);
    return {
      name: t.name,
      steps: t.models.map((m, i) => {
        const q = quotaOf(m);
        const bad = q?.status === 'down' || q?.status === 'cooldown' || q?.status === 'rate_limited';
        const st = i === liveIdx ? `live · ${Math.round((reqs[i] / total) * 100)}% of calls` : bad ? String(q?.status).replace('_', ' ') : 'standby';
        return { m, st, live: i === liveIdx, down: bad };
      }),
    };
  });

  const topQuota = (tier: number): { n: string; used: number; limit: number } | null => {
    const rows = tiers[tier].models.map(quotaOf).filter((q): q is QuotaRow => !!q && q.rpd.limit > 0);
    if (!rows.length) return null;
    const q = rows.sort((a, b) => (b.rpd.used ?? 0) - (a.rpd.used ?? 0))[0];
    return { n: `${tiers[tier].name.toLowerCase()} · ${q.model}`, used: q.rpd.used ?? 0, limit: q.rpd.limit };
  };
  const quotas = [topQuota(0), topQuota(1)].filter((q): q is NonNullable<typeof q> => !!q);
  const tokensToday = (data?.quotas?.models ?? []).reduce((s, q) => s + (q.tokens_today ?? 0), 0);

  const latencyRows = [...(data?.models ?? [])].filter((r) => r.latency && (r.latency.n ?? 0) > 0).sort((a, b) => (b.requests ?? 0) - (a.requests ?? 0)).slice(0, 4);
  const maxMs = Math.max(1, ...latencyRows.map((r) => Math.max(r.latency?.p50 ?? 0, r.latency?.p95 ?? 0)));
  const pct = (v: number | null | undefined) => `${Math.min(100, Math.max(0, ((v ?? 0) / maxMs) * 100))}%`;

  const health = [
    { s: live.offline ? 'DOWN' : 'OK', n: 'local daemon', d: live.health?.uptime_seconds ? `up ${Math.round(live.health.uptime_seconds / 60)} min` : '' },
    ...(data?.providers ?? []).map((p) => {
      const rate = p.success_rate;
      const few = (p.requests ?? 0) < 3; // one failure is not an outage
      return { s: rate === null || rate === undefined ? 'OK' : rate >= 0.9 ? 'OK' : few ? 'WARN' : rate >= 0.5 ? 'SLOW' : 'DOWN', n: p.provider ?? '?', d: rate === null || rate === undefined ? 'no calls' : `${Math.round(rate * 100)}% ok · ${p.requests ?? 0} calls` };
    }),
  ];

  const results = data?.bench?.results ?? [];
  const okRate = results.length ? results.reduce((s, r) => s + (r.success_count ?? 0), 0) / Math.max(1, results.reduce((s, r) => s + (r.iterations ?? 0), 0)) : null;
  const schemaRate = results.length ? results.reduce((s, r) => s + (r.schema_valid_count ?? 0), 0) / Math.max(1, results.reduce((s, r) => s + (r.iterations ?? 0), 0)) : null;
  const p95 = results.length ? Math.max(...results.map((r) => r.p95_latency_ms ?? 0)) : null;
  const bench = [
    { v: okRate === null ? '—' : `${(okRate * 100).toFixed(1)}%`, l: 'benchmark calls succeeded' },
    { v: schemaRate === null ? '—' : `${(schemaRate * 100).toFixed(1)}%`, l: 'schema-valid answers' },
    { v: p95 === null ? '—' : ms(p95), l: 'worst p95 latency' },
    { v: '0', l: 'keystrokes read' },
  ];

  return (
    <div className="page-body">
      <Title orange aside="Where the models live, and what happens when they don't answer.">Engine Room</Title>
      {error && <div role="alert" className="stamp drifting" style={{ alignSelf: 'flex-start' }}>! {error}</div>}
      {loading && !data && <><Hatch w="100%" h={400} /><div style={{ fontSize: 32 }}>asking the models how they are…</div></>}
      <div className="card" style={{ padding: 40, display: 'flex', flexDirection: 'column', gap: 24 }}>
        <div className="label">WHO ANSWERS FIRST · PRIORITY ORDER</div>
        <div style={{ display: 'flex', alignItems: 'stretch', gap: 18, flexWrap: 'wrap' }}>
          {[...(data?.lanes?.lanes ?? []), { lane: 'gemini', model: 'last resort · free-tier quota', state: 'standby', queued: 0, calls: 0, failures: 0, yields: 0, last_error: null, last_latency_ms: null, last_rival: null } as Lane].map((l, i, arr) => (
            <div key={l.lane} style={{ display: 'flex', alignItems: 'center', gap: 18 }}>
              <div style={{ minWidth: 420, padding: '14px 26px', border: `5px ${l.state === 'idle' || l.state === 'busy' ? 'solid' : 'dashed'} var(--ink)`, background: l.state.startsWith('yielding') ? 'var(--drift)' : l.state === 'busy' ? 'var(--intent)' : 'var(--paper-hi)', color: l.state === 'busy' ? 'var(--paper)' : 'var(--ink)' }}>
                <div style={{ fontSize: 22, letterSpacing: '.1em' }}>{i + 1} · {l.lane.replace('_', ' ').toUpperCase()}</div>
                <div style={{ fontSize: 32, fontWeight: 500 }}>{l.model}</div>
                <div style={{ fontSize: 24 }}>{l.state}{l.calls ? ` · ${l.calls} calls` : ''}{l.yields ? ` · yielded ${l.yields}×` : ''}{l.failures ? ` · ${l.failures} failed` : ''}</div>
                {l.last_error && <div style={{ fontSize: 22, marginTop: 4 }}>{l.last_error.slice(0, 70)}</div>}
              </div>
              <span style={{ fontSize: 44 }}>{i < arr.length - 1 ? '→' : ''}</span>
            </div>
          ))}
        </div>
        {data?.lanes && (
          <div style={{ display: 'flex', gap: 16, flexWrap: 'wrap', fontSize: 26 }}>
            {([['web LLM', data.lanes.settings.webllm_enabled ? `on · ${data.lanes.settings.webllm_provider}` : 'off'], ['Claude Code', data.lanes.settings.claude_code_enabled ? `on · ${data.lanes.settings.claude_code_model}` : 'off'], ['Strict alignment', data.lanes.settings.strict_alignment ? 'on' : 'off'], ['Ollama', data.lanes.settings.ollama_enabled ? 'on' : 'off']] as [string, string][]).map(([k, v]) => (
              <span key={k} style={{ border: '3px solid var(--ink)', padding: '4px 14px', background: v.startsWith('on') ? 'var(--paper-hi)' : 'var(--paper-lo)' }}>{k}: {v}</span>
            ))}
          </div>
        )}
        <div style={{ fontSize: 26 }}>Quaero always goes first: while it has a request in flight, Noema skips the web LLM and falls through. Realtime nudge reasoning is served before background classification.</div>
      </div>
      <div style={{ display: 'grid', gridTemplateColumns: '1.25fr 1fr', gap: 60 }}>
        <div className="card" style={{ padding: 40, display: 'flex', flexDirection: 'column', gap: 40 }}>
          <div className="label">MODEL TIERS · FALLBACK CHAINS</div>
          {chains.map((c) => (
            <div key={c.name} style={{ display: 'flex', flexDirection: 'column', gap: 18 }}>
              <div className="display" style={{ fontSize: 54, letterSpacing: 0 }}>{c.name}</div>
              <div style={{ display: 'flex', alignItems: 'center', gap: 18, flexWrap: 'wrap' }}>
                {c.steps.map((s, i) => (
                  <div key={s.m} style={{ display: 'flex', alignItems: 'center', gap: 18 }}>
                    <div style={{ minHeight: 90, display: 'flex', flexDirection: 'column', justifyContent: 'center', padding: '8px 26px', border: `5px ${s.live ? 'solid' : 'dashed'} var(--ink)`, background: s.live ? 'var(--intent)' : s.down ? 'var(--drift)' : 'var(--paper-hi)', color: s.live ? 'var(--paper)' : 'var(--ink)' }}>
                      <span style={{ fontSize: 34, fontWeight: 500 }}>{s.m}</span><span style={{ fontSize: 22 }}>{s.st}</span>
                    </div>
                    <span style={{ fontSize: 44 }}>{i < c.steps.length - 1 ? '→' : ''}</span>
                  </div>
                ))}
              </div>
            </div>
          ))}
          <div style={{ fontSize: 26 }}>Last step is always deterministic-only: if no model answers, drift is still scored and the policy gate still runs.</div>
        </div>
        <div className="card" style={{ padding: 40, display: 'flex', flexDirection: 'column', gap: 30 }}>
          <div className="label">QUOTA · TODAY</div>
          {quotas.length === 0 && <div style={{ fontSize: 28 }}>no quota data yet</div>}
          {quotas.map((q) => (
            <div key={q.n} style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 30 }}><span>{q.n} · requests</span><span>{q.used.toLocaleString()} / {q.limit.toLocaleString()}</span></div>
              <div style={{ height: 46, border: '4px solid var(--ink)', background: 'var(--paper)' }}><div style={{ height: '100%', width: `${Math.min(100, (q.used / q.limit) * 100)}%`, background: q.used / q.limit > 0.75 ? 'var(--drift)' : 'var(--intent)' }} /></div>
            </div>
          ))}
          <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 30 }}><span>tokens · today</span><span>{tokensToday.toLocaleString()}</span></div>
        </div>
      </div>
      <div style={{ display: 'grid', gridTemplateColumns: '1.25fr 1fr', gap: 60 }}>
        <div className="card" style={{ padding: 40, display: 'flex', flexDirection: 'column', gap: 22 }}>
          <div className="label">LATENCY · LAST 30 DAYS</div>
          {latencyRows.length === 0 && <div style={{ fontSize: 28 }}>no model calls recorded yet</div>}
          {latencyRows.map((r) => (
            <div key={r.model} style={{ display: 'grid', gridTemplateColumns: '460px 1fr 360px', alignItems: 'center', gap: 30, borderTop: '3px dashed var(--ink)', paddingTop: 16 }}>
              <div style={{ fontSize: 30, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{r.model}</div>
              <div style={{ position: 'relative', height: 36, border: '3px solid var(--ink)', background: 'var(--paper)', overflow: 'hidden' }} aria-hidden="true">
                <div style={{ position: 'absolute', top: 0, bottom: 0, left: 0, width: pct(r.latency?.p95), background: 'var(--paper-lo)', borderRight: '3px solid var(--ink)' }} />
                <div style={{ position: 'absolute', top: 0, bottom: 0, left: 0, width: pct(r.latency?.p50), background: 'var(--intent)' }} />
              </div>
              <div style={{ fontSize: 28, textAlign: 'right' }}>p50 {ms(r.latency?.p50)} · p95 {ms(r.latency?.p95)}</div>
            </div>
          ))}
        </div>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 50 }}>
          <div className="card" style={{ padding: 40, display: 'flex', flexDirection: 'column', gap: 18 }}>
            <div className="label">PROVIDER HEALTH</div>
            {health.map((p, i) => (
              <div key={i} style={{ display: 'flex', alignItems: 'center', gap: 20, fontSize: 30 }}>
                <div style={{ minWidth: 110, textAlign: 'center', border: '4px solid var(--ink)', background: p.s === 'OK' ? 'var(--recovered)' : 'var(--drift)', color: p.s === 'OK' ? 'var(--paper)' : 'var(--ink)', padding: '4px 10px', fontSize: 24 }}>{p.s}</div>
                <div style={{ flex: 1 }}>{p.n}</div><div style={{ fontSize: 24 }}>{p.d}</div>
              </div>
            ))}
          </div>
          <div style={{ background: 'var(--ink)', color: 'var(--paper)', border: '5px solid var(--ink)', padding: 40, display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '26px 40px' }}>
            <div className="label" style={{ gridColumn: '1/-1' }}>BENCHMARK · {data?.bench ? `${data.bench.test_set ?? 'latest run'}` : 'no run recorded'}</div>
            {bench.map((b) => <div key={b.l}><div style={{ fontSize: 84, fontWeight: 500, lineHeight: 1 }}>{b.v}</div><div style={{ fontSize: 24, marginTop: 6 }}>{b.l}</div></div>)}
          </div>
        </div>
      </div>
    </div>
  );
}
