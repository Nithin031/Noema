import { describe, expect, it } from 'vitest';
import type { DailySegment } from '../api/noema';
import { cleanTitle, friendlyApp, fmtMin, hourly, kindOf, tally, toSegs, weekRows } from './adapt';
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
});



describe('routing and zoom', () => {
  it('parses hash routes with a safe default', () => {
    expect(parseRoute('#/week')).toBe('week');
    expect(parseRoute('#/models')).toBe('models');
    expect(parseRoute('#/nope')).toBe('today');
    expect(parseRoute('')).toBe('today');
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
