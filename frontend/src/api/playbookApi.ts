/**
 * API client for the StreamFlix Help Center on Redis Playbook
 */
const API_BASE = import.meta.env.VITE_API_URL || '/api';

// ---------------------------------------------------------------- shared types
export interface FaqContent {
  question: string;
  answer: string;
}

export interface SkillPreview {
  name: string;
  description: string;
  uri: string;
}

export interface PreviewEntry {
  entryId: string;
  entryKey?: string;
  entryType: 'faq' | 'skill';
  version: number;
  intents: string[];
  faq?: FaqContent;
  skill?: SkillPreview;
  score?: number;
}

export interface DiscoverResult {
  pinnedEntries: PreviewEntry[];
  discoveredEntries: PreviewEntry[];
  truncated: boolean;
  budgetTruncated?: boolean;
  estimatedPreviewTokens?: number;
  maxPreviewTokens?: number | null;
  budgetSkipped?: { entryId: string; entryType: string; tokens: number; score: number }[];
  candidatesConsidered?: number;
}

export interface ContentPreview {
  payload: { faq?: FaqContent; skill?: SkillPreview };
  intents: string[];
  attributes: Record<string, unknown>;
  metadata?: unknown;
}

export interface StableEntry {
  entryId: string;
  entryKey?: string;
  entryType: 'faq' | 'skill';
  latestVersion: number;
  publication?: { version: number; at: string; content: ContentPreview };
  createdAt: string;
  updatedAt: string;
}

export interface ResourceDescriptor {
  resourceId: string;
  uri: string;
  path: string;
  digest: string;
  sizeBytes: number;
  mimeType: string;
  executable: boolean;
}

export interface SkillContent {
  format: string;
  manifest: {
    name: string;
    description: string;
    license?: string;
    compatibility?: string;
    metadata?: Record<string, string>;
    allowedTools?: string[];
  };
  instructions: string;
  resources: ResourceDescriptor[];
}

export interface Version {
  entryId: string;
  entryType: 'faq' | 'skill';
  version: number;
  createdAt: string;
  contentDigest: string;
  content: {
    payload: { faq?: FaqContent; skill?: SkillContent };
    intents: string[];
    attributes: Record<string, unknown>;
    metadata?: unknown;
  };
}

export interface ProposalEvidence {
  evidenceId: string;
  schemaVersion: number;
  observedAt: string;
  producer: { name: string; version: string };
  source: { type: string; reference?: Record<string, unknown>; content?: Record<string, unknown> };
}

export interface ProposalPreview {
  proposalId: string;
  status: 'open' | 'trashed' | 'promoting' | 'promoted';
  contentDigest: string;
  proposalRevision: number | string;
  evidenceCount: number;
  createdAt: string;
  updatedAt: string;
  inboxExpiresAt: string;
  purgeAt: string;
  targetEntryId?: string;
  action: {
    add: {
      target: { newEntry?: { entryKey?: string } };
      candidate: { content: { payload: {
        faq?: { question: string };
        // Proposal list responses use a compact Skill preview, while detail
        // responses return the full Agent Skills document.
        skill?: {
          name?: string;
          description?: string;
          manifest?: SkillContent['manifest'];
          instructions?: string;
          resources?: ResourceDescriptor[];
        };
      }; metadata?: unknown } };
    };
  };
}

export interface Proposal extends Omit<ProposalPreview, 'action'> {
  source?: { type: string; reference?: Record<string, unknown>; content?: Record<string, unknown> };
  action: {
    add: {
      target: { newEntry?: { entryKey?: string } };
      candidate: { content: { payload: { faq?: FaqContent; skill?: SkillContent }; intents: string[]; attributes: Record<string, unknown>; metadata?: unknown } };
    };
  };
  evidence: ProposalEvidence[];
}

export interface PlaybookConfig {
  playbookId: string;
  name: string;
  state?: string;
  discovery: {
    faqMinimumScore?: number;
    skillMinimumScore?: number;
    maxResults?: number;
    maxPreviewTokens?: number | null;
  };
  proposalPolicy: Record<string, number>;
  note?: string;
}

export interface PlaybookStats {
  mode: 'embedded' | 'remote';
  playbookId: string;
  name: string;
  faqs: { total: number; published: number; drafts: number };
  skills: { total: number; published: number };
  proposals: { open: number; trashed: number; promoted: number };
  discovery: PlaybookConfig['discovery'];
}

// ------------------------------------------------------------------ chat types
export interface TraceStep {
  step: string;
  label: string;
  status: 'ok' | 'hit' | 'miss' | 'skip' | 'error' | 'tool';
  ms: number;
  detail: Record<string, unknown>;
}

export interface HelpArticleSource {
  id: string;
  title: string;
  category: string;
  content: string;
  similarity?: number;
}

