import { cleanup, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import AIDashboard from './index';

const SEG_DIRECT = {
  timestamp: '2026-09-04T10:02:00Z', end: '2026-09-04T10:02:30Z', seconds: 30,
  application: 'Brave', activity: 'Claude Code session', title: 'Claude Code session',
  productivity: 'productive', confidence: 0.85, classification_status: 'classified',
  evidence_quality: 'strong', inherited_from: null, meaningful_session_id: null,
};

const SEG_INHERITED = {
  timestamp: '2026-09-04T10:00:00Z', end: '2026-09-04T10:00:30Z', seconds: 30,
  application: 'Firefox', activity: 'Cinepolis showtimes', title: 'Cinepolis showtimes',
  productivity: 'productive', confidence: 0.78, classification_status: 'classified',
  evidence_quality: 'moderate', inherited_from: 'feedface0001', meaningful_session_id: 'feedface0001',
  provenance: 'verified_extension',
};

const SEG_LEGACY = {
  timestamp: '2026-09-04T10:01:00Z', end: '2026-09-04T10:01:30Z', seconds: 30,
  application: 'Firefox', activity: 'Trailers roundup', title: 'Trailers roundup',
  productivity: 'productive', confidence: 0.78, classification_status: 'classified',
  evidence_quality: 'moderate', inherited_from: 'feedface0001', meaningful_session_id: 'feedface0001',
};

function mockFetch(timeline: unknown[] = [SEG_DIRECT, SEG_INHERITED, SEG_LEGACY]) {
  const fn = vi.fn(async (url: unknown) => {
    const target = String(url);
    let body: unknown = {};
    if (target.includes('/api/daemon/health')) {
      body = { status: 'running', provider: 'ollama', provider_model: 'test-model' };
    } else if (target.includes('/api/dashboard/summary')) {
      body = {
        daily: {
          date: '2026-09-04', timezone: 'Asia/Kolkata', range: 'today',
          total_time: 90, productive_time: 90, distractive_time: 0, neutral_time: 0,
          unclassified_time: 0, afk_time: 0, active_time: 90, classified_time: 90,
          productivity_score: 100, applications: [], distractions: [],
          timeline,
        },
      };
    } else if (target.includes('/api/events')) body = { events: [] };
    else if (target.includes('/api/sessions')) body = { sessions: [] };
    else if (target.includes('/api/meaningful-sessions')) body = { sessions: [] };
    else if (target.includes('/api/classifications')) body = { classifications: [] };
    else if (target.includes('/api/behavior')) body = { observations: [] };
    else if (target.includes('/api/interventions')) body = { interventions: [] };
    else if (target.includes('/api/outcomes')) body = { outcomes: [] };
    else if (target.includes('/api/memories')) body = { memories: [] };
    else if (target.includes('/api/browser/registrations')) body = { registrations: [] };
    else if (target.includes('/api/classification/status')) {
      body = {
        enabled: true, intervalMinutes: 20, status: 'idle', presence: null,
        lastRunAt: null, lastRunDurationSeconds: null, nextRunAt: null,
        processedLastRun: 0, failedLastRun: 0, lastError: null,
        pendingCount: 0, processingCount: 0, failedCount: 0, classifiedCount: 2,
        totalEligibleCount: 2, coveragePercent: 100, latestClassificationAt: null,
        provider: 'ollama', model: 'test-model', quota: null,
      };
    } else if (target.includes('/api/dashboard/recent-activity')) {
      body = { range: 'today', timezone: 'Asia/Kolkata', activities: [] };
    } else if (target.includes('/api/dashboard/recent-classifications')) {
      body = { classifications: [] };
    } else if (target.includes('/api/presence/current')) {
      body = {
        state: 'active', last_input_at: null, idle_seconds: 5,
        as_of: '2026-09-04T10:05:00Z', stale: false, current_session: null,
        afk_since: null, last_activity: null,
      };
    }
    return { ok: true, status: 200, text: async () => JSON.stringify(body) } as Response;
  });
  vi.stubGlobal('fetch', fn);
  return fn;
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('AIDashboard Today activity provenance', () => {
  it('marks verified extensions distinctly from direct classifications', async () => {
    mockFetch();
    render(<AIDashboard />);
    // All three rows share the episode verdict honestly in the verdict column.
    expect(await screen.findAllByText('PRODUCTIVE')).toHaveLength(3);
    // ... but only the directly classified row claims CLASSIFIED status.
    expect(screen.getByText('CLASSIFIED')).toBeInTheDocument();
    const verified = screen.getByText('VERIFIED EXT');
    expect(verified).toBeInTheDocument();
    expect(verified.getAttribute('title')).toMatch(/did not directly inspect/);
    // Legacy payloads without provenance keep the EPISODE fallback.
    expect(screen.getByText('EPISODE')).toBeInTheDocument();
  });

  it('keeps the inherited confidence value with episode provenance', async () => {
    mockFetch();
    render(<AIDashboard />);
    const table = (await screen.findByText('Cinepolis showtimes')).closest('.table-row');
    if (!(table instanceof HTMLElement)) throw new Error('inherited row missing');
    // The parent's 78% is shown, labelled by the VERIFIED EXT status in the same row.
    expect(within(table).getByText(/78%/)).toBeInTheDocument();
    expect(within(table).getByText('VERIFIED EXT')).toBeInTheDocument();
    const direct = (await screen.findByText('Claude Code session')).closest('.table-row');
    if (!(direct instanceof HTMLElement)) throw new Error('direct row missing');
    expect(within(direct).getByText(/85%/)).toBeInTheDocument();
    expect(within(direct).getByText('CLASSIFIED')).toBeInTheDocument();
  });

  it('preserves four independent member verdicts from one episode', async () => {
    // Acceptance case: A/B/C/D share meaningful_session_id E but carry
    // their own direct verdicts. The UI must not average, copy, collapse,
    // or re-mark them as episode verdicts.
    const episode = 'ep-accept-0001';
    const members = [
      { timestamp: '2026-09-04T10:00:00Z', application: 'Code', activity: 'Alpha work', title: 'Alpha work', seconds: 30, productivity: 'productive', confidence: 0.82, classification_status: 'classified', inherited_from: null, meaningful_session_id: episode, provenance: 'direct' },
      { timestamp: '2026-09-04T10:01:00Z', application: 'Code', activity: 'Beta work', title: 'Beta work', seconds: 30, productivity: 'productive', confidence: 0.70, classification_status: 'classified', inherited_from: null, meaningful_session_id: episode, provenance: 'direct' },
      { timestamp: '2026-09-04T10:02:00Z', application: 'Firefox', activity: 'Gamma docs', title: 'Gamma docs', seconds: 30, productivity: 'neutral', confidence: 0.78, classification_status: 'classified', inherited_from: null, meaningful_session_id: episode, provenance: 'direct' },
      { timestamp: '2026-09-04T10:03:00Z', application: 'Arcade', activity: 'Delta game', title: 'Delta game', seconds: 30, productivity: 'distractive', confidence: 0.91, classification_status: 'classified', inherited_from: null, meaningful_session_id: episode, provenance: 'direct' },
    ];
    mockFetch(members);
    render(<AIDashboard />);
    // Same category + different confidence stays distinct (A vs B)...
    expect(await screen.findByText('Alpha work')).toBeInTheDocument();
    // ... and same confidence + different category stays distinct (B vs C).
    const rows = await screen.findAllByText('CLASSIFIED');
    expect(rows).toHaveLength(4);
    expect(screen.queryByText('EPISODE')).not.toBeInTheDocument();
    expect(screen.queryByText('VERIFIED EXT')).not.toBeInTheDocument();
    const texts = ['Alpha work', 'Beta work', 'Gamma docs', 'Delta game'];
    for (const text of texts) {
      expect(await screen.findByText(text)).toBeInTheDocument();
    }
    const rowA = (await screen.findByText('Alpha work')).closest('.table-row') as HTMLElement;
    const rowB = (await screen.findByText('Beta work')).closest('.table-row') as HTMLElement;
    const rowC = (await screen.findByText('Gamma docs')).closest('.table-row') as HTMLElement;
    const rowD = (await screen.findByText('Delta game')).closest('.table-row') as HTMLElement;
    expect(within(rowA).getByText(/82%/)).toBeInTheDocument();
    expect(within(rowB).getByText(/70%/)).toBeInTheDocument();
    expect(within(rowC).getByText(/78%/)).toBeInTheDocument();
    expect(within(rowD).getByText(/91%/)).toBeInTheDocument();
    expect(within(rowA).getByText('PRODUCTIVE')).toBeInTheDocument();
    expect(within(rowB).getByText('PRODUCTIVE')).toBeInTheDocument();
    expect(within(rowC).getByText('NEUTRAL')).toBeInTheDocument();
    expect(within(rowD).getByText('DISTRACTIVE')).toBeInTheDocument();
  });

  it('renders pending, failed and afk states honestly', async () => {
    mockFetch([
      { timestamp: '2026-09-04T10:00:00Z', application: 'Firefox', activity: 'Mystery', title: 'Mystery', seconds: 30, productivity: 'unclassified', confidence: 0, classification_status: 'pending', inherited_from: null, meaningful_session_id: null, provenance: 'none' },
      { timestamp: '2026-09-04T10:01:00Z', application: 'Firefox', activity: 'Broken', title: 'Broken', seconds: 30, productivity: 'neutral', confidence: 0, classification_status: 'classification_failed', inherited_from: null, meaningful_session_id: null, provenance: 'none' },
      { timestamp: '2026-09-04T10:02:00Z', application: 'Firefox', activity: 'Idle — no user input', title: 'AFK slice', seconds: 30, productivity: 'afk', confidence: 0, classification_status: 'afk', inherited_from: null, meaningful_session_id: null },
    ]);
    render(<AIDashboard />);
    expect(await screen.findByText('Mystery')).toBeInTheDocument();
    // STATUS column: PENDING / FAILED / AFK — AFK must never read CLASSIFIED.
    const pendingRow = (await screen.findByText('Mystery')).closest('.table-row') as HTMLElement;
    const failedRow = (await screen.findByText('Broken')).closest('.table-row') as HTMLElement;
    const afkRow = (await screen.findByText('AFK slice')).closest('.table-row') as HTMLElement;
    const badges = (row: HTMLElement) => within(row).getAllByText(/PENDING|FAILED|AFK/);
    expect(badges(pendingRow).some((b) => b.textContent === 'PENDING')).toBe(true);
    expect(badges(failedRow).some((b) => b.textContent === 'FAILED')).toBe(true);
    expect(badges(afkRow).some((b) => b.textContent === 'AFK')).toBe(true);
    expect(within(afkRow).queryByText('CLASSIFIED')).not.toBeInTheDocument();
  });
});
