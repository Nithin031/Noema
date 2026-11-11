import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import App from './App';

type Json = Record<string, unknown>;

function mockBackend(routes: Record<string, Json | number>) {
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input).replace(/^https?:\/\/[^/]+/, '').split('?')[0];
    const hit = routes[url];
    if (hit === undefined) return new Response(JSON.stringify({ error: 'not_found' }), { status: 404 });
    if (typeof hit === 'number') return new Response('{}', { status: hit });
    return new Response(JSON.stringify(hit), { status: 200 });
  }));
}

describe('App shell', () => {
  beforeEach(() => { window.location.hash = '#/command'; });
  afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

  it('shows the offline state when the daemon does not answer', async () => {
    mockBackend({});
    render(<App />);
    expect(await screen.findByText(/DAEMON OFFLINE/i)).toBeInTheDocument();
    expect(screen.getAllByText(/daemon · offline/i).length).toBeGreaterThan(0);
  });

  it('shows the empty state, not fake data, when there is no intent or activity', async () => {
    mockBackend({
      '/api/daemon/health': { status: 'ok' }, '/api/realtime/status': {}, '/api/detections': { detections: [] },
      '/api/interventions/feed': { interventions: [] }, '/api/break': { break: { active: false } },
      '/api/intents': { intents: [] }, '/api/dashboard/summary': { daily: { timeline: [] } },
      '/api/outcomes': { outcomes: [] }, '/api/meme-assets/stats': { assets: 0, favorites: 0, by_sentiment: {}, curated_assets: 0, active_responses: 0 },
    });
    render(<App />);
    expect(await screen.findByText('No rope yet.')).toBeInTheDocument();
  });

  it('shows one plain status word from the live drift score', async () => {
    mockBackend({
      '/api/daemon/health': { status: 'ok' },
      '/api/realtime/status': { tracker: { last_score: 0.31, enter_threshold: 0.6, exit_threshold: 0.45 } },
      '/api/detections': { detections: [{ timestamp: '2026-11-11T10:00:00Z', model_verdict: 'NOT_DISTRACTED', reason: 'train.py matches the goal' }] },
      '/api/interventions/feed': { interventions: [] }, '/api/break': { break: { active: false } },
      '/api/intents': { intents: [{ id: 'i1', text: 'finish the ML assignment', created_at: '2026-11-11T09:40:00Z' }] },
      '/api/dashboard/summary': { daily: { timeline: [] } }, '/api/outcomes': { outcomes: [] },
      '/api/meme-assets/stats': { assets: 3, favorites: 0, by_sentiment: {}, curated_assets: 0, active_responses: 0 },
    });
    render(<App />);
    expect(await screen.findByText('finish the ML assignment')).toBeInTheDocument();
    await waitFor(() => expect(screen.getAllByText('ON TASK').length).toBeGreaterThan(0));
    expect(screen.getByText(/doing what you said/i)).toBeInTheDocument();
  });

  it('opens the nudge overlay for a delivered, unanswered nudge', async () => {
    mockBackend({
      '/api/daemon/health': { status: 'ok' }, '/api/realtime/status': { tracker: { last_score: 0.8 } }, '/api/detections': { detections: [] },
      '/api/interventions/feed': { interventions: [{ id: 'n1', created_at: new Date().toISOString(), delivered: true, interacted: false, message: { title: 'You said you were locking in.', body: 'Bold of you.' } }] },
      '/api/break': { break: { active: false } }, '/api/intents': { intents: [{ id: 'i1', text: 'study', created_at: '2026-11-11T09:40:00Z' }] },
      '/api/dashboard/summary': { daily: { timeline: [] } }, '/api/outcomes': { outcomes: [] },
      '/api/meme-assets/stats': { assets: 0, favorites: 0, by_sentiment: {}, curated_assets: 0, active_responses: 0 },
      '/api/responses/effectiveness': { effectiveness: [] },
    });
    render(<App />);
    expect(await screen.findByRole('dialog', { name: /nudge/i })).toBeInTheDocument();
    expect(screen.getByText('You said you were locking in.')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /LOCKING IN/ })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /GIVE ME 5 MIN/ })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /THIS IS INTENTIONAL/ })).toBeInTheDocument();
  });
});
