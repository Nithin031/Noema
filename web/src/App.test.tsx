import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import App from './App';

type Json = Record<string, unknown>;

function mockBackend(routes: Record<string, Json>) {
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input).replace(/^https?:\/\/[^/]+/, '').split('?')[0];
    const hit = routes[url];
    if (hit === undefined) return new Response(JSON.stringify({ error: 'not_found' }), { status: 404 });
    return new Response(JSON.stringify(hit), { status: 200 });
  }));
}

const seg = (app: string, title: string, minutes: number) => ({
  timestamp: '2026-11-11T09:00:00', end: '2026-11-11T09:30:00', seconds: minutes * 60, application: app, title,
  activity: 'Coding', productivity: 'neutral', confidence: 0.9,
});

const BACKGROUND = { enabled: true, now: { open: [], audio: [], audio_supported: true }, today: [], error: null };

describe('App shell (plain analytics)', () => {
  beforeEach(() => { window.location.hash = '#/today'; });
  afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

  it('tells you when the daemon is not answering', async () => {
    mockBackend({});
    render(<App />);
    const alerts = await screen.findAllByRole('alert');
    expect(alerts.some((a) => /daemon is not answering/i.test(a.textContent ?? ''))).toBe(true);
  });

  it('shows the day as plain numbers, with no judging words', async () => {
    mockBackend({
      '/api/daemon/health': { status: 'ok' },
      '/api/dashboard/summary': { daily: { timeline: [seg('WindowsTerminal', '✳ Whether to keep', 30)] } },
      '/api/background': BACKGROUND,
    });
    render(<App />);
    expect((await screen.findAllByText('Terminal')).length).toBeGreaterThan(0);   // friendly app name
    for (const word of [/on task/i, /off task/i, /drift/i, /distract/i, /nudge/i]) {
      expect(screen.queryByText(word)).not.toBeInTheDocument();
    }
  });

  it('has only the three analytics pages in the nav', async () => {
    mockBackend({ '/api/daemon/health': { status: 'ok' }, '/api/dashboard/summary': { daily: { timeline: [] } }, '/api/background': BACKGROUND });
    render(<App />);
    const nav = await screen.findByRole('navigation', { name: 'Pages' });
    expect(nav.querySelectorAll('a')).toHaveLength(3);
    expect(nav).toHaveTextContent('Today');
    expect(nav).toHaveTextContent('Week');
    expect(nav).toHaveTextContent('Models');
  });
});