export interface TokenUsage {
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
}

export type AnsweredBy = 'faq' | 'skill' | 'mixed' | 'rag' | 'blocked' | 'confirmation';

export interface SkillRun {
  name: string;
  entryId: string;
  version: number;
  score?: number;
  resourcesFetched: string[];
  toolCalls: { tool: string; args: Record<string, string> }[];
  runner: 'llm' | 'offline';
}

export interface ChatResponse {
  answer: string;
  answered_by: AnsweredBy;
  sources: HelpArticleSource[];
  response_time_ms: number;
  token_usage?: TokenUsage | null;
  blocked: boolean;
  trace: TraceStep[];
  faq_hits: { entryKey?: string; entryId: string; version: number; score?: number; question: string }[];
  skill_runs: SkillRun[];
  discovery?: {
    faqs: { entryKey?: string; version: number; score?: number; question: string }[];
    skills: { entryKey?: string; version: number; score?: number; name: string }[];
    truncated: boolean;
    budgetTruncated: boolean;
    estimatedPreviewTokens?: number | null;
    maxPreviewTokens?: number | null;
  } | null;
  proposal?: { proposed: boolean; proposalId?: string; clusterSize: number; needed?: number; question?: string } | null;
  session_id?: string | null;
  resolution?: ConversationResolutionResult | null;
  llm_mode: 'llm' | 'offline';
}

export interface ChatMessage {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  timestamp: string;
  response?: ChatResponse;
}

export interface HelpStats {
  articles: { index_name: string; num_articles: number; index_status: string };
  playbook: PlaybookStats;
  sessions: { total: number; pending_rag: number; pending_resolved_cases?: number; min_for_proposal: number };
  llm_mode: 'llm' | 'offline';
  playbook_mode: 'embedded' | 'remote';
}

export interface Scenario {
  title: string;
  steps: string[];
}

export interface SessionRecord {
  session_id: string;
  question: string;
  answer: string;
  answered_by: string;
  proposal_id?: string;
  created_at: string;
}

export interface SkillProposalWorkerResult {
  status: 'waiting' | 'proposed' | 'failed';
  proposed: boolean;
  proposalId?: string;
  name?: string;
  clusterSize?: number;
  needed?: number;
  evidenceAdded?: number;
  generator?: string;
  detail?: string;
  type?: string;
}

export interface ConversationResolutionResult {
  session: SessionRecord;
  worker: SkillProposalWorkerResult;
}

export interface Account {
  accountId: string;
  name: string;
  plan: string;
  paymentMethod: string;
  region: string;
  refund?: Record<string, string> | null;
  stream?: Record<string, string> | null;
}

// ------------------------------------------------------------------- helpers
export class ApiError extends Error {
  status: number;
  type?: string;
  body?: Record<string, unknown>;
  constructor(status: number, message: string, body?: Record<string, unknown>) {
    super(message);
    this.status = status;
    this.body = body;
    this.type = typeof body?.type === 'string' ? (body.type as string) : undefined;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    headers: { 'Content-Type': 'application/json', ...(init?.headers || {}) },
    ...init,
  });
  if (!response.ok) {
    let body: Record<string, unknown> | undefined;
    try {
      body = await response.json();
    } catch {
      /* no body */
    }
    const detail = (body?.detail as string) || (body?.title as string) || `API error ${response.status}`;
    throw new ApiError(response.status, detail, body);
  }
  if (response.status === 204) return undefined as T;
  const text = await response.text();
  return (text ? JSON.parse(text) : undefined) as T;
}

const post = <T>(path: string, body?: unknown) => request<T>(path, { method: 'POST', body: body === undefined ? undefined : JSON.stringify(body) });
const put = <T>(path: string, body?: unknown) => request<T>(path, { method: 'PUT', body: JSON.stringify(body ?? {}) });
const patch = <T>(path: string, body?: unknown) => request<T>(path, { method: 'PATCH', body: JSON.stringify(body ?? {}) });
const del = <T>(path: string) => request<T>(path, { method: 'DELETE' });

// --------------------------------------------------------------------- chat
export const helpChat = (message: string, options: {
  discovery_limit?: number | null;
  pins?: string[];
  user_id?: string;
  memory_session_id?: string;
} = {}) => post<ChatResponse>('/help/chat', {
  message,
  discovery_limit: options.discovery_limit ?? null,
  pins: options.pins,
  user_id: options.user_id,
  memory_session_id: options.memory_session_id,
});
export const helpStats = () => request<HelpStats>('/help/stats');
export const helpSuggestions = () => request<{ suggestions: string[]; scenarios: Scenario[] }>('/help/suggestions');
export const helpIngest = () => post<{ status: string; count: number }>('/help/ingest');
export const listSessions = (limit = 50) => request<{ sessions: SessionRecord[] }>(`/help/sessions?limit=${limit}`);
export const clearSessions = () => post<{ deleted: number }>('/help/sessions/clear');
export const extractProposals = () => post<{ proposals: { proposalId: string; question: string; clusterSize: number }[]; pendingSessions: number; minSessions: number }>('/help/extract');
export const recordResolvedCase = (issue: string, resolution: string) =>
  post<SessionRecord>('/help/resolved-cases', { issue, resolution });
