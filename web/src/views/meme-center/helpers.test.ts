import { describe, expect, it } from 'vitest';
import { describeOutcome, formatBytes, formatCooldown, sentimentLabel, truncate } from './helpers';

describe('truncate', () => {
  it('returns short text unchanged', () => {
    expect(truncate('hello', 90)).toBe('hello');
  });

  it('truncates long OCR with an ellipsis', () => {
    expect(truncate('a'.repeat(100), 90)).toBe(`${'a'.repeat(89)}…`);
  });

  it('handles nullish input', () => {
    expect(truncate(null)).toBe('');
    expect(truncate(undefined)).toBe('');
  });
});

describe('formatBytes', () => {
  it('formats sizes', () => {
    expect(formatBytes(0)).toBe('—');
    expect(formatBytes(512)).toBe('512 B');
    expect(formatBytes(2048)).toBe('2 KB');
    expect(formatBytes(2.5 * 1024 * 1024)).toBe('2.5 MB');
  });
});

describe('sentimentLabel', () => {
  it('passes dataset labels through verbatim in upper case', () => {
    expect(sentimentLabel('very_positive')).toBe('VERY POSITIVE');
    expect(sentimentLabel('neutral')).toBe('NEUTRAL');
    expect(sentimentLabel(null)).toBe('UNKNOWN');
  });
});

describe('formatCooldown', () => {
  it('formats backend cooldown seconds compactly', () => {
    expect(formatCooldown(60)).toBe('1m');
    expect(formatCooldown(3600)).toBe('1h');
    expect(formatCooldown(0)).toBe('0s');
    expect(formatCooldown(null)).toBe('—');
  });
});

describe('describeOutcome', () => {
  it('labels real outcomes without inventing data', () => {
    expect(describeOutcome(null)).toBe('No outcome recorded');
    expect(describeOutcome({ recovery_status: 'RECOVERED', attribution: 'direct' })).toBe('RECOVERED · direct');
    expect(describeOutcome({ recovery_status: 'PENDING', attribution: null })).toBe('PENDING');
  });
});
