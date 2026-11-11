import { useEffect, useState } from 'react';
import Stage from './components/Stage';
import Nav from './components/Nav';
import { parseRoute, type PageKey, type Route } from './lib/pages';
import { LiveProvider, useLive } from './hooks/useLive';
import { openNudge } from './lib/adapt';
import Command from './screens/Command';
import Analytics from './screens/Analytics';
import Memes from './screens/Memes';
import Engine from './screens/Engine';
import Week from './screens/Week';
import { BreakScreen, NudgeOverlay, RecoveryScreen } from './screens/Nudge';

function Shell() {
  const live = useLive();
  const [route, setRoute] = useState<Route>(() => parseRoute(window.location.hash));
  const [answered, setAnswered] = useState<Set<string>>(new Set());

  useEffect(() => {
    const on = () => setRoute(parseRoute(window.location.hash));
    window.addEventListener('hashchange', on);
    return () => window.removeEventListener('hashchange', on);
  }, []);

  const nudge = openNudge(live.feed);
  const showNudge = nudge && !answered.has(nudge.id) && route !== 'demo' ? nudge : null;
  const navKey: PageKey = route === 'recovery' || route === 'break' || route === 'demo' ? 'command' : route;

  return (
    <Stage>
      <div className="page">
        <Nav active={navKey} offline={live.offline} />
        {route === 'command' && <Command />}
        {route === 'demo' && <Command demo />}
        {route === 'analytics' && <Analytics />}
        {route === 'memes' && <Memes />}
        {route === 'engine' && <Engine />}
        {route === 'week' && <Week />}
        {route === 'recovery' && <RecoveryScreen />}
        {route === 'break' && <BreakScreen />}
      </div>
      {live.breakStatus?.active && route !== 'break' && (
        <a href="#/break" className="stamp" style={{ position: 'fixed', right: 60, bottom: 60, zIndex: 40, background: 'var(--intent)', color: 'var(--paper)', fontSize: 34, padding: '14px 28px', textDecoration: 'none' }}>
          BREAK · nudges paused
        </a>
      )}
      {showNudge && (
        <NudgeOverlay
          nudge={showNudge}
          onHide={() => setAnswered((s) => new Set(s).add(showNudge.id))}
          onAnswered={(a) => {
            setAnswered((s) => new Set(s).add(showNudge.id));
            if (a === 'GIVE_ME_5') window.location.hash = '#/break';
            else if (a === 'LOCKING_IN') window.location.hash = '#/recovery';
          }}
        />
      )}
    </Stage>
  );
}

export default function App() {
  return <LiveProvider><Shell /></LiveProvider>;
}