export const resolveConversation = (sessionId: string, resolution: string) =>
  post<ConversationResolutionResult>(`/help/sessions/${sessionId}/resolution`, { resolution });
export const extractSkillProposals = () => post<{ proposals: { proposalId: string; name: string; clusterSize: number }[]; pendingSessions: number; minSessions: number }>('/help/extract-skills');
export const listAccounts = () => request<{ accounts: Account[] }>('/accounts');

// ----------------------------------------------------------------- playbook
export const playbookStats = () => request<PlaybookStats>('/playbook/stats');
export const playbookConfig = () => request<PlaybookConfig>('/playbook/config');
export const patchPlaybookConfig = (body: { discovery?: Record<string, unknown>; proposalPolicy?: Record<string, unknown> }) =>
  patch<PlaybookConfig>('/playbook/config', body);
export const seedPlaybook = (reset = false) => post<Record<string, unknown>>(`/playbook/seed?reset=${reset}`);
export const clearPlaybook = (scope: 'all' | 'entries' | 'proposals' = 'all') => post<{ deletedEntriesCount: number }>(`/playbook/clear?scope=${scope}`);

export const listFaqs = () => request<{ faqs: StableEntry[] }>('/playbook/faqs');
export const createFaq = (body: { question: string; answer: string; entryKey?: string; intents?: string[]; attributes?: Record<string, unknown> }) =>
  post<{ entry: StableEntry; version: Version }>('/playbook/faqs', body);
export const listFaqVersions = (entryId: string) => request<{ versions: Version[] }>(`/playbook/faqs/${entryId}/versions`);
export const appendFaqVersion = (entryId: string, body: { question: string; answer: string; intents?: string[]; attributes?: Record<string, unknown> }) =>
  post<{ entry: StableEntry; version: Version }>(`/playbook/faqs/${entryId}/versions`, body);
export const publishFaq = (entryId: string, version: number) => put<StableEntry>(`/playbook/faqs/${entryId}/publication`, { version });
export const withdrawFaq = (entryId: string) => del<StableEntry>(`/playbook/faqs/${entryId}/publication`);
export const deleteFaq = (entryId: string) => del<void>(`/playbook/faqs/${entryId}`);

export const listSkills = () => request<{ skills: StableEntry[] }>('/playbook/skills');
export const listSkillVersions = (entryId: string) => request<{ versions: Version[] }>(`/playbook/skills/${entryId}/versions`);
export const getSkillVersion = (entryId: string, version: number) => request<Version>(`/playbook/skills/${entryId}/versions/${version}`);
export const publishSkill = (entryId: string, version: number) => put<StableEntry>(`/playbook/skills/${entryId}/publication`, { version });
export const withdrawSkill = (entryId: string) => del<StableEntry>(`/playbook/skills/${entryId}/publication`);
export const fetchResource = (uri: string) => post<{ uri: string; mimeType: string; digest: string; sizeBytes: number; path: string; text?: string }>('/playbook/resource', { uri });

export const discover = (body: { query: string; limit?: number; pinnedEntries?: { entryKey?: string; entryId?: string }[] }) =>
  post<DiscoverResult>('/playbook/discover', body);

export const listProposals = (view: 'inbox' | 'trash' = 'inbox') => request<{ proposals: ProposalPreview[]; total?: number }>(`/playbook/proposals?view=${view}`);
export const getProposal = (proposalId: string) => request<Proposal>(`/playbook/proposals/${proposalId}`);
export const replaceProposal = (proposalId: string, body: { expectedProposalRevision: number | string; question: string; answer: string; entryKey?: string; intents?: string[] }) =>
  patch<Proposal>(`/playbook/proposals/${proposalId}`, body);
export const dismissProposal = (proposalId: string) => put<Proposal>(`/playbook/proposals/${proposalId}/dismissal`, {});
export const restoreProposal = (proposalId: string) => del<Proposal>(`/playbook/proposals/${proposalId}/dismissal`);
export const purgeProposal = (proposalId: string) => del<void>(`/playbook/proposals/${proposalId}`);
export const acceptProposal = (proposalId: string, expectedProposalRevision: number | string) =>
  post<{ entry: StableEntry; version: Version }>(`/playbook/proposals/${proposalId}/acceptance`, { expectedProposalRevision });
