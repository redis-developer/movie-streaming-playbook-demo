import { useEffect, useRef, useState } from 'react';
import { helpChat, type ChatMessage, type ChatResponse, type HelpArticleSource, type TraceStep } from '../api/playbookApi';
import '../styles/HelpChat.css';

interface HelpChatProps {
  suggestions: string[];
  discoveryLimit: number | null;
  pins: string[];
  onResponse?: (response: ChatResponse) => void;
}

export function HelpChat({ suggestions, discoveryLimit, pins, onResponse }: HelpChatProps) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState('');
  const [isLoading, setIsLoading] = useState(false);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  const sendMessage = async (text: string) => {
    if (!text.trim() || isLoading) return;
    const userMessage: ChatMessage = { id: Date.now().toString(), role: 'user', content: text, timestamp: new Date().toISOString() };
    setMessages((prev) => [...prev, userMessage]);
    setInput('');
    setIsLoading(true);
    try {
      const response = await helpChat(text, { discovery_limit: discoveryLimit, pins });
      setMessages((prev) => [...prev, { id: (Date.now() + 1).toString(), role: 'assistant', content: response.answer, timestamp: new Date().toISOString(), response }]);
      onResponse?.(response);
    } catch (error) {
      setMessages((prev) => [...prev, {
        id: (Date.now() + 1).toString(), role: 'assistant', timestamp: new Date().toISOString(),
        content: `Sorry, something went wrong: ${error instanceof Error ? error.message : 'unknown error'}`,
      }]);
    } finally {
      setIsLoading(false);
    }
  };

  return (
    <div className="help-chat">
      <div className="chat-messages">
        {messages.length === 0 ? (
          <div className="chat-welcome">
            <div className="welcome-icon">?</div>
            <h3>StreamFlix Help Center</h3>
            <p>Ask about your account, a refund, playback problems or our policies. Answers come from the approved Playbook first.</p>
            <div className="suggestions">
              <p className="suggestions-label">Try asking:</p>
              <div className="suggestion-buttons">
                {suggestions.slice(0, 4).map((suggestion, index) => (
                  <button key={index} className="suggestion-btn" onClick={() => sendMessage(suggestion)} disabled={isLoading}>
                    {suggestion}
                  </button>
                ))}
              </div>
            </div>
          </div>
        ) : (
          <>
            {messages.map((message) => (
              <MessageBubble key={message.id} message={message} />
            ))}
            {isLoading && (
              <div className="message assistant loading">
                <div className="message-bubble">
                  <div className="typing-indicator"><span></span><span></span><span></span></div>
                </div>
              </div>
            )}
          </>
        )}
        <div ref={messagesEndRef} />
      </div>

      {messages.length > 0 && (
        <div className="quick-suggestions">
          {suggestions.slice(0, 5).map((suggestion, index) => (
            <button key={index} className="quick-suggestion-btn" onClick={() => sendMessage(suggestion)} disabled={isLoading}>
              {suggestion}
            </button>
          ))}
        </div>
      )}

      <form className="chat-input-form" onSubmit={(e) => { e.preventDefault(); sendMessage(input); }}>
        <input ref={inputRef} type="text" className="chat-input" value={input} onChange={(e) => setInput(e.target.value)}
               placeholder="Type your question..." disabled={isLoading} />
        <button type="submit" className="send-btn" disabled={!input.trim() || isLoading}>
          <svg viewBox="0 0 24 24" fill="currentColor" className="send-icon"><path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z" /></svg>
        </button>
      </form>
    </div>
  );
}

// ------------------------------------------------------------------ bubbles
const BADGES: Record<string, { label: string; className: string; title: string }> = {
  faq: { label: 'Playbook FAQ', className: 'badge-faq', title: 'Approved, published answer returned verbatim. No model call.' },
  skill: { label: 'Playbook Skill', className: 'badge-skill', title: 'Followed a published Skill against live account data.' },
  mixed: { label: 'FAQ + Skill', className: 'badge-mixed', title: 'A FAQ answered the policy part, a Skill handled the live-data part.' },
  rag: { label: 'RAG fallback', className: 'badge-rag', title: 'No governed knowledge matched; answered from help articles.' },
  blocked: { label: 'Off-topic', className: 'badge-blocked', title: 'Blocked by the semantic router guardrail.' },
};

