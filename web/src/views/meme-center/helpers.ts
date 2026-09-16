import { useEffect, useState } from 'react';

/** Truncate display text without breaking words harshly. */
export function truncate(value: string | null | undefined, max = 90): string {
  const text = (value ?? '').trim();
  if (text.length <= max) return text;
  return `${text.slice(0, max - 1).trimEnd()}…`;
}

/** Human byte size for the detail panel. */
export function formatBytes(value?: number | null): string {
  const bytes = Number(value || 0);
  if (!Number.isFinite(bytes) || bytes <= 0) return '—';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${Math.round((bytes / (1024 * 1024)) * 10) / 10} MB`;
}

/** Sentiment pill label. Dataset labels pass through verbatim. */
export function sentimentLabel(value?: string | null): string {
  if (!value) return 'UNKNOWN';
  return value.replace(/_/g, ' ').toUpperCase();
}

/** Debounce a fast-changing input (search box). */
export function useDebouncedValue<T>(value: T, delayMs = 350): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const timer = window.setTimeout(() => setDebounced(value), delayMs);
    return () => window.clearTimeout(timer);
  }, [value, delayMs]);
  return debounced;
}

export const RESPONSE_KINDS = ['MEME', 'CHARACTER', 'STICKER', 'TEXT', 'NUDGE', 'CHALLENGE', 'ENCOURAGEMENT'] as const;

export const RESPONSE_TONES = ['gentle', 'sarcastic', 'supportive', 'playful', 'firm'] as const;

export const SENTIMENT_FILTERS = ['', 'very_positive', 'positive', 'neutral', 'negative', 'very_negative'] as const;

export const STATUS_FILTERS = ['all', 'favorite', 'curated', 'not_curated', 'disabled'] as const;

/** Minimum shows before a recovery rate is displayable as data. */
export const MIN_SHOWS_FOR_RATE = 5;

/** Compact cooldown label from a backend cooldown_seconds value. */
export function formatCooldown(value?: number | null): string {
  if (value == null) return '—';
  const seconds = Number(value);
  if (!Number.isFinite(seconds) || seconds < 0) return '—';
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.round((seconds / 3600) * 10) / 10}h`.replace(/\.0h$/, 'h');
  return `${Math.round((seconds / 86400) * 10) / 10}d`.replace(/\.0d$/, 'd');
}

/** Honest outcome label from a real intervention outcome row. Never invents. */
export function describeOutcome(outcome?: {
  recovery_status?: string | null;
  attribution?: string | null;
} | null): string {
  if (!outcome) return 'No outcome recorded';
  const status = String(outcome.recovery_status || 'unknown').replace(/_/g, ' ');
  const attribution = outcome.attribution ? String(outcome.attribution) : '';
  return attribution ? `${status} · ${attribution}` : status;
}
