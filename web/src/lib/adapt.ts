// Pure adapters: Noema backend payloads -> the shapes the riso screens draw.
// No React, no fetching. Everything here is unit-tested (adapt.test.ts).
import type {
  Detection, DailySegment, DailySummary, InterventionFeedRow, MemeAsset,
  OutcomeRow, RealtimeStatus, ResponseEffectivenessRow,
} from '../api/noema';
import type { BehaviorState, Verdict } from './contracts';

export type Kind = 'aligned' | 'drifting' | 'unknown' | 'afk';

export const STAMP: Record<Kind, string> = {
  aligned: '✓ ALIGNED', drifting: '! DRIFTING', unknown: '? UNKNOWN', afk: 'AFK',
};

/** Backend productivity -> design alignment. Unknown never counts as drift. */
export function kindOf(s: Pick<DailySegment, 'productivity'>): Kind {
  switch (s.productivity) {
    case 'productive': return 'aligned';
    case 'distractive': return 'drifting';
    case 'afk': return 'afk';
    default: return 'unknown';
  }
}

export const clamp = (x: number, lo = 0, hi = 1) => Math.max(lo, Math.min(hi, x));

export function fmtMin(totalMinutes: number): string {
  const m = Math.max(0, Math.round(totalMinutes));
  return `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, '0')}m`;
}

export function fmtClock(iso: string | Date | null | undefined): string {
  if (!iso) return '--:--';
  const d = typeof iso === 'string' ? new Date(iso) : iso;
  if (Number.isNaN(d.getTime())) return '--:--';
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}

/** Minutes since local midnight. */
export const localMinutes = (d: Date) => d.getHours() * 60 + d.getMinutes() + d.getSeconds() / 60;

// ---- drift / behavior --------------------------------------------------

export type DriftView = {
  score: number | null;      // null = no realtime reading yet
  enter: number;
  exit: number;
  state: BehaviorState;
};

export function driftView(rt: RealtimeStatus | null | undefined): DriftView {
  const enter = rt?.tracker?.enter_threshold ?? rt?.detector?.enter_threshold ?? 0.6;
  const exit = rt?.tracker?.exit_threshold ?? rt?.detector?.exit_threshold ?? 0.45;
  const raw = rt?.tracker?.last_score;
  const score = typeof raw === 'number' && Number.isFinite(raw) ? clamp(raw) : null;
  return { score, enter, exit, state: stateFromScore(score, enter, exit) };
}

export function stateFromScore(score: number | null, enter = 0.6, exit = 0.45): BehaviorState {
  if (score === null || score < exit) return 'LOCKED_IN';
  if (score < enter) return 'WOBBLING';
  return 'DRIFTING';
}

const VERDICTS: Verdict[] = ['NOT_DISTRACTED', 'UNCERTAIN', 'DISTRACTED', 'INTENTIONAL_BREAK'];

export function verdictOf(d: Detection | null | undefined): { verdict: Verdict | null; rationale: string } {
  const raw = String(d?.model_verdict ?? '').trim().toUpperCase();
  const verdict = (VERDICTS as string[]).includes(raw) ? (raw as Verdict) : null;
  return { verdict, rationale: String(d?.reason ?? '').trim() };
}

// ---- episodes / timeline ----------------------------------------------

export type Seg = {
  start: Date; end: Date; minutes: number; kind: Kind;
  app: string; title: string; domain: string; activity: string;
};

const FRIENDLY: Record<string, string> = {
  windowsterminal: 'Terminal', wt: 'Terminal', brave: 'Brave', chrome: 'Chrome', msedge: 'Edge', firefox: 'Firefox',
  code: 'VS Code', explorer: 'File Explorer', 'whatsapp.root': 'WhatsApp', whatsapp: 'WhatsApp', snippingtool: 'Snipping Tool',
  shellexperiencehost: 'Windows Shell', copilotapp: 'Copilot', opencode: 'OpenCode', notepad: 'Notepad', slack: 'Slack',
};
const BROWSERS = new Set(['Brave', 'Chrome', 'Edge', 'Firefox', 'Google Chrome']);

