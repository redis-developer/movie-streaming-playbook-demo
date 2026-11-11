import { useEffect, useRef, useState } from 'react';
import { HelpCenter } from './pages/HelpCenter';
import { PlaybookStudio } from './pages/PlaybookStudio';
import { helpStats, type HelpStats } from './api/playbookApi';
import './App.css';

type AppPage = 'help' | 'studio';

function App() {
  const [page, setPage] = useState<AppPage>('help');
  const [stats, setStats] = useState<HelpStats | null>(null);
  const [backendState, setBackendState] = useState<'loading' | 'reachable' | 'unreachable'>('loading');
  const [refreshKey, setRefreshKey] = useState(0);
  const consecutiveFailures = useRef(0);

  const refresh = () => setRefreshKey((k) => k + 1);

  useEffect(() => {
    let cancelled = false;
    let retry: ReturnType<typeof setTimeout> | undefined;

    const checkBackend = async () => {
      try {
        const next = await helpStats();
        if (cancelled) return;
        consecutiveFailures.current = 0;
        setStats(next);
        setBackendState('reachable');
      } catch {
        if (cancelled) return;
        consecutiveFailures.current += 1;
        if (consecutiveFailures.current >= 3) setBackendState('unreachable');
      } finally {
        if (!cancelled) retry = setTimeout(checkBackend, 5000);
      }
    };

    checkBackend();
    return () => {
      cancelled = true;
      if (retry) clearTimeout(retry);
    };
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
          {stats && backendState !== 'unreachable' ? (
            <>
              <span className={`mode-pill ${stats.playbook_mode}`} title="Where the Playbook lives">
                Playbook: {stats.playbook_mode === 'remote' ? 'Redis Playbook service' : 'embedded on Redis'}
              </span>
              <span className={`mode-pill ${stats.llm_mode}`} title="Set OPENAI_API_KEY for the tool-calling agent">
                Agent: {stats.llm_mode === 'llm' ? 'LLM tool calling' : 'offline runner'}
              </span>
            </>
          ) : backendState === 'loading' ? (
            <span className="mode-pill offline">Connecting to backend…</span>
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
