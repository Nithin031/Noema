import { describe, expect, it } from 'vitest';
import type { DailySegment, InterventionFeedRow, OutcomeRow, MemeAsset } from '../api/noema';
import {
  cleanTitle, friendlyApp, attrOf, driftView, fmtMin, hotspots, hourly, kindOf, memeStats, openNudge, overallRecovery,
  recoveryByDay, splitCaption, stateFromScore, tally, toSegs, verdictOf, weekRows,
} from './adapt';
import { parseRoute } from './pages';
import { zoomFor } from './zoom';

const seg = (over: Partial<DailySegment>): DailySegment => ({
  timestamp: '2026-11-11T10:00:00', end: '2026-11-11T10:30:00', seconds: 1800, application: 'VS Code',
  activity: 'Coding', productivity: 'productive', confidence: 0.9, ...over,
});

describe('alignment mapping', () => {
  it('maps backend productivity to design alignment; unknown never drifts', () => {
    expect(kindOf({ productivity: 'productive' })).toBe('aligned');
    expect(kindOf({ productivity: 'distractive' })).toBe('drifting');
    expect(kindOf({ productivity: 'afk' })).toBe('afk');
    expect(kindOf({ productivity: 'neutral' })).toBe('unknown');
    expect(kindOf({ productivity: 'unclassified' })).toBe('unknown');
  });
});

describe('drift', () => {
  it('derives behavior state from the score and hysteresis thresholds', () => {
    expect(stateFromScore(null)).toBe('LOCKED_IN');
    expect(stateFromScore(0.2)).toBe('LOCKED_IN');
    expect(stateFromScore(0.5)).toBe('WOBBLING');
    expect(stateFromScore(0.6)).toBe('DRIFTING');
    expect(stateFromScore(0.9)).toBe('DRIFTING');
  });
  it('reads thresholds from the realtime tracker and clamps the score', () => {
    const v = driftView({ tracker: { last_score: 1.7, enter_threshold: 0.7, exit_threshold: 0.5 } });
    expect(v).toMatchObject({ score: 1, enter: 0.7, exit: 0.5, state: 'DRIFTING' });
    expect(driftView(null)).toMatchObject({ score: null, enter: 0.6, exit: 0.45 });
  });
  it('accepts only known Gemini verdicts', () => {
    expect(verdictOf({ model_verdict: 'distracted', reason: ' why ' })).toEqual({ verdict: 'DISTRACTED', rationale: 'why' });
    expect(verdictOf({ model_verdict: 'LOL' }).verdict).toBeNull();
    expect(verdictOf(undefined).verdict).toBeNull();
  });
});

describe('episodes', () => {
  it('splits active minutes across hour boundaries and ignores AFK', () => {
    const segs = toSegs([
      seg({ timestamp: '2026-11-11T10:45:00', end: '2026-11-11T11:15:00', seconds: 1800 }),
      seg({ timestamp: '2026-11-11T12:00:00', end: '2026-11-11T12:30:00', seconds: 1800, productivity: 'afk' }),
    ]);
    const h = hourly(segs);
    const ten = h.find((b) => b.hour === 10)!, eleven = h.find((b) => b.hour === 11)!;
    expect(Math.round(ten.aligned)).toBe(15);
    expect(Math.round(eleven.aligned)).toBe(15);
    expect(h.find((b) => b.hour === 12)?.aligned ?? 0).toBe(0);
  });
  it('tallies minutes by key with the dominant alignment', () => {
    const segs = toSegs([
      seg({ application: 'Chrome', productivity: 'distractive', seconds: 1200 }),
      seg({ application: 'Chrome', productivity: 'productive', seconds: 300 }),
      seg({ application: 'VS Code', seconds: 600 }),
    ]);
    const t = tally(segs, (s) => s.app);
    expect(t[0]).toMatchObject({ name: 'Chrome', kind: 'drifting' });
    expect(Math.round(t[0].minutes)).toBe(25);
  });
  it('formats minutes', () => { expect(fmtMin(125)).toBe('2h 05m'); });
});

describe('week', () => {
  const today = new Date(2026, 10, 11, 12, 0, 0);
  const daily = { timeline: [
    seg({ timestamp: '2026-11-11T14:00:00', end: '2026-11-11T14:12:00', seconds: 720, productivity: 'distractive', application: 'YouTube' }),
    seg({ timestamp: '2026-11-11T15:00:00', end: '2026-11-11T15:30:00', seconds: 1800 }),
  ] } as never;
  it('builds seven day rows ending today', () => {
    const rows = weekRows(daily, today);
    expect(rows).toHaveLength(7);
    expect(rows[6].activeH).toBeGreaterThan(0);
  });
  it('finds drift hotspots and never invents them', () => {
    expect(hotspots(undefined)).toEqual([]);
    const spots = hotspots(daily);
    expect(spots.map((s) => s.k)).toEqual(['TIME OF DAY', 'APP', 'LONGEST STRETCH']);
    expect(spots[1].t).toContain('YouTube');
  });
});

