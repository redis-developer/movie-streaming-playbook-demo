import { useCallback, useEffect, useState } from 'react';
import {
  acceptProposal,
  appendFaqVersion,
  ApiError,
  clearPlaybook,
  clearSessions,
  createFaq,
  deleteFaq,
  discover,
  dismissProposal,
  fetchResource,
  getProposal,
  getSkillVersion,
  listFaqs,
  listFaqVersions,
  listProposals,
  listSessions,
  listSkills,
  listSkillVersions,
  patchPlaybookConfig,
  playbookConfig,
  playbookStats,
  publishFaq,
  publishSkill,
  purgeProposal,
  replaceProposal,
  restoreProposal,
  seedPlaybook,
  withdrawFaq,
  withdrawSkill,
  type DiscoverResult,
  type PlaybookConfig,
  type PlaybookStats,
  type PreviewEntry,
  type Proposal,
  type ProposalPreview,
  type SessionRecord,
  type StableEntry,
  type Version,
} from '../api/playbookApi';
import { formatContent } from '../components/HelpChat';
import '../styles/PlaybookStudio.css';

type Tab = 'faqs' | 'skills' | 'proposals' | 'discovery' | 'settings';

interface StudioProps {
  onChanged: () => void;
}

const fmtDate = (iso?: string) => (iso ? new Date(iso).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' }) : '');
const shortDigest = (digest?: string) => (digest ? digest.replace('sha256:', '').slice(0, 10) : '');
const errorText = (error: unknown) => (error instanceof ApiError ? `${error.message}${error.type ? ` (${error.type})` : ''}` : error instanceof Error ? error.message : 'Unknown error');

export function PlaybookStudio({ onChanged }: StudioProps) {
  const [tab, setTab] = useState<Tab>('faqs');
  const [stats, setStats] = useState<PlaybookStats | null>(null);
  const [notice, setNotice] = useState<{ text: string; kind: 'ok' | 'error' } | null>(null);

  const refreshStats = useCallback(() => {
    playbookStats().then(setStats).catch(() => setStats(null));
  }, []);

  useEffect(() => { refreshStats(); }, [refreshStats, tab]);

  const changed = () => { refreshStats(); onChanged(); };
  const ok = (text: string) => setNotice({ text, kind: 'ok' });
  const fail = (error: unknown) => setNotice({ text: errorText(error), kind: 'error' });

  return (
    <div className="studio-page">
      <header className="studio-header">
        <div>
          <h1>Playbook Studio</h1>
          <p>
            {stats ? <>
              <strong>{stats.name}</strong> · {stats.mode === 'remote' ? 'Redis Playbook service' : 'embedded on Redis'} ·{' '}
              {stats.faqs.published}/{stats.faqs.total} FAQs published · {stats.skills.published}/{stats.skills.total} Skills published ·{' '}
              {stats.proposals.open} open proposal(s)
            </> : 'Loading…'}
          </p>
        </div>
        <nav className="studio-tabs">
          {([
            ['faqs', 'FAQs'], ['skills', 'Skills'], ['proposals', `Proposals${stats?.proposals.open ? ` (${stats.proposals.open})` : ''}`],
            ['discovery', 'Discovery'], ['settings', 'Settings'],
          ] as [Tab, string][]).map(([key, label]) => (
            <button key={key} className={`studio-tab ${tab === key ? 'active' : ''}`} onClick={() => setTab(key)}>{label}</button>
          ))}
        </nav>
      </header>

      {notice && <div className={`studio-notice ${notice.kind}`} onClick={() => setNotice(null)}>{notice.text}</div>}

      <main className="studio-main">
        {tab === 'faqs' && <FaqRegistry onChanged={changed} ok={ok} fail={fail} />}
        {tab === 'skills' && <SkillRegistry onChanged={changed} ok={ok} fail={fail} />}
        {tab === 'proposals' && <ProposalsInbox onChanged={changed} ok={ok} fail={fail} goToFaqs={() => setTab('faqs')} />}
        {tab === 'discovery' && <DiscoveryPlayground fail={fail} />}
        {tab === 'settings' && <Settings onChanged={changed} ok={ok} fail={fail} />}
      </main>
    </div>
  );
}

interface PanelProps {
  onChanged: () => void;
  ok: (text: string) => void;
  fail: (error: unknown) => void;
}

// =============================================================== FAQ registry
function FaqRegistry({ onChanged, ok, fail }: PanelProps) {
  const [faqs, setFaqs] = useState<StableEntry[]>([]);
  const [selected, setSelected] = useState<StableEntry | null>(null);
  const [versions, setVersions] = useState<Version[]>([]);
  const [draft, setDraft] = useState<{ question: string; answer: string; intents: string } | null>(null);
  const [newFaq, setNewFaq] = useState<{ entryKey: string; question: string; answer: string; intents: string } | null>(null);

  const load = useCallback(async () => {
    const data = await listFaqs().catch(() => ({ faqs: [] }));
    setFaqs(data.faqs);
    if (selected) {
      const fresh = data.faqs.find((f) => f.entryId === selected.entryId) || null;
      setSelected(fresh);
    }
  }, [selected]);

  useEffect(() => { load(); /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, []);

  useEffect(() => {
    if (!selected) { setVersions([]); return; }
    listFaqVersions(selected.entryId).then((data) => setVersions(data.versions)).catch(fail);
  }, [selected, fail]);

  const act = async (fn: () => Promise<unknown>, message: string) => {
    try {
      await fn();
      ok(message);
      await load();
      onChanged();
    } catch (error) {
      fail(error);
    }
  };

  const select = (faq: StableEntry) => { setSelected(faq); setDraft(null); };

  return (
    <div className="registry">
      <div className="registry-list">
        <div className="list-head">
          <h2>FAQs</h2>
          <button className="primary-btn small" onClick={() => setNewFaq({ entryKey: '', question: '', answer: '', intents: '' })}>New FAQ</button>
        </div>
        {newFaq && (
          <form className="editor" onSubmit={(e) => {
            e.preventDefault();
            act(() => createFaq({ entryKey: newFaq.entryKey || undefined, question: newFaq.question, answer: newFaq.answer,
              intents: newFaq.intents.split(',').map((s) => s.trim()).filter(Boolean) }), 'FAQ created as an unpublished draft (v1)')
              .then(() => setNewFaq(null));
          }}>
            <input placeholder="entry key (kebab-case, optional)" value={newFaq.entryKey} onChange={(e) => setNewFaq({ ...newFaq, entryKey: e.target.value })} />
            <input placeholder="Question" required value={newFaq.question} onChange={(e) => setNewFaq({ ...newFaq, question: e.target.value })} />
            <textarea placeholder="Approved answer" required rows={4} value={newFaq.answer} onChange={(e) => setNewFaq({ ...newFaq, answer: e.target.value })} />
            <input placeholder="intents, comma separated (optional)" value={newFaq.intents} onChange={(e) => setNewFaq({ ...newFaq, intents: e.target.value })} />
            <div className="editor-actions">
              <button type="submit" className="primary-btn small">Create draft</button>
              <button type="button" className="ghost-btn small" onClick={() => setNewFaq(null)}>Cancel</button>
            </div>
          </form>
        )}
        <table className="entry-table">
          <thead><tr><th>Entry</th><th>Latest</th><th>Published</th><th>Updated</th></tr></thead>
          <tbody>
            {faqs.map((faq) => (
              <tr key={faq.entryId} className={selected?.entryId === faq.entryId ? 'selected' : ''} onClick={() => select(faq)}>
                <td>
                  <div className="entry-key">{faq.entryKey || faq.entryId.slice(0, 14)}</div>
                  <div className="entry-question">{faq.publication?.content.payload.faq?.question || '(unpublished draft)'}</div>
                </td>
                <td>v{faq.latestVersion}</td>
                <td>
                  {faq.publication ? <span className="pill published">v{faq.publication.version}</span> : <span className="pill draft">unpublished</span>}
                  {faq.publication && faq.publication.version < faq.latestVersion && <span className="pill newer">draft v{faq.latestVersion}</span>}
                </td>
                <td className="muted">{fmtDate(faq.updatedAt)}</td>
              </tr>
            ))}
            {faqs.length === 0 && <tr><td colSpan={4} className="muted">No FAQs yet. Seed the Playbook in Settings or create one.</td></tr>}
          </tbody>
        </table>
      </div>

      <div className="registry-detail">
        {!selected ? (
          <div className="empty">Select a FAQ to see its versions. Publishing an older version is a rollback; agents only ever see the published one.</div>
        ) : (
          <>
            <div className="detail-head">
              <div>
                <h3>{selected.entryKey || selected.entryId}</h3>
                <p className="muted">{selected.entryId} · created {fmtDate(selected.createdAt)}</p>
              </div>
              <div className="detail-actions">
                <button className="ghost-btn small" onClick={() => {
                  const latest = versions[0]?.content.payload.faq;
                  setDraft({ question: latest?.question || '', answer: latest?.answer || '', intents: (versions[0]?.content.intents || []).join(', ') });
                }}>New version</button>
                {selected.publication && (
                  <button className="warn-btn small" onClick={() => act(() => withdrawFaq(selected.entryId), 'Publication withdrawn: agents no longer see this FAQ')}>Withdraw</button>
                )}
                <button className="danger-btn small" onClick={() => { if (confirm('Delete this FAQ and all its versions?')) act(() => deleteFaq(selected.entryId), 'FAQ deleted').then(() => setSelected(null)); }}>Delete</button>
              </div>
            </div>

            {draft && (
              <form className="editor" onSubmit={(e) => {
                e.preventDefault();
                act(() => appendFaqVersion(selected.entryId, { question: draft.question, answer: draft.answer,
                  intents: draft.intents.split(',').map((s) => s.trim()).filter(Boolean) }), `Appended v${selected.latestVersion + 1} as an unpublished draft`)
                  .then(() => setDraft(null));
              }}>
                <input required value={draft.question} onChange={(e) => setDraft({ ...draft, question: e.target.value })} />
                <textarea required rows={5} value={draft.answer} onChange={(e) => setDraft({ ...draft, answer: e.target.value })} />
                <input placeholder="intents, comma separated" value={draft.intents} onChange={(e) => setDraft({ ...draft, intents: e.target.value })} />
                <div className="editor-actions">
                  <button type="submit" className="primary-btn small">Save as new version (unpublished)</button>
                  <button type="button" className="ghost-btn small" onClick={() => setDraft(null)}>Cancel</button>
                </div>
              </form>
            )}

            <div className="version-list">
              {versions.map((version) => {
                const isPublished = selected.publication?.version === version.version;
                return (
                  <div key={version.version} className={`version-card ${isPublished ? 'published' : ''}`}>
                    <div className="version-head">
                      <span className="version-number">v{version.version}</span>
                      {isPublished ? <span className="pill published">published</span> : <span className="pill draft">{version.version === selected.latestVersion ? 'latest draft' : 'previous'}</span>}
                      <span className="muted">{fmtDate(version.createdAt)} · {shortDigest(version.contentDigest)}</span>
                      {!isPublished && (
                        <button className="primary-btn small" onClick={() => act(() => publishFaq(selected.entryId, version.version),
                          selected.publication && version.version < selected.publication.version ? `Rolled back to v${version.version}` : `Published v${version.version}`)}>
                          {selected.publication && version.version < selected.publication.version ? 'Roll back to this' : 'Publish'}
                        </button>
                      )}
                    </div>
                    <div className="version-q">{version.content.payload.faq?.question}</div>
                    <div className="version-a">{formatContent(version.content.payload.faq?.answer || '')}</div>
                    {version.content.intents.length > 0 && <div className="muted small">intents: {version.content.intents.join(', ')}</div>}
                  </div>
                );
              })}
            </div>
          </>
        )}
      </div>
    </div>
  );
}

// ============================================================= Skill registry
function SkillRegistry({ onChanged, ok, fail }: PanelProps) {
  const [skills, setSkills] = useState<StableEntry[]>([]);
  const [selected, setSelected] = useState<StableEntry | null>(null);
  const [versions, setVersions] = useState<Version[]>([]);
  const [resourceView, setResourceView] = useState<{ path: string; text: string; digest: string } | null>(null);

  const load = useCallback(async () => {
    const data = await listSkills().catch(() => ({ skills: [] }));
    setSkills(data.skills);
    if (selected) setSelected(data.skills.find((s) => s.entryId === selected.entryId) || null);
  }, [selected]);

  useEffect(() => { load(); /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, []);
  useEffect(() => {
    if (!selected) { setVersions([]); return; }
    listSkillVersions(selected.entryId).then((d) => setVersions(d.versions)).catch(fail);
    setResourceView(null);
  }, [selected, fail]);

  const act = async (fn: () => Promise<unknown>, message: string) => {
    try { await fn(); ok(message); await load(); onChanged(); } catch (error) { fail(error); }
  };

  return (
    <div className="registry">
      <div className="registry-list">
        <div className="list-head"><h2>Skills</h2><span className="muted small">Upload a new package with POST /api/playbook/skills (base64 ZIP of a SKILL.md folder)</span></div>
        <table className="entry-table">
          <thead><tr><th>Skill</th><th>Latest</th><th>Published</th><th>Updated</th></tr></thead>
          <tbody>
            {skills.map((skill) => (
              <tr key={skill.entryId} className={selected?.entryId === skill.entryId ? 'selected' : ''} onClick={() => setSelected(skill)}>
                <td>
                  <div className="entry-key">{skill.entryKey || skill.publication?.content.payload.skill?.name || skill.entryId.slice(0, 14)}</div>
                  <div className="entry-question">{skill.publication?.content.payload.skill?.description || '(unpublished)'}</div>
                </td>
                <td>v{skill.latestVersion}</td>
                <td>{skill.publication ? <span className="pill published">v{skill.publication.version}</span> : <span className="pill draft">unpublished</span>}</td>
                <td className="muted">{fmtDate(skill.updatedAt)}</td>
              </tr>
            ))}
            {skills.length === 0 && <tr><td colSpan={4} className="muted">No Skills yet. Seed the Playbook in Settings.</td></tr>}
          </tbody>
        </table>
      </div>
      <div className="registry-detail">
        {!selected ? (
          <div className="empty">Select a Skill. Discovery only returns its name, description and SKILL.md URI; agents load instructions and resources progressively.</div>
        ) : (
          <>
            <div className="detail-head">
              <div>
                <h3>{selected.entryKey}</h3>
                <p className="muted">{selected.entryId}{selected.publication && <> · published URI <code>{selected.publication.content.payload.skill?.uri}</code></>}</p>
              </div>
              <div className="detail-actions">
                {selected.publication && <button className="warn-btn small" onClick={() => act(() => withdrawSkill(selected.entryId), 'Skill withdrawn')}>Withdraw</button>}
              </div>
            </div>
            {versions.map((version) => {
              const skill = version.content.payload.skill!;
              const isPublished = selected.publication?.version === version.version;
              return (
                <div key={version.version} className={`version-card ${isPublished ? 'published' : ''}`}>
                  <div className="version-head">
                    <span className="version-number">v{version.version}</span>
                    {isPublished ? <span className="pill published">published</span> : <span className="pill draft">draft</span>}
                    <span className="muted">{fmtDate(version.createdAt)} · {shortDigest(version.contentDigest)}</span>
                    {!isPublished && <button className="primary-btn small" onClick={() => act(() => publishSkill(selected.entryId, version.version), `Published Skill v${version.version}`)}>Publish</button>}
                  </div>
                  <div className="manifest">
                    <strong>{skill.manifest.name}</strong> — {skill.manifest.description}
                    {skill.manifest.allowedTools && <div className="muted small">allowed tools: {skill.manifest.allowedTools.join(', ')}</div>}
                  </div>
                  <details className="instructions">
                    <summary>Instructions ({skill.instructions.length} chars)</summary>
                    <pre>{skill.instructions}</pre>
                  </details>
                  <table className="resource-table">
                    <thead><tr><th>Resource</th><th>Type</th><th>Size</th><th>Digest</th><th></th></tr></thead>
                    <tbody>
                      {skill.resources.map((resource) => (
                        <tr key={resource.resourceId}>
                          <td><code>{resource.path}</code>{resource.executable && <span className="pill warn">executable (never run)</span>}</td>
                          <td className="muted">{resource.mimeType}</td>
                          <td className="muted">{resource.sizeBytes} B</td>
                          <td className="muted">{shortDigest(resource.digest)}</td>
                          <td>
                            <button className="ghost-btn small" disabled={!isPublished} title={isPublished ? 'fetch_resource by URI' : 'Only the published version can be fetched by URI'}
                              onClick={() => fetchResource(resource.uri).then((r) => setResourceView({ path: r.path, text: r.text || '(binary)', digest: r.digest })).catch(fail)}>
                              Fetch
                            </button>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              );
            })}
            {resourceView && (
              <div className="resource-view">
                <div className="version-head"><strong>{resourceView.path}</strong><span className="muted">{shortDigest(resourceView.digest)}</span>
                  <button className="ghost-btn small" onClick={() => setResourceView(null)}>Close</button></div>
                <pre>{resourceView.text}</pre>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}

// ============================================================ Proposals inbox
function ProposalsInbox({ onChanged, ok, fail, goToFaqs }: PanelProps & { goToFaqs: () => void }) {
  const [view, setView] = useState<'inbox' | 'trash'>('inbox');
  const [proposals, setProposals] = useState<ProposalPreview[]>([]);
  const [selected, setSelected] = useState<Proposal | null>(null);
  const [edit, setEdit] = useState<{ question: string; answer: string; entryKey: string; intents: string } | null>(null);
  const [accepted, setAccepted] = useState<{ entryId: string; entryKey?: string } | null>(null);

  const load = useCallback(async () => {
    const data = await listProposals(view).catch(() => ({ proposals: [] }));
    setProposals(data.proposals);
  }, [view]);

  useEffect(() => { load(); setSelected(null); setEdit(null); setAccepted(null); }, [load]);

  const open = async (proposalId: string) => {
    try {
      const proposal = await getProposal(proposalId);
      setSelected(proposal);
      const faq = proposal.action.add.candidate.content.payload.faq;
      setEdit({ question: faq.question, answer: faq.answer, entryKey: proposal.action.add.target.newEntry?.entryKey || '',
        intents: (proposal.action.add.candidate.content.intents || []).join(', ') });
      setAccepted(proposal.targetEntryId ? { entryId: proposal.targetEntryId } : null);
    } catch (error) { fail(error); }
  };

  const act = async (fn: () => Promise<unknown>, message: string, reopen = true) => {
    try {
      await fn();
      ok(message);
      await load();
      if (reopen && selected) await open(selected.proposalId);
      onChanged();
    } catch (error) { fail(error); if (selected) open(selected.proposalId); }
  };

  return (
    <div className="registry">
      <div className="registry-list">
        <div className="list-head">
          <h2>Proposals</h2>
          <div className="segmented">
            <button className={view === 'inbox' ? 'active' : ''} onClick={() => setView('inbox')}>Inbox</button>
            <button className={view === 'trash' ? 'active' : ''} onClick={() => setView('trash')}>Trash</button>
          </div>
        </div>
        <p className="muted small">Candidates extracted from support sessions. Nothing here is visible to agents until a human accepts and publishes it.</p>
        <table className="entry-table">
          <thead><tr><th>Candidate FAQ</th><th>Evidence</th><th>Status</th><th>Updated</th></tr></thead>
          <tbody>
            {proposals.map((p) => (
              <tr key={p.proposalId} className={selected?.proposalId === p.proposalId ? 'selected' : ''} onClick={() => open(p.proposalId)}>
                <td>
                  <div className="entry-question strong">{p.action.add.candidate.content.payload.faq.question}</div>
                  <div className="muted small">{p.action.add.target.newEntry?.entryKey || 'no entry key'} · rev {p.proposalRevision}</div>
                </td>
                <td>{p.evidenceCount} session{p.evidenceCount === 1 ? '' : 's'}</td>
                <td><span className={`pill status-${p.status}`}>{p.status}</span></td>
                <td className="muted">{fmtDate(p.updatedAt)}</td>
              </tr>
            ))}
            {proposals.length === 0 && <tr><td colSpan={4} className="muted">{view === 'inbox' ? 'Inbox is empty. Ask the Help Center the same policy question three times to see a proposal appear.' : 'Trash is empty.'}</td></tr>}
          </tbody>
        </table>
      </div>

      <div className="registry-detail">
        {!selected || !edit ? (
          <div className="empty">Select a proposal to review its evidence, edit the candidate answer and accept it as a draft FAQ.</div>
        ) : (
          <>
            <div className="detail-head">
              <div>
                <h3>Review candidate</h3>
                <p className="muted">{selected.proposalId} · revision {selected.proposalRevision} · {selected.evidenceCount} evidence item(s) · inbox expires {fmtDate(selected.inboxExpiresAt)}</p>
                {selected.source && <p className="muted small">source: {selected.source.type} {JSON.stringify(selected.source.reference || {})}</p>}
              </div>
              <span className={`pill status-${selected.status}`}>{selected.status}</span>
            </div>

            <form className="editor" onSubmit={(e) => {
              e.preventDefault();
              act(() => replaceProposal(selected.proposalId, { expectedProposalRevision: selected.proposalRevision, question: edit.question, answer: edit.answer, entryKey: edit.entryKey || undefined,
                intents: edit.intents.split(',').map((s) => s.trim()).filter(Boolean).slice(0, 5) }),
                'Candidate updated');
            }}>
              <label>Entry key<input value={edit.entryKey} onChange={(e) => setEdit({ ...edit, entryKey: e.target.value })} disabled={selected.status !== 'open'} /></label>
              <label>Question<input required value={edit.question} onChange={(e) => setEdit({ ...edit, question: e.target.value })} disabled={selected.status !== 'open'} /></label>
              <label>Intents (how customers phrase it, up to 5, comma separated)<input value={edit.intents} onChange={(e) => setEdit({ ...edit, intents: e.target.value })} disabled={selected.status !== 'open'} /></label>
              <label>Approved answer<textarea required rows={7} value={edit.answer} onChange={(e) => setEdit({ ...edit, answer: e.target.value })} disabled={selected.status !== 'open'} /></label>
              {selected.status === 'open' && (
                <div className="editor-actions">
                  <button type="submit" className="ghost-btn small">Save edits</button>
                  <button type="button" className="primary-btn small" onClick={() => act(async () => {
                    const result = await acceptProposal(selected.proposalId, selected.proposalRevision);
                    setAccepted({ entryId: result.entry.entryId, entryKey: result.entry.entryKey });
                  }, 'Accepted as an unpublished FAQ draft (v1). Publish it to make it live.')}>Accept as draft FAQ</button>
                  <button type="button" className="warn-btn small" onClick={() => act(() => dismissProposal(selected.proposalId), 'Proposal moved to trash', false).then(() => setSelected(null))}>Dismiss</button>
                </div>
              )}
              {selected.status === 'trashed' && (
                <div className="editor-actions">
                  <button type="button" className="ghost-btn small" onClick={() => act(() => restoreProposal(selected.proposalId), 'Proposal restored to the inbox', false).then(() => setSelected(null))}>Restore</button>
                  <button type="button" className="danger-btn small" onClick={() => act(() => purgeProposal(selected.proposalId), 'Proposal permanently deleted', false).then(() => setSelected(null))}>Delete permanently</button>
                </div>
              )}
              {(selected.status === 'promoted' || accepted) && accepted && (
                <div className="accepted-box">
                  Draft FAQ <code>{accepted.entryKey || accepted.entryId}</code> created, not yet published.
                  <button type="button" className="primary-btn small" onClick={() => act(() => publishFaq(accepted.entryId, 1), 'Published v1: agents now answer with this FAQ', false)}>Publish v1 now</button>
                  <button type="button" className="ghost-btn small" onClick={goToFaqs}>Open in FAQs</button>
                </div>
              )}
            </form>

            <h4>Evidence from support sessions</h4>
            <div className="evidence-list">
              {selected.evidence.map((item) => (
                <div key={item.evidenceId} className="evidence-card">
                  <div className="evidence-head">
                    <span className="pill">{item.source.type}</span>
                    <span className="muted small">{fmtDate(item.observedAt)} · {item.producer.name} {item.producer.version}</span>
                  </div>
                  <div className="evidence-q">“{String(item.source.content?.question || '')}”</div>
                  <div className="evidence-a">{String(item.source.content?.answerExcerpt || '')}</div>
                  <div className="muted small">session {String(item.source.reference?.sessionId || '')} · answered by {String(item.source.content?.answeredBy || '')}</div>
                </div>
              ))}
              {selected.evidence.length === 0 && <p className="muted">No evidence attached.</p>}
            </div>
          </>
        )}
      </div>
    </div>
  );
}

// ========================================================== Discovery playground
function DiscoveryPlayground({ fail }: { fail: (error: unknown) => void }) {
  const [query, setQuery] = useState('I was charged twice and my video keeps buffering on my TV');
  const [limit, setLimit] = useState<number | ''>('');
  const [pins, setPins] = useState('');
  const [result, setResult] = useState<DiscoverResult | null>(null);
  const [loaded, setLoaded] = useState<Version | null>(null);
  const [resource, setResource] = useState<{ path: string; text: string } | null>(null);
  const [running, setRunning] = useState(false);

  const run = async () => {
    setRunning(true);
    setLoaded(null);
    setResource(null);
    try {
      setResult(await discover({ query, limit: limit === '' ? undefined : Number(limit),
        pinnedEntries: pins.split(',').map((p) => p.trim()).filter(Boolean).map((entryKey) => ({ entryKey })) }));
    } catch (error) { fail(error); } finally { setRunning(false); }
  };

  const loadSkill = async (entry: PreviewEntry) => {
    try { setLoaded(await getSkillVersion(entry.entryId, entry.version)); setResource(null); } catch (error) { fail(error); }
  };

  const EntryCard = ({ entry, pinned }: { entry: PreviewEntry; pinned?: boolean }) => (
    <div className={`preview-card ${entry.entryType}`}>
      <div className="version-head">
        <span className={`pill ${entry.entryType}`}>{entry.entryType}</span>
        <strong>{entry.entryKey || entry.entryId.slice(0, 14)}</strong>
        <span className="muted">v{entry.version}</span>
        {pinned ? <span className="pill">pinned</span> : <span className="score">{((entry.score || 0) * 100).toFixed(0)}%</span>}
      </div>
      {entry.faq && <><div className="version-q">{entry.faq.question}</div><div className="version-a small">{entry.faq.answer}</div></>}
      {entry.skill && (
        <>
          <div className="version-q">{entry.skill.name}</div>
          <div className="version-a small">{entry.skill.description}</div>
          <div className="muted small"><code>{entry.skill.uri}</code></div>
          <button className="ghost-btn small" onClick={() => loadSkill(entry)}>load_skill</button>
        </>
      )}
    </div>
  );

  return (
    <div className="playground">
      <div className="playground-form">
        <h2>Discovery playground</h2>
        <p className="muted">What an agent receives from one <code>discover</code> call: ranked FAQ answers and compact Skill previews, under the Playbook's score floors, result cap and preview token budget (set in Settings).</p>
        <textarea rows={2} value={query} onChange={(e) => setQuery(e.target.value)} />
        <div className="inline-controls">
          <label>limit <input type="number" min={1} max={50} value={limit} onChange={(e) => setLimit(e.target.value === '' ? '' : Number(e.target.value))} placeholder="policy" /></label>
          <label className="grow">pin entry keys <input value={pins} onChange={(e) => setPins(e.target.value)} placeholder="escalate-to-human, household-sharing" /></label>
          <button className="primary-btn" onClick={run} disabled={running || !query.trim()}>{running ? 'Discovering…' : 'Discover'}</button>
        </div>
      </div>

      {result && (
        <div className="playground-result">
          <div className="result-flags">
            <span className="pill">{result.pinnedEntries.length} pinned</span>
            <span className="pill">{result.discoveredEntries.length} discovered</span>
            {result.candidatesConsidered != null && <span className="pill">{result.candidatesConsidered} candidates considered</span>}
            {result.estimatedPreviewTokens != null && <span className="pill">~{result.estimatedPreviewTokens} preview tokens{result.maxPreviewTokens ? ` / ${result.maxPreviewTokens}` : ''}</span>}
            {result.truncated && <span className="pill warn">truncated: more eligible than slots</span>}
            {result.budgetTruncated && <span className="pill warn">budgetTruncated: {result.budgetSkipped?.length || ''} skipped for size</span>}
          </div>
          {result.budgetSkipped && result.budgetSkipped.length > 0 && (
            <p className="muted small">Skipped by budget: {result.budgetSkipped.map((s) => `${s.entryType} ${s.entryId.slice(0, 12)} (${s.tokens} tokens, ${(s.score * 100).toFixed(0)}%)`).join('; ')}</p>
          )}
          <div className="preview-grid">
            {result.pinnedEntries.map((e) => <EntryCard key={e.entryId} entry={e} pinned />)}
            {result.discoveredEntries.map((e) => <EntryCard key={e.entryId} entry={e} />)}
            {result.pinnedEntries.length + result.discoveredEntries.length === 0 && <p className="muted">Nothing above the score floors{result.budgetTruncated ? ' fit in the budget' : ''}.</p>}
          </div>
          {loaded && loaded.content.payload.skill && (
            <div className="version-card published">
              <div className="version-head"><span className="version-number">load_skill → {loaded.content.payload.skill.manifest.name} v{loaded.version}</span><span className="muted">{shortDigest(loaded.contentDigest)}</span></div>
              <details open className="instructions"><summary>Instructions</summary><pre>{loaded.content.payload.skill.instructions}</pre></details>
              <div className="resource-chips">
                {loaded.content.payload.skill.resources.filter((r) => r.path !== 'SKILL.md').map((r) => (
                  <button key={r.resourceId} className="ghost-btn small" onClick={() => fetchResource(r.uri).then((x) => setResource({ path: x.path, text: x.text || '(binary)' })).catch(fail)}>
                    fetch_resource {r.path} ({r.sizeBytes} B)
                  </button>
                ))}
              </div>
              {resource && <div className="resource-view"><strong>{resource.path}</strong><pre>{resource.text}</pre></div>}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ================================================================== Settings
function Settings({ onChanged, ok, fail }: PanelProps) {
  const [config, setConfig] = useState<PlaybookConfig | null>(null);
  const [form, setForm] = useState({ faqMinimumScore: 0.72, skillMinimumScore: 0.62, maxResults: 6, maxPreviewTokens: '' as string | number });
  const [sessions, setSessions] = useState<SessionRecord[]>([]);
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    playbookConfig().then((c) => {
      setConfig(c);
      setForm({
        faqMinimumScore: c.discovery.faqMinimumScore ?? 0.72, skillMinimumScore: c.discovery.skillMinimumScore ?? 0.62,
        maxResults: c.discovery.maxResults ?? 6, maxPreviewTokens: c.discovery.maxPreviewTokens ?? '',
      });
    }).catch(fail);
    listSessions(25).then((d) => setSessions(d.sessions)).catch(() => setSessions([]));
  }, [fail]);

  useEffect(() => { load(); }, [load]);

  const act = async (fn: () => Promise<unknown>, message: string) => {
    setBusy(true);
    try { await fn(); ok(message); load(); onChanged(); } catch (error) { fail(error); } finally { setBusy(false); }
  };

  return (
    <div className="settings">
      <section className="settings-card">
        <h2>Discovery policy</h2>
        <p className="muted small">{config?.note || 'Scores are normalized cosine similarity ((2 − distance) / 2). The preview token budget caps one discovery payload (o200k_base estimate).'}</p>
        <form className="settings-form" onSubmit={(e) => {
          e.preventDefault();
          act(() => patchPlaybookConfig({ discovery: {
            faqMinimumScore: Number(form.faqMinimumScore), skillMinimumScore: Number(form.skillMinimumScore), maxResults: Number(form.maxResults),
            maxPreviewTokens: form.maxPreviewTokens === '' ? null : Number(form.maxPreviewTokens),
          } }), 'Discovery policy saved');
        }}>
          <label>FAQ minimum score<input type="number" step="0.01" min={0} max={1} value={form.faqMinimumScore} onChange={(e) => setForm({ ...form, faqMinimumScore: Number(e.target.value) })} /></label>
          <label>Skill minimum score<input type="number" step="0.01" min={0} max={1} value={form.skillMinimumScore} onChange={(e) => setForm({ ...form, skillMinimumScore: Number(e.target.value) })} /></label>
          <label>Max results<input type="number" min={1} max={50} value={form.maxResults} onChange={(e) => setForm({ ...form, maxResults: Number(e.target.value) })} /></label>
          <label>Max preview tokens<input type="number" min={64} placeholder="unlimited" value={form.maxPreviewTokens} onChange={(e) => setForm({ ...form, maxPreviewTokens: e.target.value })} /></label>
          <button type="submit" className="primary-btn" disabled={busy || !!config?.note}>Save policy</button>
        </form>
      </section>

      <section className="settings-card">
        <h2>Data</h2>
        <div className="settings-actions">
          <button className="primary-btn" disabled={busy} onClick={() => act(() => seedPlaybook(false), 'Seeded missing FAQs, Skills and demo accounts')}>Seed Playbook</button>
          <button className="warn-btn" disabled={busy} onClick={() => { if (confirm('Clear the Playbook and sessions, then reseed?')) act(() => seedPlaybook(true), 'Reset and reseeded'); }}>Reset &amp; reseed</button>
          <button className="danger-btn" disabled={busy} onClick={() => { if (confirm('Delete every FAQ, Skill and proposal?')) act(() => clearPlaybook('all'), 'Playbook cleared'); }}>Clear Playbook</button>
          <button className="ghost-btn" disabled={busy} onClick={() => act(() => clearSessions(), 'Sessions cleared')}>Clear sessions</button>
        </div>
        <p className="muted small">Seeding is idempotent by entry key. It loads resources/playbook/faqs.json, resources/playbook/skills/* and resources/playbook/accounts.json.</p>
      </section>

      <section className="settings-card">
        <h2>Recent support sessions</h2>
        <p className="muted small">Sessions answered without an approved FAQ (answered by <code>rag</code>) are evidence for proposals once enough similar ones exist.</p>
        <table className="entry-table compact">
          <thead><tr><th>When</th><th>Question</th><th>Answered by</th><th>Proposal</th></tr></thead>
          <tbody>
            {sessions.map((s) => (
              <tr key={s.session_id}>
                <td className="muted">{fmtDate(s.created_at)}</td>
                <td>{s.question}</td>
                <td><span className={`pill by-${s.answered_by}`}>{s.answered_by}</span></td>
                <td className="muted small">{s.proposal_id || '—'}</td>
              </tr>
            ))}
            {sessions.length === 0 && <tr><td colSpan={4} className="muted">No sessions yet.</td></tr>}
          </tbody>
        </table>
      </section>
    </div>
  );
}
