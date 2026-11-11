import { PAGES, type PageKey } from '../lib/pages';

export default function Nav({ active, offline }: { active: PageKey; offline: boolean }) {
  return (
    <header style={{
      height: 150, display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '0 120px',
      borderBottom: '5px solid var(--ink)', background: 'var(--paper)',
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 28 }}>
        <div className="display" style={{ fontSize: 84, textShadow: '5px 4px 0 rgba(255,91,31,.9)' }}>noema</div>
        <div style={{ fontSize: 24, border: '3px solid var(--ink)', padding: '6px 14px', letterSpacing: '.12em' }}>LOCAL-FIRST</div>
      </div>
      <nav aria-label="Pages" style={{ display: 'flex', gap: 10 }}>
        {PAGES.map(([k, label]) => (
          <a key={k} href={`#/${k}`} className={`pill${k === active ? ' on' : ''}`} aria-current={k === active ? 'page' : undefined}>{label}</a>
        ))}
      </nav>
      <div role="status" style={{ display: 'flex', alignItems: 'center', gap: 16, fontSize: 26, border: '3px solid var(--ink)', padding: '14px 24px', background: 'var(--paper-hi)' }}>
        <div style={{ width: 20, height: 20, borderRadius: '50%', border: '3px solid var(--ink)', background: offline ? 'var(--drift)' : 'var(--recovered)' }} />
        <div>{offline ? 'daemon · offline' : 'daemon · local · online'}</div>
      </div>
    </header>
  );
}