function MessageBubble({ message }: { message: ChatMessage }) {
  const [showTrace, setShowTrace] = useState(false);
  const [showSources, setShowSources] = useState(false);
  const response = message.response;
  const badge = response ? BADGES[response.answered_by] : null;

  return (
    <div className={`message ${message.role}`}>
      <div className="message-bubble">
        {message.role === 'assistant' && response && (
          <div className="response-info">
            {badge && <span className={`answer-badge ${badge.className}`} title={badge.title}>{badge.label}</span>}
            {response.faq_hits.map((hit) => (
              <span key={hit.entryId} className="entry-chip" title={hit.question}>
                {hit.entryKey || hit.entryId.slice(0, 12)} v{hit.version}{hit.score !== undefined && ` · ${(hit.score * 100).toFixed(0)}%`}
              </span>
            ))}
            {response.skill_runs.map((run) => (
              <span key={run.entryId} className="entry-chip skill" title={`${run.resourcesFetched.length} resource(s) fetched, ${run.toolCalls.length} live lookup(s)`}>
                {run.name} v{run.version}
              </span>
            ))}
            <span className="response-time">{response.response_time_ms}ms</span>
            {response.token_usage && <span className="token-usage">{response.token_usage.total_tokens} tokens</span>}
          </div>
        )}

        <div className="message-content">{formatContent(message.content)}</div>

        {response?.proposal?.proposed && (
          <div className="proposal-notice">
            New FAQ proposed from {response.proposal.clusterSize} similar sessions: <em>{response.proposal.question}</em>. Review it in Playbook Studio.
          </div>
        )}
        {response?.proposal && !response.proposal.proposed && response.answered_by === 'rag' && (
          <div className="proposal-hint">
            Recorded as evidence ({response.proposal.clusterSize}/{response.proposal.needed} similar sessions before a FAQ is proposed).
          </div>
        )}

        {response && (response.trace.length > 0 || response.sources.length > 0) && (
          <div className="sources-section">
            <div className="toggle-row">
              {response.trace.length > 0 && (
                <button className="sources-toggle" onClick={() => setShowTrace(!showTrace)}>
                  <span className="sources-icon">{showTrace ? '▼' : '▶'}</span> Playbook trace ({response.trace.length} steps)
                </button>
              )}
              {response.sources.length > 0 && (
                <button className="sources-toggle" onClick={() => setShowSources(!showSources)}>
                  <span className="sources-icon">{showSources ? '▼' : '▶'}</span> {response.sources.length} help article{response.sources.length > 1 ? 's' : ''}
                </button>
              )}
            </div>
            {showTrace && <TraceList steps={response.trace} />}
            {showSources && (
              <div className="sources-list">
                {response.sources.map((source) => <SourceCard key={source.id} source={source} />)}
              </div>
            )}
          </div>
        )}
      </div>
      <span className="message-time">{new Date(message.timestamp).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</span>
    </div>
  );
}

// -------------------------------------------------------------------- trace
const STEP_ICONS: Record<string, string> = {
  guardrail: '🛡', discover: '🔎', route: '↪', faq: '📘', load_skill: '🧩', fetch_resource: '📄', tool: '⚙', llm: '🤖',
  offline_runner: '🧭', answer: '✅', rag: '📚', propose: '💡',
};

function TraceList({ steps }: { steps: TraceStep[] }) {
  return (
    <ol className="trace-list">
      {steps.map((step, index) => (
        <TraceRow key={index} step={step} />
      ))}
    </ol>
  );
}

function TraceRow({ step }: { step: TraceStep }) {
  const [open, setOpen] = useState(false);
  const detailEntries = Object.entries(step.detail || {}).filter(([, value]) => value !== null && value !== undefined && value !== '');
  return (
    <li className={`trace-row status-${step.status}`}>
      <button className="trace-head" onClick={() => setOpen(!open)} disabled={detailEntries.length === 0}>
        <span className="trace-icon">{STEP_ICONS[step.step] || '•'}</span>
        <span className="trace-label">{step.label}</span>
        {step.ms > 0 && <span className="trace-ms">{step.ms}ms</span>}
        {detailEntries.length > 0 && <span className="trace-expand">{open ? '−' : '+'}</span>}
      </button>
      {open && (
        <pre className="trace-detail">{detailEntries.map(([key, value]) => `${key}: ${typeof value === 'string' ? value : JSON.stringify(value, null, 1)}`).join('\n')}</pre>
      )}
    </li>
  );
}

// ------------------------------------------------------------------ sources
function SourceCard({ source }: { source: HelpArticleSource }) {
  const [expanded, setExpanded] = useState(false);
  const colors: Record<string, string> = { account: '#3498db', playback: '#e74c3c', content: '#9b59b6', device: '#2ecc71', billing: '#f39c12', technical: '#34495e' };
  return (
    <div className="source-card">
      <div className="source-header" onClick={() => setExpanded(!expanded)}>
        <span className="source-category" style={{ backgroundColor: colors[source.category] || '#7f8c8d' }}>{source.category}</span>
        <span className="source-title">{source.title}</span>
        {source.similarity != null && <span className="source-similarity">{(source.similarity * 100).toFixed(0)}%</span>}
        <span className="expand-icon">{expanded ? '−' : '+'}</span>
      </div>
      {expanded && <div className="source-content">{source.content}</div>}
    </div>
  );
}

// --------------------------------------------------------------- formatting
function inline(text: string) {
  const parts = text.split(/(\*\*[^*]+\*\*)/g);
  return parts.map((part, i) => (part.startsWith('**') && part.endsWith('**') ? <strong key={i}>{part.slice(2, -2)}</strong> : <span key={i}>{part}</span>));
}

export function formatContent(content: string): React.ReactNode {
  return content.split('\n\n').map((paragraph, index) => {
    if (paragraph.trim() === '---') return <hr key={index} className="content-divider" />;
    const lines = paragraph.split('\n');
    if (lines.every((l) => /^\s*-\s+/.test(l) || l.trim() === '')) {
      return <ul key={index} className="bullet-list">{lines.filter((l) => l.trim()).map((l, i) => <li key={i}>{inline(l.replace(/^\s*-\s+/, ''))}</li>)}</ul>;
    }
    if (lines.every((l) => /^\s*\d+\.\s+/.test(l) || l.trim() === '')) {
      return <ol key={index} className="numbered-list">{lines.filter((l) => l.trim()).map((l, i) => <li key={i}>{inline(l.replace(/^\s*\d+\.\s+/, ''))}</li>)}</ol>;
    }
    return (
      <p key={index}>
        {lines.map((line, i) => (
          <span key={i}>{inline(line)}{i < lines.length - 1 && <br />}</span>
        ))}
      </p>
    );
  });
}
