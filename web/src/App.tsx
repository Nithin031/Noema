import { useState } from 'react';
import './css/globals.css';
import './css/ai-activity.css';
import AIDashboard from './views/ai-activity';
import ResultsPage from './views/results';
import MemeCenterPage from './views/meme-center';

function App() {
  const [page, setPage] = useState<'command' | 'results' | 'memes'>('command');
  return (
    <>
      <nav className="top-tabs" aria-label="Workspace pages">
        <button className={page === 'command' ? 'active' : ''} onClick={() => setPage('command')}>Command center</button>
        <button className={page === 'results' ? 'active' : ''} onClick={() => setPage('results')}>Results &amp; models</button>
        <button className={page === 'memes' ? 'active' : ''} onClick={() => setPage('memes')}>Meme center</button>
      </nav>
      {page === 'command' ? <AIDashboard onOpenResults={() => setPage('results')} /> : page === 'results' ? <ResultsPage /> : <MemeCenterPage />}
    </>
  );
}

export default App;
