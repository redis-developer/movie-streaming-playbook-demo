import { useEffect, useState } from 'react';
import { HelpCenter } from './pages/HelpCenter';
import { PlaybookStudio } from './pages/PlaybookStudio';
import { helpStats, type HelpStats } from './api/playbookApi';
import './App.css';

type AppPage = 'help' | 'studio';

function App() {
  const [page, setPage] = useState<AppPage>('help');
  const [stats, setStats] = useState<HelpStats | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);

  const refresh = () => setRefreshKey((k) => k + 1);

  useEffect(() => {
    helpStats().then(setStats).catch(() => setStats(null));
  }, [refreshKey, page]);

  return (
    <div className="app">
      <nav className="app-nav">
        <div className="nav-brand">
          <span className="nav-logo">SF</span>
          <span className="nav-title">StreamFlix Support</span>
          <span className="nav-powered">on Redis Playbook</span>
        </div>
        <div className="nav-links">
          <button className={`nav-btn ${page === 'help' ? 'active' : ''}`} onClick={() => setPage('help')}>
            Help Center
          </button>
          <button className={`nav-btn ${page === 'studio' ? 'active' : ''}`} onClick={() => setPage('studio')}>
            Playbook Studio
            {stats && stats.playbook.proposals.open > 0 && (
              <span className="nav-badge" title="Open proposals waiting for review">{stats.playbook.proposals.open}</span>
            )}
          </button>
        </div>
        <div className="nav-status">
          {stats ? (
            <>
              <span className={`mode-pill ${stats.playbook_mode}`} title="Where the Playbook lives">
                Playbook: {stats.playbook_mode === 'remote' ? 'Redis Playbook service' : 'embedded on Redis'}
              </span>
              <span className={`mode-pill ${stats.llm_mode}`} title="Set OPENAI_API_KEY for the tool-calling agent">
                Agent: {stats.llm_mode === 'llm' ? 'LLM tool calling' : 'offline runner'}
              </span>
            </>
          ) : (
            <span className="mode-pill offline">Backend unreachable</span>
          )}
        </div>
      </nav>

      {page === 'help' ? <HelpCenter stats={stats} onChanged={refresh} /> : <PlaybookStudio onChanged={refresh} />}
    </div>
  );
}

export default App;
