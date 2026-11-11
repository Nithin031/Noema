// Data contracts the UI expects from the Noema backend/daemon.
// Field names are suggestions — map them to your existing models.

export type Alignment = 'aligned' | 'misaligned' | 'unknown';           // unknown never counts as drift
export type Verdict = 'NOT_DISTRACTED' | 'UNCERTAIN' | 'DISTRACTED' | 'INTENTIONAL_BREAK';
export type Attribution = 'DIRECT' | 'AMBIENT' | 'NONE';
export type NudgeAction = 'LOCKING_IN' | 'GIVE_ME_5' | 'INTENTIONAL';
export type BehaviorState = 'LOCKED_IN' | 'WOBBLING' | 'DRIFTING' | 'RECOVERING';
export type Owner = 'LOCAL' | 'GEMINI' | 'LOCAL+GEMINI';

export interface Intent { text: string; setAt: string /* ISO */; }

export interface Episode {
  id: string; start: string; end: string;       // ISO
  app: string; windowTitle: string; domain?: string;
  category: string;                              // Coding, Writing, Research, Comms, Video, Social…
  alignment: Alignment; afk: boolean;
}

export interface DriftSnapshot {                 // every ~60 s
  at: string; score: number;                     // 0..1
  enterThreshold: number;                        // 0.60
  releaseThreshold: number;                      // 0.45 (hysteresis)
  state: BehaviorState;
}

export interface GeminiVerdict {
  at: string; verdict: Verdict; confidence: number;
  rationale: string; shouldIntervene: boolean; model: string;
}

export interface PolicyGate {                    // deterministic, has the last word
  pass: boolean; driftChecksOverThreshold: number;
  cooldownClear: boolean; afkVeto: boolean; breakActive: boolean;
}

export interface Nudge {
  id: string; firedAt: string; memeId: string; copy: string;
  evidence: { app: string; minutes: number; goal: string };
  verdict: GeminiVerdict; gate: PolicyGate;
  response?: { action: NudgeAction; at: string };
  outcome?: { attribution: Attribution; returnedAfterSec?: number; alignedForMin?: number; explanation: string };
}

export interface BreakMode { active: boolean; endsAt?: string; secondsLeft?: number; }

export interface Meme {
  id: string; ocrText: string; sentiment: { label: string; score: number };
  provenance: string; tags: string[]; favorite: boolean;
  status: 'active' | 'benched' | 'untested';
  responses: Record<NudgeAction, number>;
  directRecoveries: number; shown: number;       // effectiveness = directRecoveries/shown
  imageUrl: string;                              // local file
}

export interface PipelineStage {                 // How Noema Thinks
  key: 'observe'|'understand'|'align'|'detect'|'reason'|'intervene'|'measure';
  owner: Owner; receives: string; decides: string;
}

export interface ModelTier { chain: 'classification'|'reasoning'; models: { name: string; status: 'live'|'standby'|'down' }[]; }
export interface Quota { name: string; used: number; limit: number; }
export interface LatencySeries { model: string; p50Ms: number; p95Ms: number; points: number[]; }
export interface ProviderHealth { name: string; status: 'OK'|'SLOW'|'DOWN'; detail: string; }

export interface PrivacyReceipt {
  at: string; model: string; purpose: string; bytes: number;
  fieldsSent: { key: string; value: string }[];
  filtered: { key: string; rule: string }[];
}

export interface DailyAnalytics {                // Analytics tab (day view)
  date: string; activeMin: number; afkMin: number;
  hourly: { hour: number; alignedMin: number; unknownMin: number; driftMin: number }[];
  categories: { name: string; minutes: number; alignment: Alignment }[];
  lanes: { app: Episode[]; browser: Episode[]; afk: { start: string; end: string }[] };
  topApps: { name: string; minutes: number }[];
  topTitles: { name: string; minutes: number; alignment: Alignment }[];
  topDomains: { name: string; minutes: number; alignment: Alignment }[];
}

export interface WeekSummary {
  days: { day: string; intendedH: number; alignedH: number }[];
  driftHotspots: { kind: 'time'|'app'|'trigger'; headline: string; detail: string }[];
  whatHelped: { label: string; directOutOf10: number }[];
  recoveryRate14d: number[];                     // % per day
}

export type SystemStatus =
  | { kind: 'ok' } | { kind: 'empty' } | { kind: 'loading'; detail: string }
  | { kind: 'daemon_offline'; since: string }
  | { kind: 'model_failure' };                   // banner: "Gemini unavailable, showing deterministic results only"
