import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import MemeCenterPage from './index';

const ASSETS = [
  {
    id: 'aaa', filename: 'image_1.jpg', ocr_text: 'STUDY HARD', corrected_text: 'Study hard',
    display_text: 'Study hard', sentiment: 'positive', tags: [], favorite: false, enabled: true,
    width: 64, height: 48, byte_size: 1000, image_format: 'jpeg', response_count: 1, curated: true,
    responses: [{ id: 'resp-1', kind: 'MEME', tone: 'sarcastic', enabled: true }],
  },
  {
    id: 'bbb', filename: 'image_2.png', ocr_text: 'REST NOW', corrected_text: 'Rest now',
    display_text: 'Rest now', sentiment: 'neutral', tags: [], favorite: false, enabled: true,
    width: 32, height: 24, byte_size: 500, image_format: 'png', response_count: 0, curated: false,
    responses: [],
  },
];

const RESPONSES = [
  {
    id: 'resp-1', kind: 'MEME', tone: 'sarcastic', title: 'Bro', body_template: 'Back to {goal}.',
    asset_id: 'aaa', severity_min: 2, severity_max: 4, context_tags: ['study'], cooldown_seconds: 60,
    enabled: true, times_shown: 10, times_clicked: 4, recovery_count: 6, recovery_rate: 0.6,
    last_shown_at: '2026-09-01T10:00:00Z',
  },
];

const FEED = [
  {
    id: 'int-1', session_id: 'sess-1', mode: 'MEME', status: 'DELIVERED',
    reason: 'Sustained off-goal browsing during study time.',
    created_at: '2026-09-01T10:00:00Z',
    response_id: 'resp-1', response_kind: 'MEME',
    message: { title: 'Bro', body: 'Back to Power Electronics.' },
    intervention_decision: {
      should_intervene: true, intervention_type: 'MEME_NUDGE', severity: 3,
      tone: 'PLAYFUL', response_class: 'MEME', use_meme: true, confidence: 0.8,
    },
    meme_asset: { id: 'aaa', filename: 'image_1.jpg' },
    delivered: true, interacted: true, actions: ['sent', 'lock_in'],
    outcome: {
      id: 'out-1', intervention_id: 'int-1', intervention_time: '2026-09-01T10:00:00Z',
      recovery_status: 'RECOVERED', recovery_time: null, recovery_session_id: null,
      recovery_duration_seconds: 252, intervention_type: 'MEME_NUDGE', meme_id: 'aaa',
      detection_id: null, response_id: 'resp-1', delivery_state: 'sent',
      user_action: 'lock_in', attribution: 'direct', break_context: false,
    },
    feedback: [],
  },
];

const EFFECTIVENESS = [
  { id: 'resp-1', kind: 'MEME', tone: 'sarcastic', times_shown: 10, times_clicked: 4, recovery_count: 6, recovery_rate: 0.6 },
];

function mockFetch(impl: (url: string, init?: RequestInit) => unknown) {
  const fn = vi.fn(async (url: unknown, init?: RequestInit) => {
    const body = impl(String(url), init);
    return { ok: true, status: 200, text: async () => JSON.stringify(body) } as Response;
  });
  vi.stubGlobal('fetch', fn);
  return fn;
}

function baseRoutes(url: string, init?: RequestInit) {
  const method = init?.method ?? 'GET';
  if (url.includes('/api/meme-assets/stats')) {
    return { assets: 2, favorites: 0, by_sentiment: { positive: 1, neutral: 1 }, curated_assets: 1, active_responses: 1 };
  }
  if (url.includes('/api/interventions/feed')) return { interventions: FEED };
  if (url.includes('/api/responses/effectiveness')) return { effectiveness: EFFECTIVENESS };
  if (url.includes('/api/responses') && method === 'GET') return { responses: RESPONSES };
  if (url.includes('/api/meme-assets/aaa/enabled')) {
    const body = JSON.parse(String(init?.body ?? '{}')) as { enabled?: boolean };
    return { asset: { ...ASSETS[0], enabled: body.enabled ?? true } };
  }
  if (url.includes('/api/meme-assets/aaa/tags')) {
    const body = JSON.parse(String(init?.body ?? '{}')) as { tags?: string[] };
    return { asset: { ...ASSETS[0], tags: body.tags ?? [] } };
  }
  if (url.includes('/api/meme-assets/aaa/favorite')) return { asset: { ...ASSETS[0], favorite: true } };
  if (url.includes('/api/meme-assets/aaa') && !url.includes('/responses')) return { asset: ASSETS[0] };
  if (url.includes('/api/meme-assets') && method === 'GET') {
    const params = new URLSearchParams(url.split('?')[1] ?? '');
    const q = (params.get('q') ?? '').toLowerCase();
    const sentiment = params.get('sentiment') ?? '';
    const items = ASSETS.filter((a) => (!q || (a.display_text ?? '').toLowerCase().includes(q)) && (!sentiment || a.sentiment === sentiment));
    return { items, total: items.length };
  }
  if (url.includes('/api/responses') && method === 'POST') return { response: RESPONSES[0] };
  throw new Error(`unexpected ${method} ${url}`);
}

