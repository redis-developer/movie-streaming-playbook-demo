import { useEffect, useState } from 'react';
import { HelpChat } from '../components/HelpChat';
import {
  extractProposals,
  helpSuggestions,
  listAccounts,
  patchPlaybookConfig,
  type Account,
  type ChatResponse,
  type HelpStats,
  type Scenario,
} from '../api/playbookApi';
import '../styles/HelpCenter.css';

interface HelpCenterProps {
  stats: HelpStats | null;
  onChanged: () => void;
}

export function HelpCenter({ stats, onChanged }: HelpCenterProps) {
  const [suggestions, setSuggestions] = useState<string[]>([]);
  const [scenarios, setScenarios] = useState<Scenario[]>([]);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [showStats, setShowStats] = useState(false);
  const [discoveryLimit, setDiscoveryLimit] = useState<number | null>(null);
  const [budget, setBudget] = useState<number | null>(null);
  const [pins, setPins] = useState<string>('');
  const [notice, setNotice] = useState<string | null>(null);
  const [lastResponse, setLastResponse] = useState<ChatResponse | null>(null);
  const [openScenario, setOpenScenario] = useState<number | null>(0);

  useEffect(() => {
    helpSuggestions()
      .then((data) => { setSuggestions(data.suggestions); setScenarios(data.scenarios); })
      .catch(() => setSuggestions(['What is your refund policy?', "What's the status of my refund? My account is SF-48213"]));
    listAccounts().then((data) => setAccounts(data.accounts)).catch(() => setAccounts([]));
  }, []);

  useEffect(() => {
    if (stats?.playbook?.discovery?.maxPreviewTokens !== undefined) {
      setBudget(stats.playbook.discovery.maxPreviewTokens ?? null);
    }
  }, [stats]);

  const applyBudget = async (value: number | null) => {
    setBudget(value);
    try {
      await patchPlaybookConfig({ discovery: { maxPreviewTokens: value } });
      setNotice(value ? `Discovery preview budget set to ${value} tokens` : 'Discovery preview budget disabled');
      onChanged();
    } catch (error) {
      setNotice(`Could not change the budget: ${error instanceof Error ? error.message : 'error'}`);
    }
  };

  const runExtraction = async () => {
    try {
      const result = await extractProposals();
      setNotice(result.proposals.length
        ? `Proposed ${result.proposals.length} FAQ(s) from ${result.pendingSessions} pending sessions. Review them in Playbook Studio.`
        : `No cluster of ${result.minSessions}+ similar sessions yet (${result.pendingSessions} pending).`);
      onChanged();
    } catch (error) {
      setNotice(`Extraction failed: ${error instanceof Error ? error.message : 'error'}`);
    }
  };

  const handleResponse = (response: ChatResponse) => {
    setLastResponse(response);
    onChanged();
  };

  const pinList = pins.split(',').map((p) => p.trim()).filter(Boolean);

  return (
    <div className="help-center-page">
      <header className="help-header">
        <div className="help-header-content">
          <div className="help-branding">
            <div className="help-logo">?</div>
            <div>
              <h1>StreamFlix Help Center</h1>
              <p>Answers governed by Redis Playbook: approved FAQs, versioned Skills, live data</p>
            </div>
          </div>
          <div className="help-actions">
            <button className="stats-toggle" onClick={() => setShowStats(!showStats)}>{showStats ? 'Hide Stats' : 'Show Stats'}</button>
          </div>
        </div>

        {showStats && stats && (
          <div className="stats-panel">
            <div className="stats-grid">
              <div className="stat-item">
                <span className="stat-label">Published FAQs</span>
                <span className="stat-value">{stats.playbook.faqs.published}</span>
                <span className="stat-status active">{stats.playbook.faqs.drafts} draft version(s)</span>
              </div>
              <div className="stat-item">
                <span className="stat-label">Published Skills</span>
                <span className="stat-value">{stats.playbook.skills.published}</span>
                <span className="stat-status active">{stats.playbook.mode}</span>
              </div>
              <div className="stat-item">
                <span className="stat-label">Open proposals</span>
                <span className="stat-value">{stats.playbook.proposals.open}</span>
                <span className={`stat-status ${stats.playbook.proposals.open ? 'pending' : 'inactive'}`}>
                  {stats.sessions.pending_rag} session(s) pending
                </span>
              </div>
              <div className="stat-item">
                <span className="stat-label">Support sessions</span>
                <span className="stat-value">{stats.sessions.total}</span>
                <span className="stat-status active">{stats.sessions.min_for_proposal} similar needed per proposal</span>
              </div>
            </div>
            <div className="admin-actions">
              <button className="ingest-btn" onClick={runExtraction}>Propose FAQs from sessions</button>
              <span className="ingest-message">{stats.articles.num_articles} help articles indexed · agent mode: {stats.llm_mode}</span>
            </div>
          </div>
        )}
        {notice && <div className="notice-bar" onClick={() => setNotice(null)}>{notice}</div>}
      </header>

      <main className="help-main">
        <div className="help-chat-container">
          <HelpChat suggestions={suggestions} discoveryLimit={discoveryLimit} pins={pinList} onResponse={handleResponse} />

          <div className="chat-controls">
            <label className="control">
              Discovery limit
              <select value={discoveryLimit ?? ''} onChange={(e) => setDiscoveryLimit(e.target.value ? Number(e.target.value) : null)}>
                <option value="">policy default</option>
                {[1, 2, 3, 4, 6, 10].map((n) => <option key={n} value={n}>{n}</option>)}
              </select>
            </label>
            <label className="control">
              Preview token budget
              <select value={budget ?? ''} onChange={(e) => applyBudget(e.target.value ? Number(e.target.value) : null)}>
                <option value="">unlimited</option>
                {[64, 120, 200, 300, 500, 800].map((n) => <option key={n} value={n}>{n} tokens</option>)}
              </select>
            </label>
            <label className="control grow">
              Pin entry keys
              <input type="text" value={pins} onChange={(e) => setPins(e.target.value)} placeholder="e.g. escalate-to-human" />
            </label>
          </div>

          {lastResponse?.discovery && (
            <div className="discovery-summary">
              <strong>Last discovery:</strong> {lastResponse.discovery.faqs.length} FAQ, {lastResponse.discovery.skills.length} Skill preview(s)
              {lastResponse.discovery.estimatedPreviewTokens != null && (
                <> · ~{lastResponse.discovery.estimatedPreviewTokens} preview tokens{lastResponse.discovery.maxPreviewTokens ? ` of ${lastResponse.discovery.maxPreviewTokens}` : ''}</>
              )}
              {lastResponse.discovery.budgetTruncated && <span className="flag warn">budget truncated</span>}
              {lastResponse.discovery.truncated && <span className="flag">more candidates than slots</span>}
              <div className="discovery-entries">
                {lastResponse.discovery.faqs.map((f) => (
                  <span key={f.entryKey || f.question} className="entry-chip" title={f.question}>FAQ {f.entryKey} v{f.version} · {((f.score || 0) * 100).toFixed(0)}%</span>
                ))}
                {lastResponse.discovery.skills.map((s) => (
                  <span key={s.name} className="entry-chip skill">Skill {s.name} v{s.version} · {((s.score || 0) * 100).toFixed(0)}%</span>
                ))}
              </div>
            </div>
          )}
        </div>

        <aside className="help-sidebar">
          <div className="sidebar-section">
            <h3>How a question is answered</h3>
            <div className="how-it-works">
              <Step n={1} title="Guardrail" text="Semantic router keeps the bot on StreamFlix topics." />
              <Step n={2} title="Playbook discovery" text="One vector query over published FAQs and Skill previews, under a token budget." />
              <Step n={3} title="FAQ or Skill" text="FAQ: approved answer verbatim. Skill: load instructions, fetch one resource, read live data." />
              <Step n={4} title="Fallback" text="RAG over help articles, never cached. Each of these sessions is evidence for a FAQ proposal." />
            </div>
          </div>

          <div className="sidebar-section">
            <h3>Demo scenarios</h3>
            <div className="scenario-list">
              {scenarios.map((scenario, index) => (
                <div key={scenario.title} className={`scenario ${openScenario === index ? 'open' : ''}`}>
                  <button className="scenario-title" onClick={() => setOpenScenario(openScenario === index ? null : index)}>
                    <span>{scenario.title}</span><span>{openScenario === index ? '−' : '+'}</span>
                  </button>
                  {openScenario === index && (
                    <ol className="scenario-steps">{scenario.steps.map((step, i) => <li key={i}>{step}</li>)}</ol>
                  )}
                </div>
              ))}
            </div>
          </div>

          <div className="sidebar-section">
            <h3>Live accounts (what Skills read)</h3>
            <div className="account-list">
              {accounts.map((account) => (
                <div key={account.accountId} className="account-row">
                  <code>{account.accountId}</code>
                  <span>{account.plan} · {account.paymentMethod}</span>
                  <span className="account-detail">
                    {account.refund ? `refund ${account.refund.status}` : 'no refund'}
                    {account.stream?.lastErrorCode ? ` · ${account.stream.lastErrorCode}` : ''}
                  </span>
                </div>
              ))}
              {accounts.length === 0 && <p className="muted">Seed the Playbook in Studio to load demo accounts.</p>}
            </div>
          </div>
        </aside>
      </main>
    </div>
  );
}

function Step({ n, title, text }: { n: number; title: string; text: string }) {
  return (
    <div className="step">
      <span className="step-number">{n}</span>
      <div className="step-content"><strong>{title}</strong><p>{text}</p></div>
    </div>
  );
}
