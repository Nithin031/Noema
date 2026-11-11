import { fmtClock } from '../lib/adapt';

export function DaemonOffline({ since, onRetry }: { since: Date | null; onRetry: () => void }) {
  return (
    <div className="page-body">
      <div className="card" style={{ padding: 60, display: 'flex', flexDirection: 'column', gap: 34 }}>
        <div className="stamp drifting tilt" style={{ alignSelf: 'flex-start', fontSize: 40, padding: '10px 26px' }}>! DAEMON OFFLINE</div>
        <div className="aside" style={{ fontSize: 110, lineHeight: 1 }}>Noema can't see anything.</div>
        <div style={{ fontSize: 34, lineHeight: 1.6 }}>
          {since ? `The local daemon stopped answering at ${fmtClock(since)}. ` : 'The local daemon is not answering. '}
          Nothing is being recorded until it's back.
        </div>
        <div style={{ display: 'flex', gap: 30, alignItems: 'center' }}>
          <div style={{ fontSize: 34, background: 'var(--ink)', color: 'var(--paper)', padding: '20px 30px' }}>$ python -m noema</div>
          <button className="btn chunky" onClick={onRetry}>RETRY</button>
        </div>
      </div>
    </div>
  );
}