class MockIntersectionObserver {
  observe() { /* grid loads first page without scrolling */ }
  unobserve() { /* noop */ }
  disconnect() { /* noop */ }
}

beforeEach(() => {
  vi.stubGlobal('IntersectionObserver', MockIntersectionObserver);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('MemeCenterPage', () => {
  it('renders the asset grid with counts', async () => {
    mockFetch(baseRoutes);
    render(<MemeCenterPage />);
    expect(await screen.findByText('Meme Center')).toBeInTheDocument();
    expect(await screen.findByText('image_1.jpg', { exact: false })).toBeInTheDocument();
    expect(screen.getByLabelText('2 assets')).toBeInTheDocument();
  });

  it('searches by OCR text', async () => {
    const spy = mockFetch(baseRoutes);
    render(<MemeCenterPage />);
    await screen.findByText('image_1.jpg', { exact: false });
    fireEvent.change(screen.getByLabelText(/search assets/i), { target: { value: 'rest' } });
    await waitFor(() => {
      const calls = spy.mock.calls.map((c) => String(c[0]));
      expect(calls.some((url) => url.includes('q=rest'))).toBe(true);
    });
  });

  it('filters by sentiment', async () => {
    mockFetch(baseRoutes);
    render(<MemeCenterPage />);
    await screen.findByText('image_1.jpg', { exact: false });
    fireEvent.click(screen.getByRole('button', { name: 'NEUTRAL' }));
    await waitFor(() => expect(screen.queryByText('image_1.jpg', { exact: false })).not.toBeInTheDocument());
    expect(screen.getByText('image_2.png', { exact: false })).toBeInTheDocument();
  });

  it('toggles favorite with rollback surface on success', async () => {
    mockFetch(baseRoutes);
    render(<MemeCenterPage />);
    const card = (await screen.findByText('image_1.jpg', { exact: false })).closest('article');
    if (!card) throw new Error('card missing');
    fireEvent.click(within(card).getByLabelText(/favorite image_1/i));
    await waitFor(() => expect(within(card).getByLabelText(/unfavorite/i)).toBeInTheDocument());
  });

  it('opens the detail panel with metadata and responses', async () => {
    mockFetch(baseRoutes);
    render(<MemeCenterPage />);
    const card = (await screen.findByText('image_1.jpg', { exact: false })).closest('article');
    if (!card) throw new Error('card missing');
    fireEvent.click(card);
    const dialog = await screen.findByRole('dialog', { name: /asset image_1/i });
    expect(within(dialog).getByText('POSITIVE')).toBeInTheDocument();
    expect(within(dialog).getByText(/resp-1/)).toBeInTheDocument();
    fireEvent.keyDown(window, { key: 'Escape' });
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  });

  it('opens the response editor from a card', async () => {
    mockFetch(baseRoutes);
    render(<MemeCenterPage />);
    const card = (await screen.findByText('image_2.png', { exact: false })).closest('article');
    if (!card) throw new Error('card missing');
    fireEvent.click(within(card).getByText('+ Response'));
    expect(await screen.findByRole('dialog', { name: /create response/i })).toBeInTheDocument();
    expect(screen.getByText(/preview · sample context/i)).toBeInTheDocument();
  });

  it('shows responses with effectiveness and data guards', async () => {
    mockFetch(baseRoutes);
    render(<MemeCenterPage />);
    fireEvent.click(screen.getByRole('tab', { name: 'Responses' }));
    expect(await screen.findByText('Bro')).toBeInTheDocument();
    const stat = screen.getByText(/Direct recovery/);
    expect(stat.parentElement?.textContent ?? '').toMatch(/60/);
  });

  it('clears the search from the toolbar', async () => {
    mockFetch(baseRoutes);
    render(<MemeCenterPage />);
    await screen.findByText('image_1.jpg', { exact: false });
    const input = screen.getByLabelText(/search assets/i) as HTMLInputElement;
    fireEvent.change(input, { target: { value: 'study' } });
    expect(await screen.findByLabelText('Clear search')).toBeInTheDocument();
    fireEvent.click(screen.getByLabelText('Clear search'));
    expect(input.value).toBe('');
  });

  it('shows linked responses and recent intervention use in the detail panel', async () => {
    mockFetch(baseRoutes);
    render(<MemeCenterPage />);
    const card = (await screen.findByText('image_1.jpg', { exact: false })).closest('article');
    if (!card) throw new Error('card missing');
    fireEvent.click(card);
    const dialog = await screen.findByRole('dialog', { name: /asset image_1/i });
    expect(within(dialog).getByText('RESPONSES USING THIS ASSET')).toBeInTheDocument();
    expect(within(dialog).getByText('Bro')).toBeInTheDocument();
    expect(within(dialog).getByText(/Selection reason: unavailable/)).toBeInTheDocument();
    expect(within(dialog).getByText('RECENT INTERVENTION USE')).toBeInTheDocument();
    expect(within(dialog).getByText(/RECOVERED · direct/)).toBeInTheDocument();
  });

  it('toggles asset enabled state from the detail panel', async () => {
    const spy = mockFetch(baseRoutes);
    render(<MemeCenterPage />);
    const card = (await screen.findByText('image_1.jpg', { exact: false })).closest('article');
    if (!card) throw new Error('card missing');
    fireEvent.click(card);
    const dialog = await screen.findByRole('dialog', { name: /asset image_1/i });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Disable' }));
    await waitFor(() => {
      const posts = spy.mock.calls.filter((c) => String(c[0]).includes('/enabled'));
      expect(posts.length).toBeGreaterThan(0);
      expect(String(posts[0]?.[1]?.body ?? '')).toContain('false');
    });
    expect(await within(dialog).findByText(/Disabled — kept for history/)).toBeInTheDocument();
  });

  it('saves asset tags from the detail panel', async () => {
    mockFetch(baseRoutes);
    render(<MemeCenterPage />);
    const card = (await screen.findByText('image_1.jpg', { exact: false })).closest('article');
    if (!card) throw new Error('card missing');
    fireEvent.click(card);
    const dialog = await screen.findByRole('dialog', { name: /asset image_1/i });
    fireEvent.change(within(dialog).getByLabelText(/tags for image_1/i), { target: { value: 'study, exams' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save tags' }));
    expect(await screen.findByText('Tags updated')).toBeInTheDocument();
  });

  it('shows enriched response metadata with cooldown and last use', async () => {
    mockFetch(baseRoutes);
    render(<MemeCenterPage />);
    fireEvent.click(screen.getByRole('tab', { name: 'Responses' }));
    expect(await screen.findByText('Bro')).toBeInTheDocument();
    expect(screen.getByText(/COOLDOWN 1m/)).toBeInTheDocument();
    expect(screen.getByText('Tags: study')).toBeInTheDocument();
    expect(screen.getByText(/Asset: image_1.jpg/)).toBeInTheDocument();
    expect(screen.getByText(/Last use 2026-09-01 10:00/)).toBeInTheDocument();
  });

  it('shows an empty state when the corpus is missing', async () => {
    mockFetch((url) => {
      if (url.includes('/api/meme-assets') && !url.includes('/stats')) return { items: [], total: 0 };
      return baseRoutes(url);
    });
    render(<MemeCenterPage />);
    expect(await screen.findByText(/run ingestion first/i)).toBeInTheDocument();
  });

  it('surfaces backend errors with dismiss', async () => {
    mockFetch(() => { throw new Error('daemon down'); });
    render(<MemeCenterPage />);
    expect(await screen.findByRole('alert')).toBeInTheDocument();
    fireEvent.click(screen.getByText('dismiss'));
    await waitFor(() => expect(screen.queryByRole('alert')).not.toBeInTheDocument());
  });
});