/** "WindowsTerminal.exe" -> "Terminal". Unknown apps just lose `.exe`. */
export function friendlyApp(raw: string | null | undefined): string {
  const name = String(raw ?? '').trim().replace(/\.exe$/i, '');
  if (!name) return '—';
  return FRIENDLY[name.toLowerCase()] ?? name;
}

/** Drop spinner/status glyphs (◐ ✳ ⠋ …) and the trailing browser name from a window title. */
export function cleanTitle(raw: string | null | undefined, app = ''): string {
  let t = String(raw ?? '').replace(/\s+/g, ' ').trim();
  t = t.replace(/^[^\p{L}\p{N}"'([]+/u, '');
  if (BROWSERS.has(app)) t = t.replace(/\s[-–—]\s(Brave|Google Chrome|Microsoft Edge|Mozilla Firefox|Chrome|Firefox)$/i, '');
  return t;
}

export function toSegs(timeline: DailySegment[] | undefined): Seg[] {
  return (timeline ?? [])
    .map((s) => {
      const start = new Date(s.timestamp);
      const end = new Date(s.end || s.timestamp);
      const minutes = Math.max(0, (s.seconds ?? (end.getTime() - start.getTime()) / 1000) / 60);
      return {
        start, end, minutes, kind: kindOf(s),
        app: friendlyApp(s.application), title: cleanTitle(s.title, friendlyApp(s.application)), domain: s.domain || '',
        activity: s.activity || 'Other',
      };
    })
    .filter((s) => !Number.isNaN(s.start.getTime()) && s.minutes > 0)
    .sort((a, b) => a.start.getTime() - b.start.getTime());
}

export type Placed = Seg & { left: number; width: number };

/** Axis window in local minutes: at least 6h, snapped to whole hours. */
export function axisFor(segs: Seg[], now = new Date()): { start: number; end: number } {
  if (!segs.length) return { start: 8 * 60, end: 18 * 60 };
  let start = Math.min(...segs.map((s) => localMinutes(s.start)));
  let end = Math.max(...segs.map((s) => localMinutes(s.end)), isSameDay(segs[0].start, now) ? localMinutes(now) : 0);
  start = Math.floor(start / 60) * 60;
  end = Math.ceil(end / 60) * 60;
  if (end - start < 360) end = Math.min(1440, start + 360);
  if (end - start < 360) start = Math.max(0, end - 360);
  return { start, end };
}

export function place(segs: Seg[], axis: { start: number; end: number }): Placed[] {
  const span = axis.end - axis.start;
  return segs
    .map((s) => {
      const a = clamp(localMinutes(s.start), axis.start, axis.end);
      const b = clamp(localMinutes(s.end) || a, axis.start, axis.end);
      return { ...s, left: ((a - axis.start) / span) * 100, width: (Math.max(b - a, 0.5) / span) * 100 };
    });
}

export function isSameDay(a: Date, b: Date) {
  return a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
}

export type HourBucket = { hour: number; aligned: number; unknown: number; drift: number };

/** Active minutes per hour, split across hour boundaries; AFK excluded. */
export function hourly(segs: Seg[]): HourBucket[] {
  const buckets = new Map<number, HourBucket>();
  for (const s of segs) {
    if (s.kind === 'afk') continue;
    let t = s.start.getTime();
    const end = s.start.getTime() + s.minutes * 60000;
    while (t < end) {
      const d = new Date(t);
      const next = new Date(d); next.setHours(d.getHours() + 1, 0, 0, 0);
      const chunk = (Math.min(end, next.getTime()) - t) / 60000;
      const h = d.getHours();
      const b = buckets.get(h) ?? { hour: h, aligned: 0, unknown: 0, drift: 0 };
      if (s.kind === 'aligned') b.aligned += chunk; else if (s.kind === 'drifting') b.drift += chunk; else b.unknown += chunk;
      buckets.set(h, b);
      t = next.getTime();
    }
  }
  if (!buckets.size) return [];
  const hours = [...buckets.keys()];
  const lo = Math.max(0, Math.min(...hours) - 1);
  const hi = Math.min(23, Math.max(...hours) + 1);
  const out: HourBucket[] = [];
  for (let h = lo; h <= Math.max(hi, lo + 7); h++) out.push(buckets.get(h) ?? { hour: h, aligned: 0, unknown: 0, drift: 0 });
  return out.filter((b) => b.hour <= 23);
}

export type Tally = { name: string; minutes: number; kind: Kind };

/** Group minutes by a key; kind = whichever alignment holds most minutes. */
export function tally(segs: Seg[], key: (s: Seg) => string, limit = 6): Tally[] {
  const m = new Map<string, { minutes: number; by: Record<Kind, number> }>();
  for (const s of segs) {
    if (s.kind === 'afk') continue;
    const k = key(s).trim();
    if (!k) continue;
    const e = m.get(k) ?? { minutes: 0, by: { aligned: 0, drifting: 0, unknown: 0, afk: 0 } };
    e.minutes += s.minutes; e.by[s.kind] += s.minutes;
    m.set(k, e);
  }
  return [...m.entries()]
    .map(([name, e]) => ({
      name, minutes: e.minutes,
      kind: (Object.entries(e.by).sort((a, b) => b[1] - a[1])[0][0]) as Kind,
    }))
    .sort((a, b) => b.minutes - a.minutes)
    .slice(0, limit);
}

export function totals(segs: Seg[]) {
  let active = 0, afk = 0;
  for (const s of segs) { if (s.kind === 'afk') afk += s.minutes; else active += s.minutes; }
  return { active, afk };
}

// ---- week --------------------------------------------------------------

export type DayRow = { key: string; label: string; activeH: number; alignedH: number; driftH: number };

const dayKey = (d: Date) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;

export function weekRows(daily: DailySummary | undefined, today = new Date()): DayRow[] {
  const segs = toSegs(daily?.timeline);
  const rows: DayRow[] = [];
  for (let i = 6; i >= 0; i--) {
    const d = new Date(today); d.setHours(0, 0, 0, 0); d.setDate(d.getDate() - i);
    rows.push({ key: dayKey(d), label: d.toLocaleDateString('en-US', { weekday: 'short' }), activeH: 0, alignedH: 0, driftH: 0 });
  }
  const idx = new Map(rows.map((r) => [r.key, r]));
  for (const s of segs) {
    const r = idx.get(dayKey(s.start));
    if (!r || s.kind === 'afk') continue;
    r.activeH += s.minutes / 60;
    if (s.kind === 'aligned') r.alignedH += s.minutes / 60;
    if (s.kind === 'drifting') r.driftH += s.minutes / 60;
  }
  return rows;
}

export type Hotspot = { n: string; k: string; t: string };

export function hotspots(daily: DailySummary | undefined): Hotspot[] {
  const drift = toSegs(daily?.timeline).filter((s) => s.kind === 'drifting');
  if (!drift.length) return [];
  const out: Hotspot[] = [];
  const byHour = new Array<number>(24).fill(0);
  let total = 0;
  for (const s of drift) { byHour[s.start.getHours()] += s.minutes; total += s.minutes; }
  // busiest consecutive 2-hour window
  let best = 0, bestH = 0;
  for (let h = 0; h < 23; h++) { const v = byHour[h] + byHour[h + 1]; if (v > best) { best = v; bestH = h; } }
  out.push({
    n: `${String(bestH).padStart(2, '0')}–${String(bestH + 2).padStart(2, '0')}h`, k: 'TIME OF DAY',
    t: `${Math.round((best / total) * 100)}% of all drift starts in this window.`,
  });
  const apps = tally(drift, (s) => s.app, 1);
  if (apps[0]) out.push({ n: fmtMin(apps[0].minutes), k: 'APP', t: `${apps[0].name} was the biggest pull this week.` });
  const longest = [...drift].sort((a, b) => b.minutes - a.minutes)[0];
  out.push({ n: `${Math.round(longest.minutes)} min`, k: 'LONGEST STRETCH', t: `${longest.app}${longest.title ? ` — ${longest.title}` : ''}.` });
  return out;
}

// ---- nudge / outcomes --------------------------------------------------

/** The nudge the user still has to answer: delivered, not yet interacted, recent. */
export function openNudge(feed: InterventionFeedRow[] | undefined, now = Date.now(), maxAgeMs = 15 * 60_000): InterventionFeedRow | null {
  for (const row of feed ?? []) {
    if (!row.delivered || row.interacted) continue;
    const at = row.created_at ? new Date(row.created_at).getTime() : 0;
    if (now - at <= maxAgeMs) return row;
  }
  return null;
}

export type Attr = 'DIRECT' | 'AMBIENT' | 'NONE';
export function attrOf(o: OutcomeRow | null | undefined): Attr | null {
  const a = String(o?.attribution ?? '').toUpperCase();
  return a === 'DIRECT' || a === 'AMBIENT' || a === 'NONE' ? a : null;
}

/** Recovery rate per local day over the last n days (percent, null = no outcomes). */
export function recoveryByDay(outcomes: OutcomeRow[] | undefined, days = 14, today = new Date()): (number | null)[] {
  const out: { tot: number; rec: number }[] = Array.from({ length: days }, () => ({ tot: 0, rec: 0 }));
  const start = new Date(today); start.setHours(0, 0, 0, 0); start.setDate(start.getDate() - (days - 1));
  for (const o of outcomes ?? []) {
    if (o.recovery_status === 'PENDING') continue;
    const d = new Date(o.intervention_time); d.setHours(0, 0, 0, 0);
    const i = Math.round((d.getTime() - start.getTime()) / 86_400_000);
    if (i < 0 || i >= days) continue;
    out[i].tot += 1; if (o.recovery_status === 'RECOVERED') out[i].rec += 1;
  }
  return out.map((x) => (x.tot ? Math.round((x.rec / x.tot) * 100) : null));
}

export function overallRecovery(outcomes: OutcomeRow[] | undefined): number | null {
  const done = (outcomes ?? []).filter((o) => o.recovery_status !== 'PENDING');
  if (!done.length) return null;
  return Math.round((done.filter((o) => o.recovery_status === 'RECOVERED').length / done.length) * 100);
}

// ---- memes -------------------------------------------------------------

export type MemeStat = { shown: number; recovered: number; outOf10: number | null };

/** Per-asset effectiveness = sum of its linked responses' recoveries / shown. */
export function memeStats(asset: MemeAsset, eff: ResponseEffectivenessRow[]): MemeStat {
  const ids = new Set((asset.responses ?? []).map((r) => r.id));
  let shown = 0, recovered = 0;
  for (const r of eff) if (ids.has(r.id)) { shown += r.times_shown; recovered += r.recovery_count; }
  return { shown, recovered, outOf10: shown ? Math.round((recovered / shown) * 10) : null };
}

export function assetStatus(a: MemeAsset, stat: MemeStat): 'active' | 'benched' | 'untested' {
  if (a.enabled === false) return 'benched';
  return stat.shown ? 'active' : 'untested';
}

export function splitCaption(text: string | null | undefined): [string, string] {
  const t = (text ?? '').replace(/\s+/g, ' ').trim();
  if (!t) return ['', ''];
  const words = t.split(' ');
  if (words.length < 4) return [t, ''];
  const mid = Math.ceil(words.length / 2);
  return [words.slice(0, mid).join(' '), words.slice(mid).join(' ')];
}
