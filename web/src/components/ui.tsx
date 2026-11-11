import type { CSSProperties, ReactNode } from 'react';
import type { Kind } from '../lib/adapt';
import { STAMP } from '../lib/adapt';

export function Stamp({ kind, children, style }: { kind: Kind | 'recovered'; children?: ReactNode; style?: CSSProperties }) {
  const text = kind === 'recovered' ? 'RECOVERED' : STAMP[kind];
  return <span className={`stamp ${kind}`} style={style}>{children ?? text}</span>;
}

export function Dots({ filled, total = 10, size }: { filled: number; total?: number; size?: number }) {
  return (
    <div className="dots" role="img" aria-label={`${filled} out of ${total}`}>
      {Array.from({ length: total }, (_, i) => (
        <div key={i} className={`dot${i < filled ? ' on' : ''}`} style={size ? { width: size, height: size } : undefined} />
      ))}
    </div>
  );
}

/** Never fabricate: show whether a card is live data or an illustrative example. */
export function Src({ live }: { live: boolean }) {
  return <span className={`src ${live ? 'live' : 'example'}`}>{live ? 'LIVE' : 'EXAMPLE'}</span>;
}

export function Title({ children, aside, orange }: { children: ReactNode; aside?: ReactNode; orange?: boolean }) {
  return (
    <div className="title-row">
      <h1 className={`display h1${orange ? ' orange' : ''}`} style={{ margin: 0 }}>{children}</h1>
      {aside ? <div className="aside" style={{ fontSize: 56 }}>{aside}</div> : null}
    </div>
  );
}

export function Banner({ children }: { children: ReactNode }) {
  return (
    <div role="status" className="stripe-drift" style={{ border: '5px solid var(--ink)', padding: '30px 60px', fontSize: 36, fontWeight: 500 }}>
      {children}
    </div>
  );
}

export function Empty({ title, body, action }: { title: string; body: string; action?: ReactNode }) {
  return (
    <div className="card" style={{ display: 'flex', flexDirection: 'column', gap: 34, padding: 60 }}>
      <div className="aside" style={{ fontSize: 120, lineHeight: 1 }}>{title}</div>
      <div style={{ fontSize: 34, lineHeight: 1.6 }}>{body}</div>
      {action}
    </div>
  );
}

export function Hatch({ w, h }: { w: string | number; h: number }) {
  return <div className="hatch" style={{ width: w, height: h }} />;
}
