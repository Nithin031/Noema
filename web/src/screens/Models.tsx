import { useMemo } from 'react';
import { api, request, type ModelPerfRow, type QuotaRow } from '../api/noema';
import { useFetch } from '../hooks/useFetch';
import { Hatch, Title } from '../components/ui';

type Lane = { lane: string; model: string; state: string; queued: number; calls: number; failures: number; yields: number; last_error: string | null; last_latency_ms: number | null };
type Lanes = {
  order: string[]; lanes: Lane[]; chain: { name: string; model: string }[];
  settings: { ollama_enabled: boolean; webllm_enabled: boolean; webllm_provider: string; claude_code_enabled: boolean; claude_code_model: string };
};

const LABEL: Record<string, string> = { web_llm: 'Web LLM', claude_code: 'Claude Code', gemini: 'Gemini', openrouter: 'OpenRouter' };
const ms = (v: number | null | undefined) => (v === null || v === undefined ? '—' : v >= 1000 ? `${(v / 1000).toFixed(1)} s` : `${Math.round(v)} ms`);

/** Everything on this page comes from the running daemon, so it cannot disagree with the backend. */
export default function Models() {
  const { data, error, loading } = useFetch(async () => {
    const settle = async <T,>(p: Promise<T>) => { try { return await p; } catch { return null; } };
    const [lanes, today, quotas, status] = await Promise.all([
      settle(request<Lanes>('/api/llm/lanes')), settle(api.metricsModels(1)), settle(api.quotas()), settle(api.classificationStatus()),
    ]);
    return { lanes, models: (today?.models ?? []).filter((m) => m.provider !== 'ollama'), quotas, status };
  });

  const perf = useMemo(() => {
    const m = new Map<string, ModelPerfRow>();
    for (const r of data?.models ?? []) if (r.model) m.set(r.model, r);
    return m;
  }, [data]);
  const quotaOf = (model: string): QuotaRow | undefined => data?.quotas?.models.find((q) => q.model === model);
  const lastModel = data?.status?.model ?? null;           // the model that answered the most recent run
  const chain = data?.lanes?.chain ?? [];
  const s = data?.lanes?.settings;

  const gemini = chain.filter((c) => c.name === 'gemini').map((c) => ({ ...c, q: quotaOf(c.model) })).filter((c) => c.q && c.q.rpd.limit > 0);
  const latency = [...perf.values()].filter((r) => r.latency && (r.latency.n ?? 0) > 0);
  const maxMs = Math.max(1, ...latency.map((r) => Math.max(r.latency?.p50 ?? 0, r.latency?.p95 ?? 0)));
  const pct = (v: number | null | undefined) => `${Math.min(100, Math.max(0, ((v ?? 0) / maxMs) * 100))}%`;

  return (
    <div className="page-body">
      <Title aside="Which AI sorts your activity into categories, in order.">Models</Title>
      {error && <div role="alert" className="stamp drifting" style={{ alignSelf: 'flex-start' }}>! {error}</div>}
      {loading && !data && <Hatch w="100%" h={400} />}

      <div className="card" style={{ padding: 40, display: 'flex', flexDirection: 'column', gap: 24 }}>
        <div className="label">ANSWER ORDER · FIRST THAT ANSWERS WINS</div>
        <div style={{ display: 'flex', alignItems: 'stretch', gap: 18, flexWrap: 'wrap' }}>
          {chain.map((c, i) => {
            const p = perf.get(c.model);
            const live = c.model === lastModel;
            return (
              <div key={`${c.name}-${c.model}`} style={{ display: 'flex', alignItems: 'center', gap: 18 }}>
                <div style={{ minWidth: 380, padding: '14px 26px', border: `5px ${live ? 'solid' : 'dashed'} var(--ink)`, background: live ? 'var(--intent)' : 'var(--paper-hi)', color: live ? 'var(--paper)' : 'var(--ink)' }}>
                  <div style={{ fontSize: 22, letterSpacing: '.1em' }}>{i + 1} · {(LABEL[c.name] ?? c.name).toUpperCase()}</div>
                  <div style={{ fontSize: 32, fontWeight: 500 }}>{c.model}</div>
                  <div style={{ fontSize: 24 }}>{live ? 'answered the last run · ' : ''}{p ? `${p.requests ?? 0} calls today` : 'no calls today'}</div>
                </div>
                <span style={{ fontSize: 44 }}>{i < chain.length - 1 ? '→' : ''}</span>
              </div>
            );
          })}
          {chain.length === 0 && !loading && <div style={{ fontSize: 28 }}>No model chain reported by the daemon.</div>}
        </div>
        {s && (
          <div style={{ display: 'flex', gap: 16, flexWrap: 'wrap', fontSize: 26 }}>
            {([['Web LLM', s.webllm_enabled ? `on · ${s.webllm_provider}` : 'off'], ['Claude Code', s.claude_code_enabled ? `on · ${s.claude_code_model}` : 'off'], ['Ollama', s.ollama_enabled ? 'on' : 'off']] as [string, string][]).map(([k, v]) => (
              <span key={k} style={{ border: '3px solid var(--ink)', padding: '4px 14px', background: v.startsWith('on') ? 'var(--paper-hi)' : 'var(--paper-lo)' }}>{k}: {v}</span>
            ))}
          </div>
        )}
        <div style={{ fontSize: 26 }}>AI is only used to name what an activity is (Coding, Video, …). It never judges you.</div>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 60 }}>
        <div className="card" style={{ padding: 40, display: 'flex', flexDirection: 'column', gap: 22 }}>
          <div className="label">SPEED TODAY</div>
          {latency.length === 0 && <div style={{ fontSize: 28 }}>no model calls yet today</div>}
          {latency.map((r) => (
            <div key={r.model} style={{ display: 'grid', gridTemplateColumns: '400px 1fr 330px', alignItems: 'center', gap: 24, borderTop: '3px dashed var(--ink)', paddingTop: 16 }}>
              <div style={{ fontSize: 28, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{r.model}</div>
              <div style={{ position: 'relative', height: 36, border: '3px solid var(--ink)', background: 'var(--paper)', overflow: 'hidden' }} aria-hidden="true">
                <div style={{ position: 'absolute', top: 0, bottom: 0, left: 0, width: pct(r.latency?.p95), background: 'var(--paper-lo)', borderRight: '3px solid var(--ink)' }} />
                <div style={{ position: 'absolute', top: 0, bottom: 0, left: 0, width: pct(r.latency?.p50), background: 'var(--intent)' }} />
              </div>
              <div style={{ fontSize: 26, textAlign: 'right' }}>typical {ms(r.latency?.p50)}{r.latency?.p95 ? ` · slow ${ms(r.latency.p95)}` : ''}</div>
            </div>
          ))}
        </div>

        <div className="card" style={{ padding: 40, display: 'flex', flexDirection: 'column', gap: 26 }}>
          <div className="label">GEMINI FREE-TIER QUOTA · TODAY</div>
          {gemini.length === 0 && <div style={{ fontSize: 28 }}>no quota data</div>}
          {gemini.map((c) => (
            <div key={c.model} style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 28 }}><span>{c.model}</span><span>{c.q?.rpd.used ?? 0} / {c.q?.rpd.limit}</span></div>
              <div style={{ height: 36, border: '4px solid var(--ink)', background: 'var(--paper)' }}>
                <div style={{ height: '100%', width: `${Math.min(100, ((c.q?.rpd.used ?? 0) / (c.q?.rpd.limit || 1)) * 100)}%`, background: 'var(--intent)' }} />
              </div>
            </div>
          ))}
          <div style={{ fontSize: 24 }}>Gemini is the last resort, so this barely moves while the other models answer.</div>
        </div>
      </div>
    </div>
  );
}
