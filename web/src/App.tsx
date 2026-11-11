import { useEffect, useState } from 'react';
import Stage from './components/Stage';
import Nav from './components/Nav';
import { parseRoute, type Route } from './lib/pages';
import { LiveProvider, useLive } from './hooks/useLive';
import Analytics from './screens/Analytics';
import Week from './screens/Week';
import Models from './screens/Models';

function Shell() {
  const live = useLive();
  const [route, setRoute] = useState<Route>(() => parseRoute(window.location.hash));

  useEffect(() => {
    const on = () => setRoute(parseRoute(window.location.hash));
    window.addEventListener('hashchange', on);
    return () => window.removeEventListener('hashchange', on);
  }, []);

  return (
    <Stage>
      <div className="page">
        <Nav active={route} offline={live.offline} />
        {live.offline && (
          <div role="alert" className="stripe-drift" style={{ margin: '40px 120px 0', border: '5px solid var(--ink)', padding: '24px 40px', fontSize: 34, fontWeight: 500 }}>
            The Noema daemon is not answering. Start it with: python -m noema
          </div>
        )}
        {route === 'today' && <Analytics />}
        {route === 'week' && <Week />}
        {route === 'models' && <Models />}
      </div>
    </Stage>
  );
}

export default function App() {
  return <LiveProvider><Shell /></LiveProvider>;
}