describe('nudges and outcomes', () => {
  const now = Date.parse('2026-11-11T15:00:00Z');
  const row = (over: Partial<InterventionFeedRow>): InterventionFeedRow => ({ id: 'a', created_at: '2026-11-11T14:55:00Z', delivered: true, interacted: false, ...over });
  it('opens only a delivered, unanswered, recent nudge', () => {
    expect(openNudge([row({})], now)?.id).toBe('a');
    expect(openNudge([row({ interacted: true })], now)).toBeNull();
    expect(openNudge([row({ delivered: false })], now)).toBeNull();
    expect(openNudge([row({ created_at: '2026-11-11T10:00:00Z' })], now)).toBeNull();
    expect(openNudge(undefined, now)).toBeNull();
  });
  it('normalises attribution', () => {
    expect(attrOf({ attribution: 'direct' } as OutcomeRow)).toBe('DIRECT');
    expect(attrOf({ attribution: 'weird' } as OutcomeRow)).toBeNull();
  });
  it('computes recovery rate per day, skipping pending outcomes', () => {
    const today = new Date(2026, 10, 11, 12);
    const o = (status: string, iso: string) => ({ recovery_status: status, intervention_time: iso }) as OutcomeRow;
    const outs = [o('RECOVERED', new Date(2026, 10, 11, 9).toISOString()), o('NOT_RECOVERED', new Date(2026, 10, 11, 10).toISOString()), o('PENDING', new Date(2026, 10, 11, 11).toISOString())];
    const days = recoveryByDay(outs, 14, today);
    expect(days[13]).toBe(50);
    expect(days[0]).toBeNull();
    expect(overallRecovery(outs)).toBe(50);
    expect(overallRecovery([])).toBeNull();
  });
});

describe('memes', () => {
  it('scores an asset from its linked responses only', () => {
    const asset = { id: 'm', responses: [{ id: 'r1', kind: 'MEME', tone: 'x', enabled: true }] } as MemeAsset;
    const eff = [
      { id: 'r1', kind: 'MEME', tone: 'x', times_shown: 10, times_clicked: 3, recovery_count: 7 },
      { id: 'other', kind: 'MEME', tone: 'x', times_shown: 99, times_clicked: 0, recovery_count: 0 },
    ];
    expect(memeStats(asset, eff)).toEqual({ shown: 10, recovered: 7, outOf10: 7 });
    expect(memeStats({ id: 'z' } as MemeAsset, eff).outOf10).toBeNull();
  });
  it('splits a caption into top and bottom halves', () => {
    expect(splitCaption('ME OPENING YOUTUBE FOR ONE VIDEO')).toEqual(['ME OPENING YOUTUBE', 'FOR ONE VIDEO']);
    expect(splitCaption(null)).toEqual(['', '']);
  });
});

describe('routing and zoom', () => {
  it('parses hash routes with a safe default', () => {
    expect(parseRoute('#/week')).toBe('week');
    expect(parseRoute('#/demo')).toBe('demo');
    expect(parseRoute('#/nope')).toBe('command');
    expect(parseRoute('')).toBe('command');
  });
  it('halves every px at 1920 and clamps', () => {
    expect(zoomFor(1920)).toBe(0.5);
    expect(zoomFor(3840)).toBe(1);
    expect(zoomFor(100)).toBe(0.25);
  });
});

describe('watcher data cleanup', () => {
  it('turns exe names into readable apps', () => {
    expect(friendlyApp('WindowsTerminal')).toBe('Terminal');
    expect(friendlyApp('brave.exe')).toBe('Brave');
    expect(friendlyApp('SomethingNew.exe')).toBe('SomethingNew');
    expect(friendlyApp('')).toBe('—');
  });
  it('strips spinner glyphs and the trailing browser name from titles', () => {
    expect(cleanTitle('✳ Whether to keep going')).toBe('Whether to keep going');
    expect(cleanTitle('◐ Whether to keep')).toBe('Whether to keep');
    expect(cleanTitle('Noema — Personal Behavioral Intelligence - Brave', 'Brave')).toBe('Noema — Personal Behavioral Intelligence');
    expect(cleanTitle('report.tex - Overleaf', 'Chrome')).toBe('report.tex - Overleaf');
  });
});
