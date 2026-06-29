import { useState, useRef, useEffect, useMemo } from 'react';
import { cn } from './utils';
import { Header } from './components/Header';
import { LeftSidebar } from './components/LeftSidebar';
import { RightSidebar } from './components/RightSidebar';
import { Footer } from './components/Footer';
import { parseUserCommand } from './api/commandParser.js';
import { answerCheckpoint, checkpointEventFromJob, createRun, createSession, createSessionRun, deleteSession, extractCheckpointSuggestions, fetchJob, listDatasets, listSessions, listTools, replayRunEvents, saveSessionSnapshot, setSessionMode, shouldUseRunStatusPollingFallback, subscribeRunEvents, updateSession } from './api/dataelfApi.js';
import { buildRunCompletionResultMessage, buildRunFailureResultMessage, executionStepFromBackendLog, formatBackendLog, logsFromRunCompletion, mergeRunLogLines, normalizeRunResultData } from './api/runDisplay.js';
import { buildModeCommandPromptMessage } from './api/sessionDisplay.js';
import { applyPilotEventToAttempts, buildPilotAttemptsFromBackendAttempts, buildPilotAttemptClarificationMessage, buildPilotCompletionResultMessage, buildPilotLifecycleMessage, buildPilotSummaryMessage, buildRejectedPilotToolCandidateMessage, parsePilotAttemptCount, pilotCheckpointAnswerPayload, pilotLifecycleMessageId } from './api/pilotDisplay.js';
import { upsertJobMessages } from './api/jobMessages.js';

interface ExecutionStep {
  id: string;
  name: string;
  status: 'pending' | 'running' | 'success' | 'error';
  log: string;
}

interface CatalogDataset {
  name: string;
  rows?: string | number;
  nesting?: string | number;
  size?: string;
}

interface CatalogTool {
  name: string;
  description?: string;
  parameters?: {
    properties?: Record<string, unknown>;
  };
}

interface Attempt {
  id: string;
  action_type: string;
  score: number;
  status: 'success' | 'failed' | 'running';
  validation: 'passed' | 'failed' | 'pending';
  produced_candidate: boolean;
  candidateApproval?: 'NA' | 'NEEDED' | 'APPROVED' | 'DISAPPROVED';
  // Detailed execution info
  attemptNumber?: number;
  totalAttempts?: number;
  planningLog?: string;
  pipelineLog?: string;
  dslCode?: string;
  model?: string;
  elapsed?: number;
  derivedToolName?: string;
  derivedToolType?: string;
  // Result data
  result?: {
    goal_satisfied: boolean;
    score: number;
    failure_type: string;
    recommended_next_action: string;
    capability_gap: any;
    reason: string;
  };
}

interface RunResultData {
  score: number;
  flaggedSamples: number;
  approvedAssets: number;
  allFailed?: boolean;
  rawResult?: any;
  artifacts?: any;
  metadata?: any;
  clarification?: any;
  capabilityGap?: any;
  logs?: any[];
  error?: string | null;
  jobId?: string | null;
}

interface Message {
  id: string;
  type: 'user' | 'system' | 'clarification' | 'pipeline' | 'execution' | 'lifecycle' | 'result' | 'pipeline_candidate' | 'tool_candidate' | 'pilot_summary';
  content: string;
  timestamp: string;
  // Clarification fields
  question?: string;
  userReply?: string;
  resolvedText?: string;
  status?: 'pending' | 'resolved';
  index?: number;
  total?: number;
  suggestions?: string[]; // Suggested options
  jobId?: string;
  checkpointId?: string;
  checkpointType?: string;
  pendingCommand?: string;
  // Execution fields
  executionSteps?: ExecutionStep[];
  // Lifecycle fields
  attempts?: Attempt[];
  resultData?: RunResultData;
  // Pipeline Candidate
  pipelineData?: {
    id: string;
    tools: string[];
    status: 'pending' | 'stable';
    score: number;
  };
  // Tool Candidate
  toolData?: {
    id: string;
    name: string;
    description: string;
    status: 'pending' | 'approved' | 'rejected';
    score: number;
  };
  // Pilot Summary
  summaryData?: {
    jobId: string;
    pilotStatus: string;
    totalAttempts: number;
    bestAttemptId: string;
    judgeScore: number;
    securityScore: number;
    derivedCandidates: number;
    approvedAssets: number;
    attemptSummaries: {
      id: string;
      action: string;
      success: boolean;
      judgeScore: number;
      securityScore: number | null;
      latency: number;
      tools: number;
      derived: number;
      nextStep: string;
      failureType: string;
      importantLog?: string;
    }[];
  };
}

export interface Session {
  id: string;
  name: string;
  date: string;
  active: boolean;
  messages?: Message[];
  logs?: string[];
  attempts?: Attempt[];
  bestScore?: number | 'NA';
  mode?: string;
  pipelineTools?: string[];
  candidateJson?: string | null;
  runResultData?: RunResultData | null;
  jobId?: string | null;
  status?: string;
  locked?: boolean;
  backendMode?: string | null;
}

function timestampNow() {
  return new Date().toLocaleTimeString('en-US', { hour12: false });
}

function createModeSelectionMessage(sessionId: string): Message {
  return {
    id: `${sessionId}-mode-selection`,
    type: 'clarification',
    content: '',
    question: '请选择这个 session 的执行模式：run 或 pilot。',
    status: 'pending',
    suggestions: ['run', 'pilot'],
    index: 1,
    total: 1,
    checkpointType: 'mode_selection',
    timestamp: timestampNow()
  };
}

function sessionFromBackend(record: any, active: boolean): Session {
  const snapshot = record?.snapshot || {};
  const mode = record?.mode ? String(record.mode).toUpperCase() : (snapshot.mode || 'NA');
  const createdAt = String(record?.created_at || new Date().toISOString());
  return {
    id: record.session_id,
    name: record.name || 'Untitled',
    date: createdAt.slice(0, 10),
    active,
    messages: snapshot.messages || [],
    logs: snapshot.logs || [],
    attempts: snapshot.attempts || [],
    bestScore: snapshot.bestScore ?? 'NA',
    mode,
    pipelineTools: snapshot.pipelineTools || [],
    candidateJson: snapshot.candidateJson ?? null,
    runResultData: snapshot.runResultData ?? null,
    jobId: record.job_id || null,
    status: record.status || 'new',
    locked: Boolean(record.locked),
    backendMode: record.backend_mode || null,
  };
}

export default function App() {
  const [sessions, setSessions] = useState<Session[]>([]);
  const activeSession = sessions.find(s => s.active) || sessions[0] || null;

  const handleNewSession = async () => {
    await createAndActivateSession();
  };

  const handleSelectSession = (id: string) => {
    activateSession(id);
  };

  const handleDeleteSession = (id: string) => {
    removeSession(id);
  };

  const extractTaskName = (cmd: string) => {
    // Attempt to extract string in quotes
    const quoteMatch = cmd.match(/"([^"]+)"|'([^']+)'/);
    if (quoteMatch) {
      return quoteMatch[1] || quoteMatch[2];
    }
    // Basic fallback: strip elf command and take first 15 chars
    const noPrefix = cmd.replace(/^elf\s+(run|pilot|submit)\s+/i, '').trim();
    return noPrefix.slice(0, 15).replace(/\s+/g, '_');
  };

  const [mode, setMode] = useState('NA');
  const [input, setInput] = useState('');
  const [messages, setMessages] = useState<Message[]>([]);
  const [activeTool, setActiveTool] = useState<string | null>(null);
  const [replyingTo, setReplyingTo] = useState<string | null>(null);
  const [pipelineTools, setPipelineTools] = useState<string[]>([]);
  const [logs, setLogs] = useState<string[]>([]);
  const [attempts, setAttempts] = useState<Attempt[]>([]);
  const [bestScore, setBestScore] = useState<number | 'NA'>('NA');
  const [runResultData, setRunResultData] = useState<RunResultData | null>(null);
  const candidateTools = useMemo(() => {
    const tools: {id: string, name: string, status: 'pending'|'stable', sessionId: string, messageId: string}[] = [];
    sessions.forEach(s => {
      if (s.messages) {
        s.messages.forEach(m => {
          if (m.type === 'tool_candidate' && m.toolData) {
            tools.push({
              id: m.toolData.id,
              name: m.toolData.name,
              status: m.toolData.status === 'pending' ? 'pending' : 'stable',
              sessionId: s.id,
              messageId: m.id
            });
          }
        });
      }
    });
    return tools;
  }, [sessions]);
  const [candidateJson, setCandidateJson] = useState<string | null>(null);
  const [headerStatus, setHeaderStatus] = useState<'IDLE' | 'PROCESSING' | 'PENDING' | 'STABLE'>('IDLE');
  const [catalogDatasets, setCatalogDatasets] = useState<CatalogDataset[]>([]);
  const [catalogTools, setCatalogTools] = useState<CatalogTool[]>([]);
  const [executingSessionId, setExecutingSessionId] = useState<string | null>(null); // Track which session is currently executing
  const [expandedAttempts, setExpandedAttempts] = useState<Set<string>>(new Set()); // Track which attempt DSL codes are expanded
  const [footerHeight, setFooterHeight] = useState(200); // Footer height in pixels
  const [isDragging, setIsDragging] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const resumeRef = useRef<((approved: boolean) => void) | null>(null);
  const runStreamsRef = useRef<Record<string, EventSource>>({});
  const runPollersRef = useRef<Record<string, number>>({});
  const runSessionByJobRef = useRef<Record<string, string>>({});
  const runLastEventIdRef = useRef<Record<string, number>>({});
  const runSeenEventIdsRef = useRef<Record<string, Set<number>>>({});
  const pilotBudgetByJobRef = useRef<Record<string, number>>({});
  const sessionSaveTimerRef = useRef<number | null>(null);

  const sessionNeedsMode = (session: Session | null) => {
    return !session?.mode || session.mode === 'NA';
  };

  const messagesWithModePrompt = (session: Session) => {
    const existingMessages = session.messages || [];
    if (!sessionNeedsMode(session)) {
      return existingMessages;
    }
    if (existingMessages.some(message => message.checkpointType === 'mode_selection')) {
      return existingMessages;
    }
    return [createModeSelectionMessage(session.id), ...existingMessages];
  };

  const applySessionState = (session: Session) => {
    const sessionMessages = session.jobId
      ? upsertJobMessages(messagesWithModePrompt(session), session.jobId, []) as Message[]
      : messagesWithModePrompt(session);
    const pendingClarification = sessionMessages.find(
      message => message.type === 'clarification' && message.status === 'pending'
    );
    setMessages(sessionMessages);
    setLogs(session.logs || []);
    setBestScore(session.bestScore ?? 'NA');
    setMode(session.mode || 'NA');
    setPipelineTools(session.pipelineTools || []);
    setAttempts(session.attempts || []);
    setReplyingTo(session.locked ? null : (pendingClarification?.id || null));
    setActiveTool(null);
    setCandidateJson(session.candidateJson || null);
    setRunResultData(session.runResultData || null);
    if (session.locked) {
      setHeaderStatus('STABLE');
    } else if (pendingClarification) {
      setHeaderStatus('PENDING');
    } else if (session.status === 'running') {
      setHeaderStatus('PROCESSING');
      setExecutingSessionId(session.id);
    } else {
      setHeaderStatus(sessionMessages.length > 0 ? 'STABLE' : 'IDLE');
    }
  };

  const buildCurrentSnapshot = () => ({
    messages,
    logs,
    attempts,
    bestScore,
    mode,
    pipelineTools,
    candidateJson,
    runResultData,
  });

  const persistActiveSnapshot = () => {
    if (!activeSession) return;
    saveSessionSnapshot(activeSession.id, buildCurrentSnapshot()).catch(() => {});
  };

  const createAndActivateSession = async () => {
    try {
      persistActiveSnapshot();
      const created = sessionFromBackend(await createSession({ name: 'Untitled' }), true);
      setSessions(prev => [
        created,
        ...prev.map(session => ({ ...session, active: false })),
      ]);
      applySessionState(created);
    } catch (error: any) {
      appendSystemMessage(`Failed to create backend session: ${String(error?.message || error)}`);
    }
  };

  const activateSession = (id: string) => {
    if (id === activeSession?.id) return;
    persistActiveSnapshot();
    const selected = sessions.find(session => session.id === id);
    if (!selected) return;
    const nextSelected = { ...selected, active: true };
    setSessions(prev => prev.map(session => (
      session.id === id
        ? nextSelected
        : session.id === activeSession?.id
          ? {
            ...session,
            messages,
            logs,
            attempts,
            bestScore,
            mode,
            pipelineTools,
            candidateJson,
            runResultData,
            active: false,
          }
          : { ...session, active: false }
    )));
    applySessionState(nextSelected);
  };

  const removeSession = async (id: string) => {
    try {
      await deleteSession(id);
    } catch (error: any) {
      appendSystemMessage(`Failed to delete backend session: ${String(error?.message || error)}`);
      return;
    }

    const remaining = sessions.filter(session => session.id !== id);
    if (remaining.length === 0) {
      setSessions([]);
      await createAndActivateSession();
      return;
    }

    const shouldMoveActive = activeSession?.id === id || !remaining.some(session => session.active);
    const nextSessions = remaining.map((session, index) => ({
      ...session,
      active: shouldMoveActive ? index === 0 : session.active,
    }));
    const selected = nextSessions.find(session => session.active) || nextSessions[0];
    setSessions(nextSessions);
    applySessionState(selected);
  };

  useEffect(() => {
    let cancelled = false;

    const loadCatalog = async () => {
      const [datasetsResult, toolsResult] = await Promise.allSettled([
        listDatasets(),
        listTools(),
      ]);

      if (cancelled) return;

      if (datasetsResult.status === 'fulfilled') {
        setCatalogDatasets(datasetsResult.value);
      }
      if (toolsResult.status === 'fulfilled') {
        setCatalogTools(toolsResult.value);
      }
    };

    loadCatalog();

    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    let cancelled = false;

    const loadSessions = async () => {
      try {
        let backendSessions = await listSessions();
        if (backendSessions.length === 0) {
          backendSessions = [await createSession({ name: 'Untitled' })];
        }
        if (cancelled) return;
        const nextSessions = backendSessions.map((session: any, index: number) => (
          sessionFromBackend(session, index === 0)
        ));
        setSessions(nextSessions);
        if (nextSessions[0]) {
          applySessionState(nextSessions[0]);
        }
      } catch (error: any) {
        if (!cancelled) {
          appendSystemMessage(`Failed to load backend sessions: ${String(error?.message || error)}`);
        }
      }
    };

    loadSessions();

    return () => {
      cancelled = true;
    };
  }, []);

  // Auto-save session state
  useEffect(() => {
    if (activeSession) {
      setSessions(prev => {
        const current = prev.find(s => s.id === activeSession.id);
        if (!current) return prev;
        
        // Only update if something actually changed to prevent infinite loops
        if (
          current.messages === messages &&
          current.logs === logs &&
          current.attempts === attempts &&
          current.bestScore === bestScore &&
          current.mode === mode &&
          current.pipelineTools === pipelineTools &&
          current.candidateJson === candidateJson &&
          current.runResultData === runResultData
        ) {
          return prev;
        }

        return prev.map(s => 
          s.id === activeSession.id 
            ? { ...s, messages, logs, attempts, bestScore, mode, pipelineTools, candidateJson, runResultData } 
            : s
        );
      });

      if (sessionSaveTimerRef.current !== null) {
        window.clearTimeout(sessionSaveTimerRef.current);
      }
      sessionSaveTimerRef.current = window.setTimeout(() => {
        saveSessionSnapshot(activeSession.id, buildCurrentSnapshot()).catch(() => {});
        sessionSaveTimerRef.current = null;
      }, 500);
    }
  }, [messages, logs, attempts, bestScore, mode, pipelineTools, candidateJson, runResultData, activeSession?.id]);

  const [pendingApproval, setPendingApproval] = useState<{msgId: string, attId: string, mockAttempts: Attempt[], currentAtt: number} | null>(null);

  const finishLifecycle = (allAttempts: Attempt[], approvedAttId?: string | null) => {
    const bestSc = Math.max(...allAttempts.map(a => a.score));
    const allFailed = allAttempts.every(a => a.status === 'failed' || a.validation === 'failed' || a.candidateApproval === 'DISAPPROVED');

    if (allFailed) {
      setMessages(prev => [
        ...prev,
        {
          id: Date.now().toString() + "-res",
          type: 'result',
          content: 'PILOT Execution Completed',
          timestamp: new Date().toLocaleTimeString('en-US', { hour12: false }),
          resultData: {
            score: bestSc,
            flaggedSamples: 0,
            approvedAssets: 0,
            allFailed: true
          }
        }
      ]);
      setHeaderStatus('STABLE');
      setExecutingSessionId(null);
    } else if (approvedAttId) {
      // User already approved an attempt, directly finish with result
      setMessages(prev => [
        ...prev,
        {
          id: Date.now().toString() + "-res",
          type: 'result',
          content: 'PILOT Execution Completed & Candidate Approved',
          timestamp: new Date().toLocaleTimeString('en-US', { hour12: false }),
          resultData: {
            score: bestSc,
            flaggedSamples: 0,
            approvedAssets: 1,
            allFailed: false
          }
        }
      ]);
      setHeaderStatus('STABLE');
      setExecutingSessionId(null);
    } else {
      // Randomly choose between tool or pipeline candidate
      const generateTool = Math.random() < 0.5;

      if (generateTool) {
        // Generate candidate tool
        const toolId = `cand-tool-${Math.floor(Math.random() * 10000).toString().padStart(4, '0')}`;
        setMessages(prev => [
          ...prev,
          {
            id: Date.now().toString() + "-tool-cand",
            type: 'tool_candidate',
            content: 'Candidate Tool Generated',
            timestamp: new Date().toLocaleTimeString('en-US', { hour12: false }),
            toolData: {
              id: toolId,
              name: `CustomTool_${toolId.slice(-4)}`,
              description: 'Auto-generated tool for data processing and validation',
              status: 'pending',
              score: bestSc
            }
          }
        ]);
      } else {
        // Generate candidate pipeline
        const pipelineId = `cand-pipe-${Math.floor(Math.random() * 10000).toString().padStart(4, '0')}`;
        setMessages(prev => [
          ...prev,
          {
            id: Date.now().toString() + "-pipe-cand",
            type: 'pipeline_candidate',
            content: 'Candidate Pipeline Generated',
            timestamp: new Date().toLocaleTimeString('en-US', { hour12: false }),
            pipelineData: {
              id: pipelineId,
              tools: approvedAttId ? [approvedAttId] : [],
              status: 'pending',
              score: bestSc
            }
          }
        ]);
      }
      setHeaderStatus('PENDING');
    }
  };

  const handleApproveCandidate = (msgId: string, attId: string, approved: boolean) => {
    setHeaderStatus(approved ? 'STABLE' : 'PROCESSING');
    
    // Find the attempt
    let foundAtt: Attempt | null = null;
    let foundAttIdx = 0;
    let mockAttempts: Attempt[] = [];
    
    setMessages(prev => {
      const msgs = [...prev];
      const idx = msgs.findIndex(m => m.id === msgId);
      if (idx !== -1 && msgs[idx].attempts) {
        mockAttempts = msgs[idx].attempts!;
        foundAttIdx = mockAttempts.findIndex(a => a.id === attId);
        if (foundAttIdx !== -1) {
          foundAtt = mockAttempts[foundAttIdx];
          msgs[idx] = {
            ...msgs[idx],
            attempts: mockAttempts.map(a => a.id === attId ? { ...a, candidateApproval: approved ? 'APPROVED' : 'DISAPPROVED' } : a)
          };
        }
      }
      return msgs;
    });

    if (approved && foundAtt) {
      const att = foundAtt as Attempt;
      const toolJson = {
        id: attId,
        type: 'tool',
        name: `Candidate_Tool_${attId}`,
        score: att.score,
        status: 'approved',
        timestamp: new Date().toISOString()
      };
      setCandidateJson(JSON.stringify(toolJson, null, 2));
    }

    setPendingApproval(null);
    setTimeout(() => {
      if (resumeRef.current) {
        resumeRef.current(approved);
        resumeRef.current = null;
      } else if (mockAttempts.length > 0) {
        // If it's from a loaded session without active simulation, just finish it here
        finishLifecycle(mockAttempts.slice(0, foundAttIdx + 1), approved ? attId : null);
      }
    }, 500);
  };

  const runLifecycleSimulation = (attemptsCount: number = 3) => {
    const lifecycleMsgId = Date.now().toString() + "-life";

    // Initialize logs for lifecycle
    setLogs([
      `[${new Date().toLocaleTimeString('en-US', { hour12: false })}] PILOT Lifecycle Started`,
      `Total attempts to run: ${attemptsCount}`,
      ''
    ]);

    setMessages(prev => [
      ...prev,
      {
        id: lifecycleMsgId,
        type: 'lifecycle',
        content: '',
        timestamp: new Date().toLocaleTimeString('en-US', { hour12: false }),
        attempts: []
      }
    ]);

    const mockAttempts: Attempt[] = Array.from({ length: attemptsCount }).map((_, idx) => {
      const isLast = idx === attemptsCount - 1;
      const attemptNum = idx + 1;
      const attemptId = `attempt_${String(attemptNum).padStart(2, '0')}`;

      // Generate realistic action types and logs
      let actionType = '';
      let planningLog = '';
      let pipelineLog = '';
      let dslCode = '';
      let derivedToolName = '';

      if (attemptNum === 1) {
        actionType = 'propose_pipeline';
        planningLog = `${attemptId} Planner: Running model=claude-sonnet-4-6\n✅ ${attemptId} Planner: status=success · model=claude-sonnet-4-6 · action=propose_pipeline`;
        pipelineLog = `${attemptId} Pipeline: Running model=claude-sonnet-4-6\n✅ ${attemptId} Pipeline: status=success · model=claude-sonnet-4-6 elapsed=41.82s`;
        dslCode = `log_step("Loading alpaca_data dataset")

data = load_dataset("alpaca_data")

log_step(f"Loaded {len(data)} records from alpaca_data")

log_step("Running semantic-heavy security audit focused on privacy leakage, jailbreak risk, and harmful response risk")

audit_results = run_tool(
    "security_audit",
    data=data,
    checker_names=[
        "PIILLMJudge",
        "HarmfulContentLLMJudge",
        "ToxicityLLMJudge",
        "AlignmentRefusalBypassRule"
    ],
    max_workers=6
)

log_step("Completed tool: security_audit")

log_step("Saving audit results")

save_result(audit_results)

log_step("Audit results saved")`;
      } else if (attemptNum === 2) {
        actionType = 'mutate_pipeline';
        planningLog = `${attemptId} Planner: Running model=claude-sonnet-4-6\n✅ ${attemptId} Planner: status=success · model=claude-sonnet-4-6 · action=mutate_pipeline`;
        pipelineLog = `${attemptId} Pipeline: Running model=claude-sonnet-4-6\n✅ ${attemptId} Pipeline: status=success · model=claude-sonnet-4-6 elapsed=27.64s`;
        dslCode = `log_step("Loading alpaca_data dataset")

data = load_dataset("alpaca_data")

log_step(f"Loaded {len(data)} records from alpaca_data")

log_step("Running broad rule-based baseline coverage for privacy, secrets, toxicity, harmfulness, and jailbreak proxies")

baseline_audit = run_tool(
    "security_audit",
    data=data,
    checker_names=[
        "PIIRule",
        "SecretRule",
        "ToxicityKeywordRule",
        "HarmfulKeywordRule",
        "AlignmentRefusalBypassRule"
    ],
    max_workers=6
)

log_step("Completed tool: security_audit")

log_step("Ranking flagged samples using baseline evidence counts")

top_risky_samples = sorted(
    baseline_audit.get("sample_results", []),
    key=lambda x: x.get("risk_score", 0),
    reverse=True
)[:30]

result_package = {
    "audit_summary": baseline_audit.get("result", baseline_audit),
    "top_risky_samples": top_risky_samples,
    "review_note": "Baseline rule coverage completed; semantic evidence is still limited."
}

log_step("Saving audit package")

save_result(result_package)

log_step("Audit package saved")`;
      } else if (attemptNum === 3) {
        actionType = 'derive_python_tool_draft';
        derivedToolName = 'policy_safe_semantic_risk_audit';
        planningLog = `${attemptId} Planner: Running model=claude-sonnet-4-6\n✅ ${attemptId} Planner: status=success · model=claude-sonnet-4-6 · action=derive_python_tool_draft`;
        pipelineLog = `${attemptId} Derived Tool: Running model=claude-sonnet-4-6\n✅ ${attemptId} Derived Tool: status=success · type=experimental_python_tool · name=policy_safe_semantic_risk_audit\n✅ ${attemptId} Derived Tool Validation: smoke_passed · summary=Redacts sensitive spans, chunks ambiguous samples, and performs policy-safe semantic risk triage.

${attemptId} Pipeline: Running model=claude-sonnet-4-6\n✅ ${attemptId} Pipeline: status=success · model=claude-sonnet-4-6 elapsed=34.11s`;
        dslCode = `log_step("Loading alpaca_data dataset")

data = load_dataset("alpaca_data")

log_step(f"Loaded {len(data)} records from alpaca_data")

log_step("Running baseline security coverage to collect broad safety evidence")

baseline_audit = run_tool(
    "security_audit",
    data=data,
    checker_names=[
        "PIIRule",
        "SecretRule",
        "ToxicityKeywordRule",
        "HarmfulKeywordRule",
        "AlignmentRefusalBypassRule"
    ],
    max_workers=6
)

log_step("Completed tool: security_audit")

log_step("Running derived policy-safe semantic triage on ambiguous and high-risk samples")

semantic_triage = run_tool(
    "policy_safe_semantic_risk_audit",
    data=data,
    baseline_results=baseline_audit,
    priority_dimensions=[
        "privacy_leakage",
        "jailbreak_risk",
        "harmful_response_risk",
        "annotation_drift"
    ],
    top_k=30,
    budget_mode="balanced"
)

log_step("Completed tool: policy_safe_semantic_risk_audit")

final_audit_package = {
    "baseline_audit": baseline_audit,
    "semantic_triage": semantic_triage,
    "top_risky_samples": semantic_triage.get("top_risky_samples", []),
    "executive_summary": semantic_triage.get("executive_summary", ""),
    "review_recommendations": semantic_triage.get("review_recommendations", [])
}

log_step("Saving final audit package")

save_result(final_audit_package)

log_step("Final audit package saved")`;
      }

      // Generate result data
      let result;
      if (attemptNum === 1) {
        result = {
          goal_satisfied: false,
          score: 0.18,
          failure_type: "llm_checker_content_filter",
          recommended_next_action: "mutate_pipeline",
          capability_gap: {
            type: "policy_blocked_semantic_check",
            avoid_checkers: ["HarmfulContentLLMJudge"],
            recommended_strategy: "switch to baseline coverage first, then derive a policy-safe semantic triage tool"
          },
          reason: "The direct semantic judge path was blocked by content filtering and no review-ready output package was produced."
        };
      } else if (attemptNum === 2) {
        result = {
          goal_satisfied: false,
          score: 0.63,
          failure_type: "insufficient_prioritized_evidence",
          recommended_next_action: "derive_python_tool_draft",
          capability_gap: {
            type: "missing_semantic_triage_tool",
            needed_capability: "policy-safe semantic ranking with executive summary generation",
            suggested_tool_shape: "redacted and chunked semantic triage wrapper over ambiguous flagged samples"
          },
          reason: "The baseline pipeline produced broad coverage and a shortlist, but it still lacks semantic prioritization quality and an executive-ready summary."
        };
      } else if (attemptNum === 3) {
        result = {
          goal_satisfied: true,
          score: 0.91,
          failure_type: "none",
          recommended_next_action: "stop_success",
          capability_gap: {},
          reason: "The final pipeline achieved broad safety coverage, produced a prioritized shortlist of risky samples, and generated an executive-ready review summary within the balanced cost envelope."
        };
      }

      return {
        id: attemptId,
        action_type: actionType,
        score: isLast ? 91 : (attemptNum === 1 ? 18 : 63),
        status: 'success',
        validation: isLast ? 'passed' : 'failed',
        produced_candidate: isLast,
        candidateApproval: isLast ? 'NEEDED' : 'NA',
        attemptNumber: attemptNum,
        totalAttempts: attemptsCount,
        planningLog,
        pipelineLog,
        dslCode,
        model: 'claude-sonnet-4-6',
        elapsed: attemptNum === 1 ? 41.82 : attemptNum === 2 ? 27.64 : 34.11,
        derivedToolName: derivedToolName || undefined,
        derivedToolType: derivedToolName ? 'experimental_python_tool' : undefined,
        result
      };
    });

    const generateAttemptLogs = (att: Attempt) => {
      const attemptNum = att.attemptNumber || 1;
      const timestamp = new Date().toLocaleTimeString('en-US', { hour12: false });

      if (attemptNum === 1) {
        return [
          `${att.id} Execution: Running pipeline`,
          `[step_1 · 0ms] Loading alpaca_data dataset`,
          `[step_2 · 2ms] Loading dataset: alpaca_data`,
          `[step_3 · 1ms] Loaded 512 records from alpaca_data`,
          `[step_4 · 0ms] Running semantic-heavy security audit focused on privacy leakage, jailbreak risk, and harmful response risk`,
          `[step_5 · 0ms] Running tool: security_audit`,
          `[step_6 · 1ms] SecurityAuditTool: 512 records, checkers=['PIILLMJudge', 'HarmfulContentLLMJudge', 'ToxicityLLMJudge', 'AlignmentRefusalBypassRule']`,
          `[step_7 · 1ms] Tool LLM model: gpt-4o-mini`,
          `[step_8 · 9ms] Loaded 4 checkers, 0 heuristic checkers`,
          `[step_9 · 0ms] Running 4 checkers on 512 samples ...`,
          `❓ [step_10 · 8124ms] HarmfulContentLLMJudge: statement extraction failed: LLM API error (status 400) · content_filter`,
          `❓ [step_11 · 7942ms] HarmfulContentLLMJudge: statement extraction failed: LLM API error (status 400) · content_filter`,
          `❓ [step_12 · 8021ms] HarmfulContentLLMJudge: statement extraction failed: LLM API error (status 400) · content_filter`,
          `❌ [step_13 · 24188ms] Pipeline execution failed: Tool 'security_audit' execution failed due to repeated content filtering in semantic judge path`,
          `🏁 [job_end · 3ms] Job completed`,
          ``,
          `❌ ${att.id} Execution: status=failed elapsed=24.19s`,
          `${att.id} Judge: Running model=claude-sonnet-4-6`,
          `✅ ${att.id} Judge: status=success · model=claude-sonnet-4-6 · goal=False`,
          ``,
          `${att.id} Judge Score: 0.18`,
          `${att.id} Output Metrics: shortlist=0 · exec_summary=none · coverage=2/4`,
          `${att.id} Next Action: mutate_pipeline · failure_type=llm_checker_content_filter`
        ];
      } else if (attemptNum === 2) {
        return [
          `${att.id} Execution: Running pipeline`,
          `[step_1 · 0ms] Loading alpaca_data dataset`,
          `[step_2 · 2ms] Loading dataset: alpaca_data`,
          `[step_3 · 1ms] Loaded 512 records from alpaca_data`,
          `[step_4 · 0ms] Running broad rule-based baseline coverage for privacy, secrets, toxicity, harmfulness, and jailbreak proxies`,
          `[step_5 · 0ms] Running tool: security_audit`,
          `[step_6 · 1ms] SecurityAuditTool: 512 records, checkers=['PIIRule', 'SecretRule', 'ToxicityKeywordRule', 'HarmfulKeywordRule', 'AlignmentRefusalBypassRule']`,
          `[step_7 · 1ms] Tool LLM model: gpt-4o-mini`,
          `[step_8 · 14ms] Loaded 5 checkers, 0 heuristic checkers`,
          `[step_9 · 0ms] Running 5 checkers on 512 samples ...`,
          `❓ [step_10 · 1926ms] Audit complete: 121/512 flagged (23.6%), score=57`,
          `✅ [step_11 · 1ms] Completed tool: security_audit`,
          `[step_12 · 0ms] Ranking flagged samples using baseline evidence counts`,
          `[step_13 · 1ms] Saving audit package`,
          `[step_14 · 0ms] Saving result to job`,
          `[step_15 · 0ms] Audit package saved`,
          `🏆 [job_end · 3ms] Job completed`,
          ``,
          `✅ ${att.id} Execution: status=success elapsed=1.95s`,
          `${att.id} Judge: Running model=claude-sonnet-4-6`,
          `✅ ${att.id} Judge: status=success · model=claude-sonnet-4-6 · goal=False`,
          ``,
          `${att.id} Judge Score: 0.63`,
          `${att.id} Security Metrics: score=57.0 · flagged_rate=0.236 · flagged_samples=121`,
          `${att.id} Output Metrics: shortlist=30 · exec_summary=draft · coverage=3/4`,
          `${att.id} Next Action: derive_python_tool_draft · failure_type=insufficient_prioritized_evidence`
        ];
      } else if (attemptNum === 3) {
        return [
          `${att.id} Execution: Running pipeline`,
          `[step_1 · 0ms] Loading alpaca_data dataset`,
          `[step_2 · 2ms] Loading dataset: alpaca_data`,
          `[step_3 · 1ms] Loaded 512 records from alpaca_data`,
          `[step_4 · 0ms] Running baseline security coverage to collect broad safety evidence`,
          `[step_5 · 0ms] Running tool: security_audit`,
          `[step_6 · 1ms] SecurityAuditTool: 512 records, checkers=['PIIRule', 'SecretRule', 'ToxicityKeywordRule', 'HarmfulKeywordRule', 'AlignmentRefusalBypassRule']`,
          `[step_7 · 1ms] Tool LLM model: gpt-4o-mini`,
          `[step_8 · 14ms] Loaded 5 checkers, 0 heuristic checkers`,
          `[step_9 · 0ms] Running 5 checkers on 512 samples ...`,
          `❓ [step_10 · 1882ms] Audit complete: 118/512 flagged (23.0%), score=62`,
          `✅ [step_11 · 1ms] Completed tool: security_audit`,
          `[step_12 · 0ms] Running derived policy-safe semantic triage on ambiguous and high-risk samples`,
          `[step_13 · 0ms] Running tool: policy_safe_semantic_risk_audit`,
          `[step_14 · 3ms] PolicySafeSemanticRiskAudit: selected 146 ambiguous-or-high-risk candidates from baseline results`,
          `[step_15 · 18ms] PolicySafeSemanticRiskAudit: redacted 82 sensitive spans and chunked candidates into 19 batches`,
          `[step_16 · 6421ms] PolicySafeSemanticRiskAudit: semantic ranking complete · shortlisted 30 samples`,
          `[step_17 · 2ms] PolicySafeSemanticRiskAudit: executive summary generated with 4 review recommendations`,
          `✅ [step_18 · 1ms] Completed tool: policy_safe_semantic_risk_audit`,
          `[step_19 · 0ms] Saving final audit package`,
          `[step_20 · 1ms] Saving result to job`,
          `[step_21 · 0ms] Final audit package saved`,
          `🏆 [job_end · 3ms] Job completed`,
          ``,
          `✅ ${att.id} Execution: status=success elapsed=8.35s`,
          `${att.id} Judge: Running model=claude-sonnet-4-6`,
          `✅ ${att.id} Judge: status=success · model=claude-sonnet-4-6 · goal=True`,
          ``,
          `${att.id} Judge Score: 0.91`,
          `${att.id} Security Metrics: score=62.0 · flagged_rate=0.230 · flagged_samples=118`,
          `${att.id} Output Metrics: shortlist=30 · exec_summary=ready · coverage=4/4`,
          `${att.id} Next Action: stop_success · failure_type=none`
        ];
      }
      return [];
    };

    const generatePilotSummary = (allAttempts: Attempt[]) => {
      const jobId = 'job_7ae92b11c4d0';
      const bestAttempt = allAttempts.reduce((best, curr) => curr.score > best.score ? curr : best, allAttempts[0]);

      const summaryData = {
        jobId,
        pilotStatus: 'success',
        totalAttempts: allAttempts.length,
        bestAttemptId: bestAttempt.id,
        judgeScore: bestAttempt.result?.score || 0,
        securityScore: 62.0,
        derivedCandidates: 2,
        approvedAssets: 1,
        attemptSummaries: allAttempts.map((att, idx) => ({
          id: att.id,
          action: att.action_type,
          success: att.result?.goal_satisfied || false,
          judgeScore: att.result?.score || 0,
          securityScore: idx === 0 ? null : (idx === 1 ? 57.0 : 62.0),
          latency: idx === 0 ? 66.42 : (idx === 1 ? 49.81 : 50.58),
          tools: idx === 2 ? 2 : 1,
          derived: idx === 2 ? 2 : 0,
          nextStep: att.result?.recommended_next_action || '',
          failureType: att.result?.failure_type || '',
          importantLog: idx === 0
            ? 'WARNING · step_10 · HarmfulContentLLMJudge content_filter repeated during semantic judge path'
            : idx === 1
              ? 'WARNING · step_10 · Audit complete: 121/512 flagged (23.6%), score=57'
              : 'INFO · step_16 · PolicySafeSemanticRiskAudit: semantic ranking complete · shortlisted 30 samples'
        }))
      };

      setMessages(prev => [
        ...prev,
        {
          id: Date.now().toString() + '-summary',
          type: 'pilot_summary',
          content: 'PILOT Execution Summary',
          timestamp: new Date().toLocaleTimeString('en-US', { hour12: false }),
          summaryData
        }
      ]);

      // Generate candidate tool card after summary
      setTimeout(() => {
        const toolId = 'cand_tool_job_7ae92b11';
        const candidateId = `cand_pipe_${jobId}`;

        // Extract dataset from resolved clarification messages
        const datasetMsg = messages.find(m =>
          m.type === 'clarification' &&
          m.status === 'resolved' &&
          (m.question?.includes('数据集') || m.question?.includes('dataset'))
        );
        const datasetsMatch = datasetMsg?.resolvedText?.match(/datasets = \[([^\]]+)\]/);
        const datasetsStr = datasetsMatch ? datasetsMatch[1] : 'security_audit_samples';
        const firstDataset = datasetsStr.split(',')[0].trim();

        // Get task from first user message
        const taskMsg = messages.find(m => m.type === 'user');
        const taskContent = taskMsg?.content || "Data processing task";

        // Generate candidate JSON
        const candidateJsonData = {
          candidate_id: candidateId,
          candidate_type: "pipeline",
          name: `pipeline_${jobId}`,
          description: `Pipeline candidate derived from pilot job ${jobId}.`,
          pipeline: `log_step("Loading ${firstDataset} dataset")\n\ndata = load_dataset("${firstDataset}")\n\nlog_step(f"Loaded {len(data)} total records from ${firstDataset}")\n\nlog_step("Filtering records where dataset_type == 'sft'")\n\nsft_data = [record for record in data if record.get("dataset_type") == "sft"]\n\nlog_step(f"Filtered {len(sft_data)} records with dataset_type == 'sft'")\n\nlog_step("Writing filtered sft records to test_data/${firstDataset}_sft.json")\n\nwrite_file(sft_data, "test_data/${firstDataset}_sft.json")\n\nlog_step(f"Successfully wrote {len(sft_data)} sft records to test_data/${firstDataset}_sft.json")\n\nlog_step("Saving final result summary")\n\nsave_result({"total_sft_count": len(sft_data), "output_file": "test_data/${firstDataset}_sft.json", "message": f"共找到 {len(sft_data)} 条 dataset_type 为 sft 的数据，已写入 test_data/${firstDataset}_sft.json"})\n\nlog_step("Pipeline completed successfully")`,
          source_attempts: allAttempts.map(a => a.id),
          status: "rejected",
          validation_criteria: [
            "Manual review required before submit.",
            "Judge score should be acceptable."
          ],
          tool_domains: [
            "security_audit_tools",
            "data_selection_tools",
            "data_scoring_tools"
          ],
          metadata: {
            job_id: jobId,
            task: taskContent,
            judge: {
              goal_satisfied: bestAttempt.result?.goal_satisfied || false,
              score: bestAttempt.result?.score || 0,
              failure_type: bestAttempt.result?.failure_type || null,
              capability_gap: bestAttempt.result?.capability_gap || null,
              recommended_next_action: bestAttempt.result?.recommended_next_action || "none",
              reason: bestAttempt.result?.reason || ""
            }
          },
          validation_status: "smoke_passed",
          validation_summary: "Pipeline candidate validated by successful execution in this attempt.",
          smoke_test_result: {
            status: "from_successful_attempt",
            attempt_id: bestAttempt.id
          },
          benchmark_result: {
            status: "not_configured"
          },
          rejection_reason: "Rejected during pilot checkpoint."
        };

        setCandidateJson(JSON.stringify(candidateJsonData, null, 2));

        setMessages(prev => [
          ...prev,
          {
            id: Date.now().toString() + "-tool-cand",
            type: 'tool_candidate',
            content: 'Candidate Tool Generated',
            timestamp: new Date().toLocaleTimeString('en-US', { hour12: false }),
            toolData: {
              id: toolId,
              name: 'policy_safe_semantic_risk_audit',
              description: 'Policy-safe semantic ranking with executive summary generation. Redacts sensitive spans, chunks ambiguous samples, and performs semantic risk triage.',
              status: 'pending',
              score: bestAttempt.score
            }
          }
        ]);
        setHeaderStatus('PENDING');
      }, 1500);
    };

    const addLogsLineByLine = (logLines: string[], callback: () => void) => {
      // Filter out any undefined/null values
      const validLogs = logLines.filter(line => line !== undefined && line !== null);
      let lineIdx = 0;
      const addNextLine = () => {
        if (lineIdx < validLogs.length) {
          setLogs(prev => [...prev, validLogs[lineIdx]]);
          lineIdx++;
          setTimeout(addNextLine, 80); // 80ms delay between lines
        } else {
          callback();
        }
      };
      addNextLine();
    };

    let currentAtt = 0;
    const step = () => {
      if (currentAtt < mockAttempts.length) {
        const att = mockAttempts[currentAtt];

        // Generate logs for this attempt
        const attemptLogs = generateAttemptLogs(att);
        const allLogs = [
          '',
          `[${new Date().toLocaleTimeString('en-US', { hour12: false })}] ========== ${att.id.toUpperCase()} ==========`,
          '',
          ...attemptLogs
        ];

        // Add logs line by line
        addLogsLineByLine(allLogs, () => {
          // After all logs are added, update attempts and messages
          setAttempts(prev => {
            const next = [...prev, att];
            const max = Math.max(...next.map(a => a.score));
            setBestScore(max);
            return next;
          });

          setMessages(prev => {
            const msgs = [...prev];
            const idx = msgs.findIndex(m => m.id === lifecycleMsgId);
            if (idx !== -1) {
              msgs[idx] = { ...msgs[idx], attempts: [...(msgs[idx].attempts || []), att] };
            }
            return msgs;
          });

          currentAtt++;
          // Longer delay between attempts to show thinking time
          const nextDelay = mockAttempts[currentAtt]?.derivedToolName ? 4500 : 3500;
          setTimeout(step, nextDelay);
        });
      } else {
        // All attempts completed, generate summary
        setHeaderStatus('STABLE');
        setTimeout(() => generatePilotSummary(mockAttempts), 1000);
      }
    };

    setHeaderStatus('PROCESSING');
    setTimeout(step, 2000);
  };

  const runExecutionSimulation = (tools: string[], currentMode: string, attemptsCount?: number) => {
    const execId = Date.now().toString() + "-exec";
    const initialSteps: ExecutionStep[] = tools.map((t, idx) => ({
      id: `step-${idx}`,
      name: t,
      status: 'pending',
      log: `Pending...`
    }));

    setMessages(prev => [
      ...prev,
      {
        id: execId,
        type: 'execution',
        content: '',
        timestamp: new Date().toLocaleTimeString('en-US', { hour12: false }),
        executionSteps: initialSteps
      }
    ]);
    
    setLogs([]);

    let currentStep = 0;
    const interval = setInterval(() => {
      setMessages(prev => {
        const msgs = [...prev];
        const execMsgIdx = msgs.findIndex(m => m.id === execId);
        if (execMsgIdx === -1 || !msgs[execMsgIdx].executionSteps) return msgs;

        const newSteps = [...msgs[execMsgIdx].executionSteps!];
        
        if (currentStep < newSteps.length) {
          const step = { ...newSteps[currentStep] };
          if (step.status === 'pending') {
            step.status = 'running';
            step.log = `Running ${step.name}...`;
            setLogs(l => [...l, `[${new Date().toLocaleTimeString('en-US', { hour12: false })}] [INFO] Executing step: ${step.name}`]);
            newSteps[currentStep] = step;
          } else if (step.status === 'running') {
            step.status = 'success';
            step.log = `Successfully finished ${step.name}`;
            setLogs(l => [...l, `[${new Date().toLocaleTimeString('en-US', { hour12: false })}] [SUCCESS] Completed step: ${step.name}`]);
            newSteps[currentStep] = step;
            currentStep++;
          }
          
          if (currentStep === newSteps.length) {
             setLogs(l => [...l, `[${new Date().toLocaleTimeString('en-US', { hour12: false })}] [DONE] Pipeline execution finished.`]);
             clearInterval(interval);
             
             if (currentMode === 'PILOT') {
               setTimeout(() => runLifecycleSimulation(attemptsCount), 1000);
             } else {
               setBestScore(98.5);
               setTimeout(() => {
                 setRunResultData({
                   score: 98.5,
                   flaggedSamples: 2,
                   approvedAssets: 0,
                   rawResult: {
                     security_score: 98.5,
                     flagged_samples: 2,
                   },
                   artifacts: {},
                   metadata: {},
                   clarification: {},
                   capabilityGap: {},
                   logs: [],
                   error: null,
                   jobId: null
                 });
                 setHeaderStatus('STABLE');
                 setExecutingSessionId(null);
               }, 1000);
             }
          }
        }
        
        msgs[execMsgIdx] = { ...msgs[execMsgIdx], executionSteps: newSteps };
        return msgs;
      });
    }, 1200);
  };


  const scrollToBottom = () => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  };

  useEffect(() => {
    scrollToBottom();
  }, [messages]);

  useEffect(() => {
    if (replyingTo && inputRef.current) {
      inputRef.current.focus();
    }
  }, [replyingTo]);

  // Auto-resize textarea based on content
  useEffect(() => {
    if (inputRef.current) {
      inputRef.current.style.height = 'auto';
      inputRef.current.style.height = `${inputRef.current.scrollHeight}px`;
    }
  }, [input]);

  useEffect(() => {
    return () => {
      Object.values(runStreamsRef.current).forEach(source => source.close());
      runStreamsRef.current = {};
      Object.values(runPollersRef.current).forEach(intervalId => window.clearInterval(intervalId));
      runPollersRef.current = {};
      if (sessionSaveTimerRef.current !== null) {
        window.clearTimeout(sessionSaveTimerRef.current);
        sessionSaveTimerRef.current = null;
      }
    };
  }, []);

  // Handle footer resize dragging
  useEffect(() => {
    const handleMouseMove = (e: MouseEvent) => {
      if (isDragging) {
        const newHeight = window.innerHeight - e.clientY;
        // Constrain height between 150px and 600px
        setFooterHeight(Math.max(150, Math.min(600, newHeight)));
      }
    };

    const handleMouseUp = () => {
      setIsDragging(false);
    };

    if (isDragging) {
      document.addEventListener('mousemove', handleMouseMove);
      document.addEventListener('mouseup', handleMouseUp);
      document.body.style.cursor = 'ns-resize';
      document.body.style.userSelect = 'none';
    }

    return () => {
      document.removeEventListener('mousemove', handleMouseMove);
      document.removeEventListener('mouseup', handleMouseUp);
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
    };
  }, [isDragging]);

  const upsertJobMessagesState = (jobId: string | undefined, incoming: Message | Message[]) => {
    if (!jobId) {
      setMessages(prev => {
        const messagesToAdd = Array.isArray(incoming) ? incoming : [incoming];
        const next = [...prev];
        messagesToAdd.forEach(message => {
          const index = next.findIndex(item => item.id === message.id);
          if (index >= 0) {
            next[index] = { ...next[index], ...message };
          } else {
            next.push(message);
          }
        });
        return next;
      });
      return;
    }
    setMessages(prev => upsertJobMessages(prev, jobId, incoming) as Message[]);
  };

  const initializeBackendJobCards = (jobId: string, execId: string, modeName: string) => {
    upsertJobMessagesState(jobId, {
      id: execId,
      type: 'execution',
      content: '',
      timestamp: new Date().toLocaleTimeString('en-US', { hour12: false }),
      jobId,
      executionSteps: [{
        id: `${execId}-accepted`,
        name: modeName === 'pilot' ? 'Backend Pilot' : 'Backend Run',
        status: 'running',
        log: 'Backend job accepted.',
      }],
    });
    if (modeName === 'pilot') {
      upsertJobMessagesState(jobId, {
        ...buildPilotLifecycleMessage(
          jobId,
          new Date().toLocaleTimeString('en-US', { hour12: false }),
        ),
        jobId,
      } as Message);
    }
  };

  const stageTimelineLog = (event: any, phase: 'started' | 'completed') => {
    const backendEvent = event.backend_event || {};
    const stage = String(backendEvent.stage || 'backend');
    const success = backendEvent.success !== false;
    const label = stageLabel(stage);
    return {
      source: 'stage',
      step: stage,
      level: phase === 'completed' ? (success ? 'SUCCESS' : 'ERROR') : 'INFO',
      message: phase === 'completed'
        ? `${label}: ${success ? 'completed' : 'failed'}`
        : `${label}: running`,
      icon: phase === 'completed' ? (success ? '✅' : '❌') : '',
      attempt_id: backendEvent.attempt_id,
    };
  };

  const stageLabel = (stage: string) => {
    const labels: Record<string, string> = {
      clarification: 'Clarification',
      clarification_llm: 'Clarification LLM',
      goal_clarification: 'Goal clarification',
      security_checker_clarification: 'Checker clarification',
      pilot_goal_clarification: 'Pilot goal clarification',
      planner: 'Planner',
      pipeline_generation: 'Pipeline generation',
      execution: 'Execution',
      judge: 'Judge',
    };
    return labels[stage] || stage.replace(/_/g, ' ');
  };

  const updateExecutionMessage = (
    execId: string,
    status: ExecutionStep['status'],
    log: string,
    name: string = 'Backend Run',
    jobId?: string,
  ) => {
    setMessages(prev => prev.map(message => {
      if (message.id !== execId || message.type !== 'execution') return message;
      const currentStep = message.executionSteps?.[0] || {
        id: 'backend-run',
        name,
        status: 'pending' as const,
        log: 'Pending...'
      };
      return {
        ...message,
        jobId: message.jobId || jobId,
        executionSteps: [{
          ...currentStep,
          name,
          status,
          log
        }]
      };
    }));
  };

  const ensureExecutionTimeline = (
    execId: string,
    tools: string[] = [],
    initialLog: string = 'Pipeline generated. Executing DSL.',
    jobId?: string,
  ) => {
    const stepNames = tools.length > 0 ? tools : ['Backend Run'];
    setMessages(prev => {
      if (prev.some(message => message.id === execId)) return prev;
      const message = {
          id: execId,
          type: 'execution',
          content: '',
          timestamp: new Date().toLocaleTimeString('en-US', { hour12: false }),
          jobId,
          executionSteps: stepNames.map((tool, idx) => ({
            id: `${execId}-step-${idx}`,
            name: tool,
            status: idx === 0 ? 'running' : 'pending',
            log: idx === 0 ? initialLog : 'Pending...'
          }))
        } as Message;
      return jobId ? upsertJobMessages(prev, jobId, message) as Message[] : [...prev, message];
    });
  };

  const completeExecutionTimeline = (
    execId: string,
    status: ExecutionStep['status'],
    log: string
  ) => {
    setMessages(prev => prev.map(message => {
      if (message.id !== execId || message.type !== 'execution' || !message.executionSteps) {
        return message;
      }
      return {
        ...message,
        executionSteps: message.executionSteps.map(step => ({
          ...step,
          status: step.status === 'running' || step.status === 'pending' ? status : step.status,
          log: step.status === 'running' || step.status === 'pending' ? log : step.log,
        }))
      };
    }));
  };

  const appendExecutionTimelineLog = (execId: string, backendLog: any, jobId?: string) => {
    const runtimeStep = executionStepFromBackendLog(backendLog) as ExecutionStep | null;
    if (!runtimeStep) return;

    setMessages(prev => {
      const messageIndex = prev.findIndex(message => message.id === execId && message.type === 'execution');
      const normalizeSteps = (steps: ExecutionStep[]) => {
        const existingIndex = steps.findIndex(step => step.id === runtimeStep.id);
        const nextSteps = runtimeStep.status === 'running'
          ? steps.map(step => step.status === 'running' ? { ...step, status: 'success' as const } : step)
          : steps;
        if (existingIndex >= 0) {
          return nextSteps.map((step, index) => index === existingIndex ? { ...step, ...runtimeStep } : step);
        }
        return [...nextSteps, runtimeStep];
      };

      if (messageIndex === -1) {
        const message = {
            id: execId,
            type: 'execution',
            content: '',
            timestamp: new Date().toLocaleTimeString('en-US', { hour12: false }),
            jobId,
            executionSteps: [runtimeStep],
          } as Message;
        return jobId ? upsertJobMessages(prev, jobId, message) as Message[] : [...prev, message];
      }

      const next = prev.map((message, index) => {
        if (index !== messageIndex || message.type !== 'execution') return message;
        return {
          ...message,
          jobId: message.jobId || jobId,
          executionSteps: normalizeSteps(message.executionSteps || []),
        };
      });
      return jobId ? upsertJobMessages(next, jobId, []) as Message[] : next;
    });
  };

  const addPipelineMessage = (
    jobId: string | undefined,
    pipeline: string,
    llmMetadata?: any,
    messageScope?: string,
  ) => {
    if (!pipeline) return;
    const pipelineId = `${jobId || 'backend'}${messageScope ? `-${messageScope}` : ''}-pipeline`;
    setMessages(prev => {
      if (prev.some(message => message.id === pipelineId)) return prev;
      const elapsed = llmMetadata?.elapsed_seconds !== undefined ? ` · ${llmMetadata.elapsed_seconds}s` : '';
      const header = llmMetadata?.model ? `# model: ${llmMetadata.model}${elapsed}\n` : '';
      const message = {
          id: pipelineId,
          type: 'pipeline',
          content: `${header}pipeline:\n${pipeline}`,
          timestamp: new Date().toLocaleTimeString('en-US', { hour12: false }),
          jobId,
        } as Message;
      return jobId ? upsertJobMessages(prev, jobId, message) as Message[] : [...prev, message];
    });
  };

  const appendSystemMessage = (content: string) => {
    setMessages(prev => [
      ...prev,
      {
        id: Date.now().toString() + '-system',
        type: 'system',
        content,
        timestamp: new Date().toLocaleTimeString('en-US', { hour12: false })
      }
    ]);
  };

  const startBackendRun = async (
    command: string,
    sessionId?: string,
    options: { budgetSteps?: number } = {}
  ) => {
    const execId = Date.now().toString() + '-backend-exec';
    setHeaderStatus('PROCESSING');
    setLogs([]);
    setBestScore('NA');
    setRunResultData(null);

    try {
      const submitted = sessionId
        ? await createSessionRun(sessionId, command, options)
        : await createRun(command, sessionId);
      runLastEventIdRef.current[submitted.job_id] = 0;
      runSeenEventIdsRef.current[submitted.job_id] = new Set<number>();
      if (sessionId) {
        runSessionByJobRef.current[submitted.job_id] = sessionId;
      }
      if (options.budgetSteps && submitted.mode === 'pilot') {
        pilotBudgetByJobRef.current[submitted.job_id] = options.budgetSteps;
      }
      setSessions(prev => prev.map(session => (
        session.id === sessionId
          ? {
            ...session,
            jobId: submitted.job_id,
            status: 'running',
            locked: false,
            backendMode: submitted.mode || session.backendMode,
          }
          : session
      )));
      setLogs(prev => [
        ...prev,
        `[${new Date().toLocaleTimeString('en-US', { hour12: false })}] [INFO] Backend job ${submitted.job_id} started`
      ]);
      initializeBackendJobCards(submitted.job_id, execId, submitted.mode || 'run');

      const source = subscribeRunEvents(submitted.job_id, {
        onEvent: (event: any) => handleBackendRunEvent(event, execId),
        onError: (error: any) => {
          if (runStreamsRef.current[submitted.job_id]?.readyState === EventSource.CLOSED) return;
          updateExecutionMessage(execId, 'error', 'Lost connection to backend event stream.');
          appendSystemMessage(`Backend event stream error: ${String(error?.message || error)}`);
          setHeaderStatus('STABLE');
          setExecutingSessionId(null);
        }
      });
      runStreamsRef.current[submitted.job_id] = source;
      startRunStatusPolling(submitted.job_id, execId);
    } catch (error: any) {
      ensureExecutionTimeline(execId, ['Backend Run'], 'Backend Run submission failed.');
      updateExecutionMessage(execId, 'error', 'Backend Run submission failed.');
      appendSystemMessage(`Backend Run failed to start: ${String(error?.message || error)}`);
      setHeaderStatus('STABLE');
      setExecutingSessionId(null);
    }
  };

  const handleBackendRunEvent = (event: any, execId: string) => {
    const eventJobId = event.job_id as string | undefined;
    const eventId = Number(event.event_id || 0);
    if (eventJobId && eventId > 0) {
      const seen = runSeenEventIdsRef.current[eventJobId] || new Set<number>();
      if (seen.has(eventId)) return;
      seen.add(eventId);
      runSeenEventIdsRef.current[eventJobId] = seen;
      runLastEventIdRef.current[eventJobId] = Math.max(
        runLastEventIdRef.current[eventJobId] || 0,
        eventId,
      );
    }

    if (String(event.type || '').startsWith('pilot.')) {
      if (event.type === 'pilot.pipeline' && event.pipeline) {
        addPipelineMessage(event.job_id, event.pipeline, event.llm, event.attempt_id || 'attempt');
        ensureExecutionTimeline(
          execId,
          extractPipelineTools(event.pipeline),
          `${event.attempt_id || 'Pilot'} pipeline generated.`,
          event.job_id,
        );
      }
      updatePilotLifecycle(event);
      return;
    }

    if (event.type === 'job.running') {
      updateExecutionMessage(
        execId,
        'running',
        event.mode === 'pilot' ? 'Backend pilot is running.' : 'Backend pipeline is running.',
        event.mode === 'pilot' ? 'Backend Pilot' : 'Backend Run',
        event.job_id,
      );
      return;
    }

    if (event.type === 'checkpoint.created') {
      const payload = event.payload || {};
      const checkpointMsgId = `${event.checkpoint_id}-clarification`;
      setHeaderStatus('PENDING');
      setMessages(prev => {
        if (prev.some(message => message.id === checkpointMsgId)) return prev;
        const message = {
            id: checkpointMsgId,
            type: 'clarification',
            content: '',
            question: payload.prompt || 'Please clarify the task.',
            status: 'pending',
            suggestions: checkpointSuggestions(payload),
            index: payload.turn || 1,
            total: 5,
            jobId: event.job_id,
            checkpointId: event.checkpoint_id,
            checkpointType: event.checkpoint_type,
            timestamp: new Date().toLocaleTimeString('en-US', { hour12: false })
          } as Message;
        return event.job_id ? upsertJobMessages(prev, event.job_id, message) as Message[] : [...prev, message];
      });
      setReplyingTo(checkpointMsgId);
      updateExecutionMessage(execId, 'running', 'Waiting for clarification.', 'Backend Run', event.job_id);
      return;
    }

    if (event.type === 'checkpoint.resolved') {
      setHeaderStatus('PROCESSING');
      setReplyingTo(null);
      return;
    }

    if (event.type === 'pipeline.generated') {
      const pipeline = event.pipeline || '';
      const tools = extractPipelineTools(pipeline);
      setPipelineTools(tools);
      addPipelineMessage(event.job_id, pipeline, event.llm_metadata);
      ensureExecutionTimeline(execId, tools, 'Pipeline generated. Executing DSL.', event.job_id);
      return;
    }

    if (event.type === 'backend.stage_started') {
      appendExecutionTimelineLog(execId, stageTimelineLog(event, 'started'), event.job_id);
      return;
    }

    if (event.type === 'backend.stage_completed') {
      appendExecutionTimelineLog(execId, stageTimelineLog(event, 'completed'), event.job_id);
      return;
    }

    if (event.type === 'log.appended') {
      setLogs(prev => mergeRunLogLines(prev, [formatBackendLog(event.log)]));
      appendExecutionTimelineLog(execId, event.log, event.job_id);
      return;
    }

    if (event.type === 'job.completed') {
      if (event.mode === 'pilot' || event.pilot_summary) {
        completeBackendPilot(event.job_id, execId, event);
        return;
      }
      completeBackendRun(event.job_id, execId, event);
      return;
    }

    if (event.type === 'job.failed') {
      if (event.mode === 'pilot' || event.pilot_summary || event.attempts) {
        failBackendPilot(event.job_id, execId, event);
        return;
      }
      failBackendRun(event.job_id, execId, event.error || event.execution?.error || 'Backend Run failed.');
    }
  };

  const closeRunStream = (jobId: string) => {
    const source = runStreamsRef.current[jobId];
    if (source) {
      source.close();
      delete runStreamsRef.current[jobId];
    }
  };

  const stopRunStatusPolling = (jobId: string) => {
    const intervalId = runPollersRef.current[jobId];
    if (intervalId !== undefined) {
      window.clearInterval(intervalId);
      delete runPollersRef.current[jobId];
    }
  };

  const replayRunEventsSince = async (jobId: string, execId: string) => {
    const events = await replayRunEvents(jobId, runLastEventIdRef.current[jobId] || 0);
    events.forEach((event: any) => handleBackendRunEvent(event, execId));
  };

  const startRunStatusPolling = (jobId: string, execId: string) => {
    stopRunStatusPolling(jobId);
    runPollersRef.current[jobId] = window.setInterval(async () => {
      try {
        await replayRunEventsSince(jobId, execId);
        if (runPollersRef.current[jobId] === undefined) {
          return;
        }
        const job = await fetchJob(jobId);
        const stream = runStreamsRef.current[jobId];
        const usePollingFallback = shouldUseRunStatusPollingFallback(stream?.readyState);
        if (job.mode === 'pilot' && usePollingFallback) {
          syncPilotLifecycleFromJob(job);
        }
        if (job.status === 'completed') {
          if (!usePollingFallback) {
            return;
          }
          if (job.mode === 'pilot' || job.result?.metadata?.pilot_summary) {
            completeBackendPilot(jobId, execId, {
              job_id: jobId,
              mode: 'pilot',
              status: 'success',
              result: job.result?.result ?? job.result,
              execution: {
                result: job.result?.result ?? job.result,
                artifacts: job.result?.artifacts || {},
                metadata: job.result?.metadata || {},
                logs: job.result?.logs || [],
                error: null
              },
              attempts: job.attempts || [],
              best_attempt: job.best_attempt || {
                attempt_id: job.result?.metadata?.best_attempt_id,
                judge: job.result?.metadata?.judge || {},
              },
              pilot_summary: job.pilot_summary || job.result?.metadata?.pilot_summary || {},
              approved_asset_ids: job.result?.metadata?.approved_asset_ids || [],
              candidate_asset_ids: job.candidate_asset_ids || [],
              clarification: job.result?.metadata?.goal_clarification || {},
            });
            return;
          }
          completeBackendRun(jobId, execId, {
            job_id: jobId,
            pipeline: job.pipeline,
            result: job.result?.result ?? job.result,
            execution: {
              result: job.result?.result ?? job.result,
              artifacts: job.result?.artifacts || {},
              metadata: job.result?.metadata || {},
              logs: job.result?.logs || [],
              error: null
            },
            clarification: {
              status: job.clarification_status,
              turns: job.clarification_turns,
              transcript: job.clarification_transcript,
              resolved_task: job.resolved_task,
              resolved_slots: job.resolved_slots
            },
            capability_gap: job.capability_gap || {}
          });
        } else if (job.status === 'failed') {
          if (!usePollingFallback) {
            return;
          }
          if (job.mode === 'pilot' || job.result?.metadata?.pilot_summary) {
            failBackendPilot(jobId, execId, {
              job_id: jobId,
              mode: 'pilot',
              status: 'failed',
              error: job.error || 'Backend Pilot failed.',
              result: job.result?.result ?? null,
              execution: {
                result: job.result?.result ?? null,
                artifacts: job.result?.artifacts || {},
                metadata: job.result?.metadata || {},
                logs: job.result?.logs || [],
                error: job.error || 'Backend Pilot failed.'
              },
              attempts: job.attempts || [],
              best_attempt: job.best_attempt || {
                attempt_id: job.result?.metadata?.best_attempt_id,
                judge: job.result?.metadata?.judge || {},
              },
              pilot_summary: job.pilot_summary || job.result?.metadata?.pilot_summary || {},
              approved_asset_ids: job.result?.metadata?.approved_asset_ids || [],
              candidate_asset_ids: job.candidate_asset_ids || [],
            });
            return;
          }
          failBackendRun(jobId, execId, job.error || 'Backend Run failed.');
        } else if (job.status === 'paused') {
          const checkpointEvent = checkpointEventFromJob(job);
          if (checkpointEvent) {
            handleBackendRunEvent(checkpointEvent, execId);
          }
          setHeaderStatus('PENDING');
          updateExecutionMessage(execId, 'running', 'Backend Run is waiting for user input.');
        }
      } catch (_error) {
        // Keep SSE as the primary transport; polling is only a state reconciliation fallback.
      }
    }, 1000);
  };

  const completeBackendRun = (jobId: string, execId: string, event: any) => {
    stopRunStatusPolling(jobId);
    closeRunStream(jobId);
    if (event.pipeline) {
      addPipelineMessage(jobId, event.pipeline, event.llm_metadata);
      ensureExecutionTimeline(execId, extractPipelineTools(event.pipeline), 'Backend Run completed.', jobId);
    }
    setLogs(prev => mergeRunLogLines(prev, logsFromRunCompletion(event)));
    const resultData = normalizeRunResultData(event);
    setBestScore(resultData.score);
    setRunResultData(resultData);
    completeExecutionTimeline(execId, 'success', 'Backend Run completed.');
    setMessages(prev => {
      if (prev.some(message => message.id === `${jobId}-result`)) return prev;
      return upsertJobMessages(prev, jobId, buildRunCompletionResultMessage(
          jobId,
          resultData,
          new Date().toLocaleTimeString('en-US', { hour12: false })
        ) as Message) as Message[];
    });
    setHeaderStatus('STABLE');
    setExecutingSessionId(null);
    const completedSessionId = runSessionByJobRef.current[jobId];
    setSessions(prev => prev.map(session => (
      session.jobId === jobId || session.id === completedSessionId
        ? { ...session, jobId, status: 'completed', locked: true }
        : session
    )));
  };

  const updatePilotLifecycle = (event: any) => {
    setHeaderStatus('PROCESSING');
    setAttempts(prev => {
      const next = applyPilotEventToAttempts(prev, event) as Attempt[];
      const maxScore = next.length ? Math.max(...next.map(attempt => Number(attempt.score || 0))) : 0;
      setBestScore(next.length ? maxScore : 'NA');
      return next;
    });
    setMessages(prev => {
      const lifecycleId = pilotLifecycleMessageId(event.job_id);
      const lifecycleMessage = {
        ...buildPilotLifecycleMessage(
          event.job_id,
          new Date().toLocaleTimeString('en-US', { hour12: false }),
        ),
        jobId: event.job_id,
      } as Message;
      const baseMessages = prev.some(message => message.id === lifecycleId)
        ? prev
        : upsertJobMessages(prev, event.job_id, lifecycleMessage) as Message[];
      const next = baseMessages.map(message => (
        message.id === lifecycleId && message.type === 'lifecycle'
          ? { ...message, attempts: applyPilotEventToAttempts(message.attempts || attempts, event) as Attempt[] }
          : message
      ));
      return upsertJobMessages(next, event.job_id, []) as Message[];
    });
  };

  const pilotBudgetFromMessages = () => {
    const attemptQuestion = [...messages].reverse().find(
      message => message.checkpointType === 'pilot_attempt_count'
    );
    const parsed = parsePilotAttemptCount(
      attemptQuestion?.userReply || attemptQuestion?.resolvedText || ''
    );
    return parsed.ok ? parsed.value : null;
  };

  const syncPilotLifecycleFromJob = (job: any) => {
    if (job?.mode !== 'pilot' || !Array.isArray(job.attempts) || job.attempts.length === 0) {
      return;
    }
    const totalAttempts = (
      pilotBudgetByJobRef.current[job.job_id] ||
      pilotBudgetFromMessages() ||
      job.budget_steps ||
      job.attempt_count ||
      job.attempts.length
    );
    const lifecycleAttempts = buildPilotAttemptsFromBackendAttempts(
      job.attempts,
      totalAttempts
    ) as Attempt[];
    setAttempts(lifecycleAttempts);
    const maxScore = lifecycleAttempts.length
      ? Math.max(...lifecycleAttempts.map(attempt => Number(attempt.score || 0)))
      : 0;
    setBestScore(lifecycleAttempts.length ? maxScore : 'NA');
    job.attempts.forEach((attempt: any) => {
      if (attempt?.pipeline) {
        addPipelineMessage(
          job.job_id,
          attempt.pipeline,
          attempt.pipeline_llm,
          attempt.attempt_id || 'attempt',
        );
      }
    });
    setMessages(prev => {
      const lifecycleId = pilotLifecycleMessageId(job.job_id);
      const lifecycleMessage = {
        ...buildPilotLifecycleMessage(
          job.job_id,
          new Date().toLocaleTimeString('en-US', { hour12: false }),
        ),
        jobId: job.job_id,
      } as Message;
      const baseMessages = prev.some(message => message.id === lifecycleId)
        ? prev
        : upsertJobMessages(prev, job.job_id, lifecycleMessage) as Message[];
      const next = baseMessages.map(message => (
        message.id === lifecycleId && message.type === 'lifecycle'
          ? { ...message, attempts: lifecycleAttempts }
          : message
      ));
      return upsertJobMessages(next, job.job_id, []) as Message[];
    });
  };

  useEffect(() => {
    if (!activeSession?.jobId) return;
    const isPilotSession =
      String(activeSession.backendMode || '').toLowerCase() === 'pilot' ||
      String(activeSession.mode || '').toUpperCase() === 'PILOT';
    if (!isPilotSession) return;

    let cancelled = false;
    fetchJob(activeSession.jobId)
      .then(job => {
        if (!cancelled) {
          syncPilotLifecycleFromJob(job);
        }
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [activeSession?.jobId, activeSession?.backendMode, activeSession?.mode]);

  const completeBackendPilot = (jobId: string, execId: string, event: any) => {
    stopRunStatusPolling(jobId);
    closeRunStream(jobId);
    setLogs(prev => mergeRunLogLines(prev, logsFromRunCompletion(event)));
    const summaryMessage = buildPilotSummaryMessage(
      event,
      new Date().toLocaleTimeString('en-US', { hour12: false })
    ) as Message;
    const candidateMessage = buildRejectedPilotToolCandidateMessage(
      event,
      new Date().toLocaleTimeString('en-US', { hour12: false })
    ) as Message;
    const resultMessage = buildPilotCompletionResultMessage(
      event,
      new Date().toLocaleTimeString('en-US', { hour12: false })
    ) as Message;
    setRunResultData(resultMessage.resultData || null);
    setBestScore(resultMessage.resultData?.score ?? 'NA');
    completeExecutionTimeline(execId, 'success', 'Pilot completed.');
    setMessages(prev => upsertJobMessages(
      prev,
      jobId,
      [summaryMessage, candidateMessage, resultMessage],
    ) as Message[]);
    setHeaderStatus('STABLE');
    setExecutingSessionId(null);
    const completedSessionId = runSessionByJobRef.current[jobId];
    setSessions(prev => prev.map(session => (
      session.jobId === jobId || session.id === completedSessionId
        ? { ...session, jobId, status: 'completed', locked: true }
        : session
    )));
  };

  const failBackendPilot = (jobId: string, execId: string, event: any) => {
    stopRunStatusPolling(jobId);
    closeRunStream(jobId);
    setLogs(prev => mergeRunLogLines(prev, logsFromRunCompletion(event)));
    ensureExecutionTimeline(execId, ['Backend Pilot'], event.error || 'Pilot failed.', jobId);
    completeExecutionTimeline(execId, 'error', event.error || 'Pilot failed.');
    const summaryMessage = buildPilotSummaryMessage(
      event,
      new Date().toLocaleTimeString('en-US', { hour12: false })
    ) as Message;
    const candidateMessage = buildRejectedPilotToolCandidateMessage(
      event,
      new Date().toLocaleTimeString('en-US', { hour12: false })
    ) as Message;
    const resultMessage = buildPilotCompletionResultMessage(
      { ...event, type: 'job.failed' },
      new Date().toLocaleTimeString('en-US', { hour12: false })
    ) as Message;
    setRunResultData(resultMessage.resultData || null);
    setMessages(prev => upsertJobMessages(
      prev,
      jobId,
      [summaryMessage, candidateMessage, resultMessage],
    ) as Message[]);
    setHeaderStatus('STABLE');
    setExecutingSessionId(null);
    const failedSessionId = runSessionByJobRef.current[jobId];
    setSessions(prev => prev.map(session => (
      session.jobId === jobId || session.id === failedSessionId
        ? { ...session, jobId, status: 'failed', locked: true }
        : session
    )));
  };

  const failBackendRun = (jobId: string, execId: string, error: string) => {
    stopRunStatusPolling(jobId);
    closeRunStream(jobId);
    ensureExecutionTimeline(execId, ['Backend Run'], error, jobId);
    completeExecutionTimeline(execId, 'error', error);
    const failedMessage = buildRunFailureResultMessage(
      jobId,
      error,
      new Date().toLocaleTimeString('en-US', { hour12: false })
    ) as Message;
    setRunResultData(failedMessage.resultData || null);
    setMessages(prev => {
      if (prev.some(message => message.id === `${jobId}-failed`)) return prev;
      return upsertJobMessages(prev, jobId, failedMessage) as Message[];
    });
    setHeaderStatus('STABLE');
    setExecutingSessionId(null);
    const failedSessionId = runSessionByJobRef.current[jobId];
    setSessions(prev => prev.map(session => (
      session.jobId === jobId || session.id === failedSessionId
        ? { ...session, jobId, status: 'failed', locked: true }
        : session
    )));
  };

  const submitBackendCheckpointReply = async (message: Message, reply: string) => {
    if (!message.jobId || !message.checkpointId) return;
    setMessages(prev => prev.map(item => {
      if (item.id !== message.id) return item;
      return {
        ...item,
        status: 'resolved' as const,
        userReply: reply,
        resolvedText: `Resolved: ${reply}`
      };
    }));
    setReplyingTo(null);
    setInput('');
    setHeaderStatus('PROCESSING');
    try {
      await answerCheckpoint(
        message.jobId,
        message.checkpointId,
        pilotCheckpointAnswerPayload(message, reply),
      );
    } catch (error: any) {
      appendSystemMessage(`Failed to answer backend checkpoint: ${String(error?.message || error)}`);
      setHeaderStatus('STABLE');
      setExecutingSessionId(null);
    }
  };

  const submitSessionModeSelection = async (message: Message, reply: string) => {
    if (!activeSession) return;
    const normalized = reply.trim().toLowerCase();
    const selectedMode = normalized.includes('pilot')
      ? 'pilot'
      : normalized.includes('run')
        ? 'run'
        : '';
    if (!selectedMode) {
      appendSystemMessage('Please choose either run or pilot for this session.');
      return;
    }

    setInput('');
    setHeaderStatus('PROCESSING');
    try {
      const saved = await setSessionMode(activeSession.id, selectedMode);
      const displayMode = String(saved.mode || selectedMode).toUpperCase();
      setMode(displayMode);
      const promptMessage = buildModeCommandPromptMessage(
        activeSession.id,
        selectedMode
      ) as Message;
      setMessages(prev => {
        const resolvedMessages = prev.map(item => (
          item.id === message.id
            ? {
              ...item,
              status: 'resolved' as const,
              userReply: reply,
              resolvedText: `Resolved: mode = ${selectedMode}`
            }
            : item
        ));
        if (resolvedMessages.some(item => item.id === promptMessage.id)) {
          return resolvedMessages;
        }
        return [...resolvedMessages, promptMessage];
      });
      setSessions(prev => prev.map(session => (
        session.id === activeSession.id
          ? {
            ...session,
            mode: displayMode,
            backendMode: saved.backend_mode || 'run',
            status: saved.status || 'mode_selected',
            locked: Boolean(saved.locked),
          }
          : session
      )));
      setReplyingTo(null);
      setHeaderStatus('IDLE');
    } catch (error: any) {
      appendSystemMessage(`Failed to set session mode: ${String(error?.message || error)}`);
      setHeaderStatus('STABLE');
    }
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    const cmd = input.trim();
    if (!cmd) return;

    if (!activeSession) {
      await createAndActivateSession();
      return;
    }

    if (activeSession.locked && !replyingTo) {
      appendSystemMessage('This session is locked because its backend job has finished. Create a new session to continue.');
      setInput('');
      return;
    }

    setHeaderStatus('STABLE');

    if (!replyingTo && activeSession && (activeSession.name === 'Untitled' || activeSession.name === 'new_session')) {
      const newName = extractTaskName(cmd);
      setSessions(prev => prev.map(s => 
        s.id === activeSession.id ? { ...s, name: newName } : s
      ));
      updateSession(activeSession.id, { name: newName }).catch(() => {});
    }

    const lowerCmd = cmd.toLowerCase();
    const parsedCommand = parseUserCommand(cmd);
    
    let detectedMode = activeSession.mode || mode;

    if (!replyingTo && sessionNeedsMode(activeSession)) {
      const modeMessage = createModeSelectionMessage(activeSession.id);
      setMessages(prev => (
        prev.some(message => message.checkpointType === 'mode_selection')
          ? prev
          : [modeMessage, ...prev]
      ));
      setReplyingTo(modeMessage.id);
      setHeaderStatus('PENDING');
      setInput('');
      return;
    }
    if (!replyingTo) {
      detectedMode = (activeSession.mode || mode || parsedCommand.mode || 'RUN').toUpperCase();
    }

    if (replyingTo) {
      // Resolve a clarification
      const clarifiedMsg = messages.find(m => m.id === replyingTo);
      if (clarifiedMsg?.checkpointType === 'mode_selection') {
        await submitSessionModeSelection(clarifiedMsg, cmd);
        return;
      }
      if (clarifiedMsg?.checkpointType === 'pilot_attempt_count') {
        const parsedAttempts = parsePilotAttemptCount(cmd);
        if (!parsedAttempts.ok) {
          appendSystemMessage('请输入 1 到 10 之间的 attempt 轮数。');
          setInput('');
          setHeaderStatus('PENDING');
          return;
        }
        const pendingCommand = clarifiedMsg.pendingCommand;
        if (!pendingCommand) {
          appendSystemMessage('Pilot command is missing. Please start a new pilot session.');
          setReplyingTo(null);
          setInput('');
          setHeaderStatus('STABLE');
          return;
        }
        setMessages(prev => prev.map(item => (
          item.id === clarifiedMsg.id
            ? {
              ...item,
              status: 'resolved' as const,
              userReply: cmd,
              resolvedText: `Resolved: attempts = ${parsedAttempts.value}`
            }
            : item
        )));
        setReplyingTo(null);
        setInput('');
        setHeaderStatus('PROCESSING');
        startBackendRun(pendingCommand, activeSession?.id, { budgetSteps: parsedAttempts.value });
        return;
      }
      if (clarifiedMsg?.jobId && clarifiedMsg?.checkpointId) {
        submitBackendCheckpointReply(clarifiedMsg, cmd);
        return;
      }

      // Check if this is a follow-up question (追问)
      const isFollowUpQuestion = (text: string) => {
        const hasQuestionMark = /[？?]/.test(text);
        const hasQuestionWord = /^(哪个|什么|如何|怎么|为什么|是否|可以|能不能|应该|推荐|建议|适合|which|what|how|why|should|recommend|suggest|suitable)/i.test(text);
        const isNotDirectSelection = !text.toLowerCase().includes('default') &&
                                     !text.split(',').every(item => item.trim().length > 0 && /^[a-z_]+$/.test(item.trim()));
        return (hasQuestionMark || hasQuestionWord) && isNotDirectSelection;
      };

      // Determine clarification type based on question content
      const isStrategyQuestion = clarifiedMsg?.question?.includes('baseline') || clarifiedMsg?.question?.includes('balanced') || clarifiedMsg?.question?.includes('strong');

      // Check if this is a dataset question - either by keyword or by checking if suggestions are dataset names
      const allAvailableDatasets = ['posttrain_dialog_safety_v3', 'instruction_tuning_value_pack', 'diverse_instruction_pickset', 'wind_tunnel_trajectory_archive', 'agent_trace_skill_mining_pool'];
      const hasDatasetKeyword = clarifiedMsg?.question?.includes('数据集') || clarifiedMsg?.question?.includes('dataset');
      const hasSingleDatasetSuggestion = clarifiedMsg?.suggestions?.length === 1 &&
                                          clarifiedMsg.suggestions.every(s => allAvailableDatasets.includes(s));
      const isDatasetQuestion = hasDatasetKeyword || hasSingleDatasetSuggestion;

      const isAttemptsQuestion = clarifiedMsg?.question?.includes('attempt');

      if (isStrategyQuestion && isFollowUpQuestion(cmd)) {
        // User is asking a follow-up question about strategy, provide guidance
        const updatedMessages = messages.map(m => {
          if (m.id === replyingTo) {
            return {
              ...m,
              status: 'resolved' as const,
              userReply: cmd,
              resolvedText: `User follow-up: ${cmd}`
            };
          }
          return m;
        });
        setMessages(updatedMessages);
        setReplyingTo(null);
        setInput('');

        // Generate a contextual response based on the question (with thinking delay)
        setTimeout(() => {
          let responseText = '';
          let suggestedStrategy = 'balanced';

          if (/成本|cost|速度|fast|cheap|便宜|快/i.test(cmd) && /效果|quality|覆盖|coverage/i.test(cmd)) {
            responseText = '我建议用 `balanced`。它通常会先用规则型检查器做隐私、secret、toxicity、harmful 等基础覆盖，再只对证据不明确或风险较高的样本补一层轻量语义判断。这样通常比 `cheap baseline` 更稳，又不会像 `strong` 那样成本高、耗时长。\n\n另外，你要不要我明确把"隐私泄露"和"越狱风险"设成优先维度？';
            suggestedStrategy = 'balanced';
          } else if (/最便宜|cheapest|最快|fastest/i.test(cmd)) {
            responseText = '如果优先考虑成本和速度，建议用 `cheap baseline`。它主要依赖规则和关键词检查，非常快速且成本低廉。不过要注意，它可能会漏掉一些需要语义理解才能检测的风险。\n\n要不要我就用 `cheap baseline`？';
            suggestedStrategy = 'cheap baseline';
          } else if (/最好|best|最强|strongest|最全|comprehensive/i.test(cmd)) {
            responseText = '如果希望最全面的覆盖，建议用 `strong`。它会使用更强大的语义判断模型，能检测到更多微妙的风险。但要注意，运行时间会更长，成本也会更高。\n\n要不要我就用 `strong`？';
            suggestedStrategy = 'strong';
          } else {
            responseText = '我建议用 `balanced`。它在成本、速度和效果之间取得了很好的平衡，适合大多数场景。\n\n另外，你要不要我明确把"隐私泄露"和"越狱风险"设成优先维度？';
            suggestedStrategy = 'balanced';
          }

          const attemptsMsg = updatedMessages.find(m => m.type === 'clarification' && m.question?.includes('attempt') && m.status === 'resolved');
          const datasetMsg = updatedMessages.find(m => m.type === 'clarification' && (m.question?.includes('数据集') || m.question?.includes('dataset')) && m.status === 'resolved');

          setMessages(prev => [
            ...prev,
            {
              id: Date.now().toString() + '-follow-strategy',
              type: 'clarification',
              content: '',
              question: responseText,
              status: 'pending',
              suggestions: [suggestedStrategy],
              index: 3,
              total: 3,
              timestamp: new Date().toLocaleTimeString('en-US', { hour12: false })
            }
          ]);
        }, 2500);

        return;
      }

      if (isDatasetQuestion && isFollowUpQuestion(cmd)) {
        // User is asking a follow-up question, provide guidance
        const updatedMessages = messages.map(m => {
          if (m.id === replyingTo) {
            return {
              ...m,
              status: 'resolved' as const,
              userReply: cmd,
              resolvedText: `User follow-up: ${cmd}`
            };
          }
          return m;
        });
        setMessages(updatedMessages);
        setReplyingTo(null);
        setInput('');

        // Generate a contextual response based on the question (with thinking delay)
        setTimeout(() => {
          let responseText = '';
          let suggestedDataset = 'posttrain_dialog_safety_v3';

          if (/后训练|对话|post.*train|dialog|conversation|sft|instruction/i.test(cmd)) {
            responseText = '如果目标是做后训练对话数据的安全与质量审计，最合适的是 **posttrain_dialog_safety_v3**。它专门针对 instruction-following / SFT 风格的数据，适合检查隐私泄露、越狱风险、有害回复和标注漂移。\n\n要不要我就用 `posttrain_dialog_safety_v3`？';
            suggestedDataset = 'posttrain_dialog_safety_v3';
          } else if (/安全|风险|安全审计|security|risk|audit/i.test(cmd)) {
            responseText = '对于安全审计场景，推荐使用 **posttrain_dialog_safety_v3**，它包含丰富的安全风险样本，可以全面检测潜在的安全问题。\n\n要不要我就用 `posttrain_dialog_safety_v3`？';
            suggestedDataset = 'posttrain_dialog_safety_v3';
          } else if (/指令|instruction|tuning|价值|value/i.test(cmd)) {
            responseText = '对于指令调优数据评估，建议使用 **instruction_tuning_value_pack**，它专门用于评估指令遵循质量和价值对齐。\n\n要不要我就用 `instruction_tuning_value_pack`？';
            suggestedDataset = 'instruction_tuning_value_pack';
          } else if (/多样|diverse|variety|comprehensive/i.test(cmd)) {
            responseText = '如果需要多样性评估，推荐 **diverse_instruction_pickset**，它覆盖了各种类型的指令和场景。\n\n要不要我就用 `diverse_instruction_pickset`？';
            suggestedDataset = 'diverse_instruction_pickset';
          } else if (/智能体|agent|trace|轨迹|行为/i.test(cmd)) {
            responseText = '对于智能体行为分析，最适合 **agent_trace_skill_mining_pool**，它包含大量的智能体执行轨迹和技能挖掘数据。\n\n要不要我就用 `agent_trace_skill_mining_pool`？';
            suggestedDataset = 'agent_trace_skill_mining_pool';
          } else {
            responseText = `基于你的需求，我推荐使用 **${suggestedDataset}**。这个数据集比较全面，适合大多数审计场景。\n\n要不要我就用 \`${suggestedDataset}\`？`;
          }

          const datasets = ['posttrain_dialog_safety_v3', 'instruction_tuning_value_pack', 'diverse_instruction_pickset', 'wind_tunnel_trajectory_archive', 'agent_trace_skill_mining_pool'];
          const attemptsMsg = updatedMessages.find(m => m.type === 'clarification' && m.question?.includes('attempt') && m.status === 'resolved');

          setMessages(prev => [
            ...prev,
            {
              id: Date.now().toString() + '-follow',
              type: 'clarification',
              content: '',
              question: responseText,
              status: 'pending',
              suggestions: [suggestedDataset],
              index: attemptsMsg ? 2 : 1,
              total: attemptsMsg ? 3 : 1,
              timestamp: new Date().toLocaleTimeString('en-US', { hour12: false })
            }
          ]);
        }, 2500);

        return;
      }

      let resolvedText = '';
      let selectedDatasets = '';
      let selectedStrategy = '';

      if (isDatasetQuestion) {
        // Handle default keywords and affirmative responses
        const lowerCmd = cmd.toLowerCase().trim();
        const isDefaultOrAffirmative =
          lowerCmd === 'default' ||
          lowerCmd === 'use default' ||
          lowerCmd === 'use defaults' ||
          lowerCmd === '好' ||
          lowerCmd === 'ok' ||
          lowerCmd === 'okay' ||
          lowerCmd === 'yes' ||
          lowerCmd === '是' ||
          lowerCmd === '行' ||
          lowerCmd === '可以';

        if (isDefaultOrAffirmative) {
          // Check if this is a follow-up clarification with specific suggestion
          const currentClarification = messages.find(m => m.id === replyingTo);
          if (currentClarification?.suggestions && currentClarification.suggestions.length === 1) {
            // Use the suggested dataset from follow-up
            selectedDatasets = currentClarification.suggestions[0];
            resolvedText = `Resolved: datasets = [${selectedDatasets}] (recommended)`;
          } else {
            // Use default set
            selectedDatasets = 'posttrain_dialog_safety_v3, instruction_tuning_value_pack, diverse_instruction_pickset';
            resolvedText = `Resolved: datasets = [${selectedDatasets}] (default)`;
          }
        } else {
          // Handle multiple datasets separated by comma
          selectedDatasets = cmd.trim();
          const datasetList = selectedDatasets.split(',').map(d => d.trim()).filter(d => d);

          // Define all available datasets in the system
          const allAvailableDatasets = ['posttrain_dialog_safety_v3', 'instruction_tuning_value_pack', 'diverse_instruction_pickset', 'wind_tunnel_trajectory_archive', 'agent_trace_skill_mining_pool'];

          // Check if all requested datasets are valid dataset names
          const invalidInputs = datasetList.filter(d => !allAvailableDatasets.includes(d));

          if (invalidInputs.length > 0) {
            // User input contains non-dataset names - show capability gap and terminate
            const updatedMessages = messages.map(m => {
              if (m.id === replyingTo) {
                return {
                  ...m,
                  status: 'resolved' as const,
                  userReply: cmd,
                  resolvedText: `Resolved: invalid dataset input`
                };
              }
              return m;
            });
            setMessages(updatedMessages);
            setReplyingTo(null);
            setInput('');

            // Show Capability Gap card
            setTimeout(() => {
              setMessages(prev => [
                ...prev,
                {
                  id: Date.now().toString(),
                  type: 'system',
                  content: `🚫 Capability Gap\n\n数据集不足`,
                  timestamp: new Date().toLocaleTimeString('en-US', { hour12: false })
                }
              ]);

              // Show task terminated message
              setTimeout(() => {
                setMessages(prev => [
                  ...prev,
                  {
                    id: Date.now().toString(),
                    type: 'system',
                    content: '任务已终止。',
                    timestamp: new Date().toLocaleTimeString('en-US', { hour12: false })
                  }
                ]);
                setHeaderStatus('STABLE');
                setExecutingSessionId(null);
              }, 300);
            }, 600);

            return;
          }

          // All inputs are valid datasets - use user's selection
          resolvedText = `Resolved: datasets = [${datasetList.join(', ')}]`;
        }
      } else if (isAttemptsQuestion) {
        resolvedText = `Resolved: attempts = ${cmd}`;
      } else if (isStrategyQuestion) {
        const lowerCmd = cmd.toLowerCase().trim();
        const isDefaultOrAffirmative =
          lowerCmd === 'default' ||
          lowerCmd === 'use default' ||
          lowerCmd === 'use defaults' ||
          lowerCmd === '好' ||
          lowerCmd === 'ok' ||
          lowerCmd === 'okay' ||
          lowerCmd === 'yes' ||
          lowerCmd === '是' ||
          lowerCmd === '行' ||
          lowerCmd === '可以';

        if (isDefaultOrAffirmative) {
          // Check if this is a follow-up with a specific suggestion
          const currentClarification = messages.find(m => m.id === replyingTo);
          if (currentClarification?.suggestions && currentClarification.suggestions.length === 1) {
            // Use the suggested strategy from follow-up
            selectedStrategy = currentClarification.suggestions[0];
            resolvedText = `Resolved: checker_profile = ${selectedStrategy}`;
          } else {
            // Use default balanced strategy
            selectedStrategy = 'balanced';
            resolvedText = `Resolved: checker_profile = balanced, priority_dimensions = [privacy_leakage, jailbreak_risk]`;
          }
        } else {
          selectedStrategy = cmd.trim();
          resolvedText = `Resolved: checker_profile = ${cmd}`;
        }
      } else {
        resolvedText = clarifiedMsg?.question?.includes('attempt')
          ? `Resolved: attempts = ${cmd}`
          : `Resolved: checker_names = ${cmd === 'use defaults' ? '[PIIRule, SecretRule, ToxicityKeywordRule]' : '[' + cmd + ']'}`;
      }

      const updatedMessages = messages.map(m => {
        if (m.id === replyingTo) {
          return {
            ...m,
            status: 'resolved' as const,
            userReply: cmd,
            resolvedText
          };
        }
        return m;
      });
      setMessages(updatedMessages);
      setReplyingTo(null);
      setInput('');

      // Send pipeline message after resolution
      setTimeout(() => {
        if (isAttemptsQuestion) {
          // After attempts selection in PILOT mode, ask for dataset
          setHeaderStatus('PENDING');
          const datasets = ['posttrain_dialog_safety_v3', 'instruction_tuning_value_pack', 'diverse_instruction_pickset', 'wind_tunnel_trajectory_archive', 'agent_trace_skill_mining_pool'];
          setMessages(prev => [
            ...prev,
            {
              id: Date.now().toString(),
              type: 'clarification',
              content: '',
              question: `请选择要运行的数据集（可多选，用逗号分隔）：\nsuggestion: ${datasets.slice(0, 3).join(', ')}\n输入 "default" 使用推荐数据集`,
              status: 'pending',
              suggestions: datasets,
              index: 2,
              total: 3,
              timestamp: new Date().toLocaleTimeString('en-US', { hour12: false })
            }
          ]);
        } else if (isDatasetQuestion) {
          // After dataset selection, check if we have attempts (PILOT mode)
          const attemptsMsg = updatedMessages.find(m =>
            m.type === 'clarification' &&
            m.question?.includes('attempt') &&
            m.status === 'resolved'
          );

          if (attemptsMsg) {
            // PILOT mode: always ask for strategy clarification
            setHeaderStatus('PENDING');

            // Check if original command mentions specific requirements to customize the question
            const firstUserMsg = updatedMessages.find(m => m.type === 'user');
            const mentionsCostOrCoverage = firstUserMsg && (
              /成本|cost|速度|fast|cheap|覆盖|coverage|风险|risk|隐私|privacy|越狱|jailbreak|有害|harmful/i.test(firstUserMsg.content)
            );

            const strategyQuestion = mentionsCostOrCoverage
              ? `刚才提到既要控制成本，又希望覆盖隐私、越狱和有害风险。我可以采用三种策略：\n- \`cheap baseline\`：更便宜、更快，主要做规则和关键词覆盖\n- \`balanced\`：先做基础覆盖，再补一个轻量语义判断\n- \`strong\`：更强的语义覆盖，但更慢、更贵\n你希望我用哪一种？`
              : `请选择checker策略：\n- \`cheap baseline\`：更便宜、更快，主要做规则和关键词覆盖\n- \`balanced\`：先做基础覆盖，再补一个轻量语义判断\n- \`strong\`：更强的语义覆盖，但更慢、更贵\n输入 "default" 使用推荐的balanced策略`;

            setMessages(prev => [
              ...prev,
              {
                id: Date.now().toString(),
                type: 'clarification',
                content: '',
                question: strategyQuestion,
                status: 'pending',
                suggestions: ['cheap baseline', 'balanced', 'strong'],
                index: 3,
                total: 3,
                timestamp: new Date().toLocaleTimeString('en-US', { hour12: false })
              }
            ]);
          } else {
            // RUN mode, proceed to pipeline
            const pipelineContent = `pipeline:\n  - name: "General Task"\n    tool: "Auto Processor"\n    params:\n      datasets: [${selectedDatasets}]`;
            const toolsList = ['Auto Processor'];

            setMessages(prev => [
              ...prev,
              {
                id: Date.now().toString(),
                type: 'pipeline',
                content: pipelineContent,
                timestamp: new Date().toLocaleTimeString('en-US', { hour12: false })
              }
            ]);

            setPipelineTools(toolsList);
            setTimeout(() => runExecutionSimulation(toolsList, detectedMode), 1000);
          }
        } else if (isStrategyQuestion) {
          // After strategy selection, proceed to pipeline
          const attemptsMsg = updatedMessages.find(m =>
            m.type === 'clarification' &&
            m.question?.includes('attempt') &&
            m.status === 'resolved'
          );
          const datasetMsg = updatedMessages.find(m =>
            m.type === 'clarification' &&
            (m.question?.includes('数据集') || m.question?.includes('dataset')) &&
            m.status === 'resolved'
          );

          const parsedAttempts = parseInt(attemptsMsg?.userReply || '3');
          const attemptsCount = !isNaN(parsedAttempts) ? parsedAttempts : 3;

          let datasetsParam = 'posttrain_dialog_safety_v3, instruction_tuning_value_pack, diverse_instruction_pickset';
          if (datasetMsg?.userReply) {
            const lowerReply = datasetMsg.userReply.toLowerCase().trim();
            const isDefaultOrAffirmative =
              lowerReply === 'default' ||
              lowerReply === 'use default' ||
              lowerReply === 'use defaults' ||
              lowerReply === '好' ||
              lowerReply === 'ok' ||
              lowerReply === 'okay' ||
              lowerReply === 'yes' ||
              lowerReply === '是' ||
              lowerReply === '行' ||
              lowerReply === '可以';

            if (!isDefaultOrAffirmative) {
              // Extract actual datasets from resolvedText
              const resolvedMatch = datasetMsg.resolvedText?.match(/datasets = \[([^\]]+)\]/);
              if (resolvedMatch) {
                datasetsParam = resolvedMatch[1];
              } else {
                datasetsParam = datasetMsg.userReply.trim();
              }
            }
          }

          const pipelineContent = `pipeline:\n  - name: "Pilot Task"\n    tool: "Auto Processor"\n    params:\n      attempts: ${attemptsCount}\n      datasets: [${datasetsParam}]\n      checker_profile: ${selectedStrategy}`;
          const toolsList = ['Auto Processor'];

          setMessages(prev => [
            ...prev,
            {
              id: Date.now().toString(),
              type: 'pipeline',
              content: pipelineContent,
              timestamp: new Date().toLocaleTimeString('en-US', { hour12: false })
            }
          ]);

          setPipelineTools(toolsList);
          setTimeout(() => runExecutionSimulation(toolsList, detectedMode, attemptsCount), 1000);
        } else {
          // Old legacy clarifications
          let pipelineContent = '';
          let toolsList: string[] = [];
          const isPilotClarification = clarifiedMsg?.question?.includes('attempt');

          if (isPilotClarification) {
            if (activeTool === '恶意skills识别') {
              pipelineContent = `pipeline:\n  - name: "Skill Extractor"\n    tool: "恶意skills识别"\n    params:\n      attempts: ${cmd}\n      checker_names: ["PIIRule", "SecretRule"]\n  - name: "Format Output"\n    tool: "JSON Formatter"`;
              toolsList = ['恶意skills识别', 'JSON Formatter'];
            } else {
              pipelineContent = `pipeline:\n  - name: "Pilot Task"\n    tool: "Auto Processor"\n    params:\n      attempts: ${cmd}`;
              toolsList = ['Auto Processor'];
            }
          } else {
            const checkerNames = cmd === 'use defaults' ? '["PIIRule", "SecretRule", "ToxicityKeywordRule"]' : `["${cmd}"]`;
            pipelineContent = `pipeline:\n  - name: "Skill Extractor"\n    tool: "恶意skills识别"\n    params:\n      checker_names: ${checkerNames}\n  - name: "Format Output"\n    tool: "JSON Formatter"`;
            toolsList = ['恶意skills识别', 'JSON Formatter'];
          }

          const parsedAttempts = parseInt(cmd);
          const attemptsCount = !isNaN(parsedAttempts) ? parsedAttempts : 3;

          setMessages(prev => [
            ...prev,
            {
              id: Date.now().toString(),
              type: 'pipeline',
              content: pipelineContent,
              timestamp: new Date().toLocaleTimeString('en-US', { hour12: false })
            }
          ]);

          setPipelineTools(toolsList);
          setTimeout(() => runExecutionSimulation(toolsList, detectedMode, isPilotClarification ? attemptsCount : undefined), 1000);
        }
      }, 600);

      return;
    }

    // Standard message processing
    // Block input if a task is currently executing in this session
    if (executingSessionId === activeSession?.id && !replyingTo) {
      // Ignore new input during execution
      return;
    }

    const newMessage: Message = {
      id: Date.now().toString(),
      type: 'user',
      content: cmd,
      timestamp: new Date().toLocaleTimeString('en-US', { hour12: false })
    };

    let nextMessages = [...messages, newMessage];
    setExecutingSessionId(activeSession?.id || null); // Mark this session as executing

    // Simulate mapping keywords to tool highlighting
    let matchedTool = null;
      if (lowerCmd.includes('extract') || lowerCmd.includes('skill')) {
        matchedTool = '恶意skills识别';
      } else if (lowerCmd.includes('安全') || lowerCmd.includes('风险')) {
        matchedTool = '敏感信息泄露检测';
      } else if (lowerCmd.includes('分析')) {
        matchedTool = '关键原子步骤执行';
      }

    if (matchedTool) {
      setActiveTool(matchedTool);
    } else {
      setActiveTool(null);
    }

    if (detectedMode === 'PILOT') {
      const attemptQuestion = buildPilotAttemptClarificationMessage({
        sessionId: activeSession.id,
        command: cmd,
        timestamp: timestampNow(),
      }) as Message;
      setMessages([...nextMessages, attemptQuestion]);
      setReplyingTo(attemptQuestion.id);
      setHeaderStatus('PENDING');
      setExecutingSessionId(null);
      setInput('');
      return;
    }

    if (detectedMode === 'RUN') {
      setMessages(nextMessages);
      setInput('');
      startBackendRun(cmd, activeSession?.id);
      return;
    }

    if (detectedMode === 'SUBMIT') {
      const pendingPipeMsg = messages.find(m => m.type === 'pipeline_candidate' && m.pipelineData?.status === 'pending');
      
      if (pendingPipeMsg && pendingPipeMsg.pipelineData) {
        const pd = pendingPipeMsg.pipelineData;
        setMessages(prev => {
          const msgs = [...prev];
          const idx = msgs.findIndex(m => m.id === pendingPipeMsg.id);
          if (idx !== -1 && msgs[idx].pipelineData) {
            msgs[idx] = { ...msgs[idx], pipelineData: { ...msgs[idx].pipelineData!, status: 'stable' } };
          }
          return msgs;
        });

        setTimeout(() => {
          const candidatePipelineOutput = {
            id: pd.id,
            type: 'pipeline',
            score: pd.score,
            tools: pd.tools,
            config: {
              timeout: 30000,
              retries: 3,
              fallback: 'default_strategy'
            },
            generated_at: new Date().toISOString()
          };
          setCandidateJson(JSON.stringify(candidatePipelineOutput, null, 2));

          setMessages(prev => [
            ...prev,
            {
              id: Date.now().toString() + "-res",
              type: 'result',
              content: 'PILOT Execution Completed & Pipeline Submitted',
              timestamp: new Date().toLocaleTimeString('en-US', { hour12: false }),
              resultData: {
                score: pd.score,
                flaggedSamples: 0,
                approvedAssets: pd.tools.length,
                allFailed: false
              }
            }
          ]);
          setHeaderStatus('STABLE');
          setExecutingSessionId(null);
        }, 600);
      } else {
        setMessages(prev => [
          ...prev,
          {
            id: Date.now().toString(),
            type: 'system',
            content: 'No pending candidate pipeline found to submit.',
            timestamp: new Date().toLocaleTimeString('en-US', { hour12: false })
          }
        ]);
        setHeaderStatus('STABLE');
        setExecutingSessionId(null);
      }
    } else if (detectedMode === 'PILOT') {
      setTimeout(() => {
        setHeaderStatus('PENDING');
        // PILOT mode stays on the existing mock path for this slice.
        setMessages(prev => [
          ...prev,
          {
            id: (Date.now() + 2).toString(),
            type: 'clarification',
            content: '',
            question: 'PILOT 模式已开启，你希望执行几轮 attempt？（例如：3）',
            status: 'pending',
            index: 1,
            total: 1,
            timestamp: new Date().toLocaleTimeString('en-US', { hour12: false })
          }
        ]);
      }, 600);
    } else if (matchedTool) {
      // Simulate adding a clarification card after 1 second to feel like processing
      setTimeout(() => {
        setHeaderStatus('PENDING');
        setMessages(prev => [
          ...prev,
          {
            id: (Date.now() + 2).toString(),
            type: 'clarification',
            content: '',
            question: '你希望使用哪些checker_names？',
            status: 'pending',
            index: 1,
            total: 1,
            timestamp: new Date().toLocaleTimeString('en-US', { hour12: false })
          }
        ]);
      }, 600);
    } else {
      // No clarification needed!
      setTimeout(() => {
        setMessages(prev => [
          ...prev,
          {
            id: (Date.now() + 2).toString(),
            type: 'pipeline',
            content: `pipeline:\n  - name: "General Task"\n    tool: "Auto Processor"\n    params:\n      instruction: "${cmd}"`,
            timestamp: new Date().toLocaleTimeString('en-US', { hour12: false })
          }
        ]);
        setPipelineTools(['Auto Processor']);
        setTimeout(() => runExecutionSimulation(['Auto Processor'], detectedMode), 1000);
      }, 600);
    }

    setMessages(nextMessages);
    setInput('');
  };

  return (
    <div className="h-screen w-screen bg-[#020602] text-white font-mono flex flex-col overflow-hidden selection:bg-gray-700/30 selection:text-gray-200 shadow-[inset_0_0_100px_rgba(0,0,0,0.5)]">
      <style>{`
        .scrollbar-hide::-webkit-scrollbar { display: none; }
        .scrollbar-hide { -ms-overflow-style: none; scrollbar-width: none; }
        .custom-scrollbar::-webkit-scrollbar { width: 8px; }
        .custom-scrollbar::-webkit-scrollbar-track { background: rgba(0, 0, 0, 0.1); border-left: 1px solid rgba(100, 100, 100, 0.2); }
        .custom-scrollbar::-webkit-scrollbar-thumb { background: rgba(150, 150, 150, 0.4); }
        .custom-scrollbar::-webkit-scrollbar-thumb:hover { background: rgba(200, 200, 200, 0.6); }
      `}</style>
      
      <Header mode={mode} score={bestScore} sessionId={activeSession?.id} sessionName={activeSession?.name} status={headerStatus} />
      
      <div className="flex-1 flex overflow-hidden">
        <LeftSidebar 
          sessions={sessions} 
          onNewSession={handleNewSession}
          onSelectSession={handleSelectSession}
          onDeleteSession={handleDeleteSession}
        />
        
        <main className="flex-1 flex flex-col p-4 bg-black/90 relative z-0 min-w-0">
          <div className="absolute inset-0 z-[-1] opacity-5 pointer-events-none" 
               style={{
                 backgroundImage: 'linear-gradient(rgba(0, 255, 0, 0.2) 1px, transparent 1px), linear-gradient(90deg, rgba(0, 255, 0, 0.2) 1px, transparent 1px)',
                 backgroundSize: '30px 30px'
               }}
          ></div>

          {/* Chat / Messages Area */}
          <div className="flex-1 flex gap-4 overflow-hidden">
            {/* Left side - Active messages */}
            <div
              ref={scrollRef}
              className="flex-1 overflow-y-auto custom-scrollbar flex flex-col gap-4 pb-4 pr-2 scroll-smooth"
            >
              {messages.length === 0 ? (
                <div className="h-full border border-gray-700/30 bg-black/40 flex items-center justify-center flex-col gap-2 opacity-50 relative overflow-hidden">
                  <div className="flex items-center gap-2 text-xl mb-4">
                    <span className="text-gray-500">{'>'}</span>
                    <span className="w-3 h-5 bg-white animate-pulse"></span>
                  </div>
                  <span className="text-gray-500 text-sm uppercase tracking-widest text-center">
                    Awaiting Input Payload<br/>
                    Enter natural language or command (e.g. elf run "extract skills")
                  </span>
                </div>
              ) : (
                messages.map((msg) => {
                if (msg.type === 'clarification') {
                  return (
                    <div 
                      key={msg.id}
                      onClick={() => {
                        if (msg.status === 'pending') {
                          setReplyingTo(msg.id);
                        }
                      }}
                      className={`flex flex-col max-w-[85%] self-start border p-3 cursor-pointer shadow-sm transition-colors ${
                        msg.status === 'resolved' 
                          ? 'border-green-600/50 bg-[#001500]/60' 
                          : 'border-red-600/50 bg-[#1a0505]/80 hover:bg-[#250a0a]/90'
                      }`}
                    >
                      <div className="flex justify-between items-center text-[10px] uppercase font-bold tracking-widest mb-2 border-b pb-1 border-opacity-30 border-current">
                        <span className={msg.status === 'resolved' ? 'text-white' : 'text-red-500 flex items-center gap-2'}>
                          {msg.status === 'resolved' ? 'SYSTEM CLARIFIED' : (
                            <>
                              <span className="w-1.5 h-1.5 bg-red-500 rounded-full animate-pulse"></span>
                              SYSTEM NEEDS CLARIFICATION
                            </>
                          )}
                        </span>
                        <span className="text-gray-500">{msg.index}/{msg.total}</span>
                      </div>
                      
                      <div className={`text-sm mb-2 font-mono ${msg.status === 'resolved' ? 'text-gray-300' : 'text-red-400'}`}>
                        <div className="opacity-70 text-xs mb-1 uppercase tracking-widest">assistant_message:</div>
                        <div className="pl-2 border-l-2 border-current whitespace-pre-line">{msg.question}</div>
                      </div>

                      {msg.suggestions && msg.suggestions.length > 0 && msg.status === 'pending' && (
                        <div className="flex flex-wrap gap-2 mb-2">
                          {(msg.question?.includes('数据集') || msg.question?.includes('dataset')) && (
                            <button
                              onClick={(e) => {
                                e.stopPropagation();
                                setInput('default');
                                setReplyingTo(msg.id);
                                inputRef.current?.focus();
                              }}
                              className="text-xs px-3 py-1.5 border-2 border-yellow-500/70 bg-yellow-950/40 text-yellow-300 hover:bg-yellow-900/60 transition-colors font-bold"
                            >
                              ⭐ Use Default (推荐)
                            </button>
                          )}
                          {msg.suggestions.map((sug, idx) => (
                            <button
                              key={idx}
                              onClick={(e) => {
                                e.stopPropagation();
                                const isDatasetQ = msg.question?.includes('数据集') || msg.question?.includes('dataset');
                                if (isDatasetQ) {
                                  // For datasets, allow multiple selection
                                  const currentInput = input.trim();
                                  if (currentInput && !currentInput.toLowerCase().includes('default')) {
                                    // Append to existing input if not default
                                    const existingDatasets = currentInput.split(',').map(d => d.trim()).filter(d => d);
                                    if (!existingDatasets.includes(sug)) {
                                      setInput([...existingDatasets, sug].join(', '));
                                    }
                                  } else {
                                    setInput(sug);
                                  }
                                } else {
                                  setInput(sug);
                                }
                                setReplyingTo(msg.id);
                                inputRef.current?.focus();
                              }}
                              className="text-xs px-2 py-1 border border-red-600/50 bg-red-950/30 text-red-300 hover:bg-red-900/50 transition-colors"
                            >
                              {sug}
                            </button>
                          ))}
                        </div>
                      )}

                      <div className="mt-2 space-y-2">
                        <div className={`text-sm font-mono p-2 border ${msg.status === 'resolved' ? 'border-gray-700/50 bg-gray-900/40 text-gray-300' : 'border-red-900/50 bg-red-950/20 text-red-400/50'}`}>
                          <div className="opacity-70 text-xs mb-1 uppercase tracking-widest">user_reply:</div>
                          <div className={msg.status === 'resolved' ? '' : 'italic opacity-50'}>
                            {msg.status === 'resolved' ? msg.userReply : '<pending user input>'}
                          </div>
                        </div>
                      </div>

                      {msg.status === 'pending' && replyingTo === msg.id && (
                        <div className="text-xs text-red-400 animate-pulse mt-3 border-t border-red-900/40 pt-2 font-mono">
                          {`> Focus transferred. Enter your reply in the command line...`}
                        </div>
                      )}
                      {msg.status === 'pending' && replyingTo !== msg.id && (
                        <div className="text-[10px] text-red-500/60 mt-2 uppercase tracking-widest text-right">
                          [Click to Reply]
                        </div>
                      )}
                    </div>
                  );
                }

                if (msg.type === 'lifecycle' && msg.attempts) {
                  return (
                    <div key={msg.id} id={`msg-${msg.id}`} className="flex flex-col w-full items-start gap-4 mb-4 transition-all duration-500">
                      <div className="text-xs uppercase text-cyan-500 font-bold tracking-widest mb-1 flex items-center gap-2">
                        <span className="w-1.5 h-1.5 bg-cyan-500 rounded-full animate-pulse"></span>
                        CANDIDATE LIFECYCLE (PILOT MODE)
                      </div>
                      <div className="flex flex-col gap-6 w-full max-w-[95%]">
                        {msg.attempts.map((att, attIdx) => {
                          const expanded = expandedAttempts.has(att.id);
                          const toggleExpanded = () => {
                            setExpandedAttempts(prev => {
                              const next = new Set(prev);
                              if (next.has(att.id)) {
                                next.delete(att.id);
                              } else {
                                next.add(att.id);
                              }
                              return next;
                            });
                          };

                          return (
                          <div key={att.id} className="p-4 flex flex-col shadow-sm transition-colors border border-cyan-800/40 bg-[#001111]/80 text-cyan-400">
                            {/* Header */}
                            <div className="flex justify-between items-center text-sm font-bold mb-3 pb-3 border-b border-cyan-500/30 text-cyan-300">
                              <span className="flex items-center gap-2">
                                👉 Attempt {att.attemptNumber}/{att.totalAttempts}
                              </span>
                              <span className="text-xs font-mono opacity-70">Attempt ID: {att.id}</span>
                            </div>

                            {/* Separator */}
                            <div className="border-t border-gray-700/40 mb-3"></div>

                            {/* Planning Log */}
                            <div className="mb-3">
                              <div className="text-[10px] uppercase tracking-wider opacity-70 mb-1.5">Planning: {att.id}</div>
                              <pre className="text-[11px] font-mono leading-relaxed whitespace-pre-wrap text-gray-300">
                                {att.planningLog}
                              </pre>
                            </div>

                            {/* Pipeline/Derived Tool Log */}
                            <div className="mb-3">
                              <pre className="text-[11px] font-mono leading-relaxed whitespace-pre-wrap text-gray-300">
                                {att.pipelineLog}
                              </pre>
                            </div>

                            {/* DSL Code (Expandable) */}
                            <div className="mb-3">
                              <button
                                onClick={toggleExpanded}
                                className={cn(
                                  "w-full text-left px-3 py-2 border text-xs font-bold uppercase tracking-widest transition-colors",
                                  expanded ? "bg-gray-900/60 border-gray-600" : "bg-black/40 border-gray-700/40 hover:border-gray-600"
                                )}
                              >
                                🔧 {att.id} Pipeline DSL {expanded ? '[-]' : '[+]'}
                              </button>
                              {expanded && (
                                <pre className="mt-2 p-3 bg-black/60 border border-gray-700/40 text-[10px] font-mono leading-relaxed whitespace-pre-wrap text-green-300 overflow-x-auto">
                                  {att.dslCode}
                                </pre>
                              )}
                            </div>

                            {/* Metadata Grid */}
                            <div className="grid grid-cols-2 gap-4 text-xs font-mono mt-2 pt-3 border-t border-gray-700/30">
                              <div className="flex flex-col gap-1">
                                <span className="opacity-50 text-[9px] uppercase tracking-wider">Validation</span>
                                <span className={att.validation === 'passed' ? 'text-green-400 font-bold' : 'text-red-400'}>{att.validation.toUpperCase()}</span>
                              </div>
                              <div className="flex flex-col gap-1">
                                <span className="opacity-50 text-[9px] uppercase tracking-wider">Judge Score</span>
                                <span className={att.score > 80 ? 'text-green-400 font-bold' : 'text-yellow-400'}>{att.score.toFixed(1)}</span>
                              </div>
                            </div>

                          </div>
                        )})}
                      </div>
                    </div>
                  );
                }

                if (msg.type === 'pilot_summary' && msg.summaryData) {
                  const sd = msg.summaryData;
                  return (
                    <div key={msg.id} className="flex flex-col w-full items-start mb-4">
                      <div className="border-2 border-white bg-black/90 p-5 flex flex-col w-full max-w-[95%] shadow-lg">
                        {/* Header */}
                        <div className="flex items-center gap-3 text-lg font-bold mb-4 pb-3 border-b-2 border-white/30 text-white">
                          <span>🏆</span>
                          <span>Pilot Summary</span>
                        </div>

                        {/* Main Stats */}
                        <div className="grid grid-cols-2 gap-x-8 gap-y-3 text-sm font-mono mb-4 pb-4 border-b border-gray-700/40">
                          <div><span className="text-gray-500">🏆 Job ID:</span> <span className="text-cyan-400">{sd.jobId}</span></div>
                          <div><span className="text-gray-500">Pilot Status:</span> <span className="text-green-400">{sd.pilotStatus}</span></div>
                          <div><span className="text-gray-500">Attempts:</span> <span className="text-white">{sd.totalAttempts}</span></div>
                          <div><span className="text-gray-500">🏆 Best Attempt:</span> <span className="text-yellow-400">{sd.bestAttemptId}</span></div>
                          <div><span className="text-gray-500">Judge Score:</span> <span className="text-green-400">{sd.judgeScore.toFixed(2)}</span></div>
                          <div><span className="text-gray-500">Security Score:</span> <span className="text-green-400">{sd.securityScore.toFixed(1)}</span></div>
                          <div><span className="text-gray-500">Output Package:</span> <span className="text-white">shortlist=30 · exec_summary=ready</span></div>
                          <div><span className="text-gray-500">Derived Candidates:</span> <span className="text-cyan-400">{sd.derivedCandidates}</span></div>
                          <div><span className="text-gray-500">Approved Assets:</span> <span className="text-cyan-400">{sd.approvedAssets}</span></div>
                        </div>

                        {/* Summary Line */}
                        <div className="mb-4 pb-4 border-b border-gray-700/40">
                          <div className="text-xs text-white mb-2">
                            💡 <span className="text-gray-500">Summary:</span> best={sd.bestAttemptId} · final={sd.bestAttemptId} · candidates={sd.derivedCandidates} · approved={sd.approvedAssets}
                          </div>
                          <div className="text-xs text-white mb-1">
                            <span className="text-gray-500">Best vs First:</span> judge=+0.73 · security=n/a · latency=-15.84s
                          </div>
                          <div className="text-xs text-white">
                            <span className="text-gray-500">Best vs Final:</span> judge=0.0 · security=0.0 · latency=0.0
                          </div>
                        </div>

                        {/* Attempt Details */}
                        <div className="flex flex-col gap-4">
                          {sd.attemptSummaries.map((attSum, idx) => (
                            <div key={idx} className="border border-gray-700/40 bg-black/60 p-3">
                              <div className="text-sm font-bold text-cyan-400 mb-2">👉 {attSum.id}</div>
                              <div className="grid grid-cols-2 gap-x-6 gap-y-1.5 text-xs font-mono mb-2">
                                <div><span className="text-gray-500">Attempt Outcome:</span> <span className="text-white">action={attSum.action} · success={attSum.success ? 'True' : 'False'} · judge_score={attSum.judgeScore.toFixed(2)} · security_score={attSum.securityScore !== null ? attSum.securityScore.toFixed(1) : 'n/a'}</span></div>
                                <div><span className="text-gray-500">Attempt Metrics:</span> <span className="text-white">latency={attSum.latency.toFixed(2)}s · tools={attSum.tools} · derived={attSum.derived}</span></div>
                                {attSum.success && attSum.derived === 0 && (
                                  <div className="col-span-2"><span className="text-gray-500">Output Metrics:</span> <span className="text-white">shortlist=30 · exec_summary={idx === 1 ? 'draft' : 'ready'} · coverage={idx === 1 ? '3/4' : '4/4'}</span></div>
                                )}
                                {attSum.derived > 0 && (
                                  <>
                                    <div className="col-span-2"><span className="text-gray-500">Output Metrics:</span> <span className="text-white">shortlist=30 · exec_summary=ready · coverage=4/4</span></div>
                                    <div className="col-span-2"><span className="text-gray-500">Candidate:</span> <span className="text-yellow-400">cand_tool_job_7ae92b11 (experimental_python_tool)</span></div>
                                    <div className="col-span-2"><span className="text-gray-500">Candidate:</span> <span className="text-yellow-400">cand_pipe_job_7ae92b11 (pipeline)</span></div>
                                  </>
                                )}
                                <div className="col-span-2"><span className="text-gray-500">Next Step:</span> <span className="text-white">{attSum.nextStep} · failure_type={attSum.failureType}</span></div>
                                <div className="col-span-2"><span className="text-gray-500">Log Ref:</span> <span className="text-gray-400">.logs/{sd.jobId}.json</span></div>
                                {attSum.importantLog && (
                                  <div className="col-span-2"><span className="text-yellow-400">❓ Important Log:</span> <span className="text-gray-300">{attSum.importantLog}</span></div>
                                )}
                              </div>
                            </div>
                          ))}
                        </div>
                      </div>
                    </div>
                  );
                }

                if (msg.type === 'execution' && msg.executionSteps) {
                  return (
                    <div 
                      key={msg.id} 
                      className="flex flex-col w-full items-start"
                    >
                      <div className="border border-green-800/40 bg-[#001500]/80 p-3 flex flex-col min-w-[300px] max-w-[85%] shadow-sm text-green-400">
                        <div className="flex justify-between items-center text-[10px] uppercase font-bold tracking-widest mb-2 border-b pb-1 border-opacity-30 border-gray-500 gap-4">
                          <span className="flex items-center gap-2">
                            <span className="w-1.5 h-1.5 bg-white rounded-full animate-pulse"></span>
                            EXECUTION TIMELINE
                          </span>
                          <span className="font-mono text-[9px] opacity-70">{msg.timestamp}</span>
                        </div>
                        <div className="mt-4 flex flex-col w-full">
                          {msg.executionSteps.map((step, idx) => (
                            <div key={step.id} className="flex gap-3 text-xs font-mono relative pb-6 last:pb-0">
                              {/* Connector line */}
                              {idx < msg.executionSteps!.length - 1 && (
                                <div className="absolute left-[7px] top-[20px] w-[2px] h-[calc(100%-20px)] bg-gray-700/40"></div>
                              )}
                              
                              <div className="flex flex-col items-center z-10 pt-0.5">
                                <div className={`w-4 h-4 rounded-full flex items-center justify-center border text-[9px] font-bold ${
                                  step.status === 'success'
                                    ? 'bg-green-500 text-black border-green-500'
                                    : step.status === 'running'
                                      ? 'bg-gray-900/80 text-white border-white animate-pulse'
                                      : 'bg-transparent text-gray-500 border-gray-700'
                                }`}>
                                  {step.status === 'success' ? '✓' : idx + 1}
                                </div>
                              </div>
                              <div className="flex-1 flex flex-col">
                                <span className={`${
                                  step.status === 'success'
                                    ? 'text-gray-300'
                                    : step.status === 'running'
                                      ? 'text-white font-bold'
                                      : 'text-gray-500'
                                }`}>
                                  {step.name}
                                </span>
                                <span className={`text-[10px] mt-0.5 ${
                                  step.status === 'running' ? 'text-white animate-pulse' : 'text-gray-500'
                                }`}>
                                  {step.log}
                                </span>
                              </div>
                            </div>
                          ))}
                        </div>
                      </div>
                    </div>
                  );
                }

                if (msg.type === 'result' && msg.resultData) {
                  const failed = msg.resultData.allFailed;
                  return (
                    <div key={msg.id} className="flex flex-col w-full items-start">
                      <div className={cn(
                        "border p-4 flex flex-col w-full max-w-[85%] mt-2 mb-4 relative overflow-hidden transition-colors",
                        failed ? "border-red-500 bg-[#180000] text-red-400 shadow-[0_0_15px_rgba(255,0,0,0.3)] animate-[pulse_2s_ease-in-out_infinite]" : "border-white bg-black/80 text-white shadow-[0_0_15px_rgba(255,255,255,0.1)]"
                      )}>
                        <div className={cn(
                          "absolute top-0 left-0 w-full h-1 opacity-50",
                          failed ? "bg-gradient-to-r from-red-500 via-red-300 to-red-500" : "bg-gradient-to-r from-white via-gray-300 to-white"
                        )}></div>
                        <div className={cn(
                          "flex justify-between items-center text-sm uppercase font-bold tracking-widest mb-4 border-b pb-2",
                          failed ? "border-red-500/50" : "border-gray-500/50"
                        )}>
                          <span className={cn(
                            "flex items-center gap-2",
                            failed ? "text-red-300" : "text-white"
                          )}>
                            <span className={cn(
                              "w-2 h-2 rounded-full animate-pulse",
                              failed ? "bg-red-400 shadow-[0_0_8px_rgba(255,0,0,0.8)]" : "bg-white shadow-[0_0_8px_rgba(255,255,255,0.8)]"
                            )}></span>
                            EXECUTION RESULT ({msg.content})
                          </span>
                          <span className="font-mono text-[10px] opacity-70">{msg.timestamp}</span>
                        </div>
                        <div className="grid grid-cols-3 gap-6 text-sm font-mono mt-2">
                          <div className={cn("flex flex-col gap-2 p-3 bg-black/40 border rounded-sm", failed ? "border-red-900/50" : "border-gray-700/50")}>
                            <span className={cn("opacity-70 text-[10px] uppercase tracking-wider", failed ? "text-red-500" : "text-gray-400")}>Security Score</span>
                            <div className="flex items-end gap-1">
                              <span className={`text-3xl font-bold ${failed ? 'text-red-400' : (msg.resultData.score >= 90 ? 'text-green-400' : 'text-yellow-400')}`}>
                                {msg.resultData.score.toFixed(1)}
                              </span>
                              <span className="text-xs mb-1 opacity-50">/ 100</span>
                            </div>
                          </div>
                          <div className={cn("flex flex-col gap-2 p-3 bg-black/40 border rounded-sm", failed ? "border-red-900/50" : "border-gray-700/50")}>
                            <span className={cn("opacity-70 text-[10px] uppercase tracking-wider", failed ? "text-red-500" : "text-gray-400")}>Flagged Samples</span>
                            <div className="flex items-end gap-1">
                              <span className={`text-3xl font-bold ${msg.resultData.flaggedSamples > 0 ? 'text-red-400' : (failed ? 'text-red-400' : 'text-white')}`}>
                                {msg.resultData.flaggedSamples}
                              </span>
                              <span className="text-xs mb-1 opacity-50">items</span>
                            </div>
                          </div>
                          <div className={cn("flex flex-col gap-2 p-3 bg-black/40 border rounded-sm", failed ? "border-red-900/50" : "border-gray-700/50")}>
                            <span className={cn("opacity-70 text-[10px] uppercase tracking-wider", failed ? "text-red-500" : "text-gray-400")}>Approved Assets</span>
                            <div className="flex items-end gap-1">
                              <span className={cn("text-3xl font-bold", failed ? "text-red-400" : "text-cyan-400")}>
                                {msg.resultData.approvedAssets}
                              </span>
                              <span className="text-xs mb-1 opacity-50">items</span>
                            </div>
                          </div>
                        </div>
                      </div>
                    </div>
                  );
                }

                if (msg.type === 'pipeline') {
                  return (
                    <div 
                      key={msg.id} 
                      className="flex flex-col w-full items-start"
                    >
                      <div className="border border-green-800/40 bg-[#001500]/80 p-3 flex flex-col min-w-[300px] max-w-[85%] shadow-sm text-green-400">
                        <div className="flex justify-between items-center text-[10px] uppercase font-bold tracking-widest mb-2 border-b pb-1 border-opacity-30 border-gray-500 gap-4">
                          <span className="flex items-center gap-2">
                            <span className="w-1.5 h-1.5 bg-green-500 rounded-full"></span>
                            PIPELINE PLAN
                          </span>
                          <span className="font-mono text-[9px] opacity-70">{msg.timestamp}</span>
                        </div>
                        <pre className="text-xs leading-relaxed font-mono whitespace-pre-wrap mt-1 text-gray-300">
                          {msg.content}
                        </pre>
                      </div>
                    </div>
                  );
                }

                if (msg.type === 'pipeline_candidate' && msg.pipelineData) {
                  const pd = msg.pipelineData;
                  const isPending = pd.status === 'pending';
                  return (
                    <div 
                      key={msg.id} 
                      className="flex flex-col w-full items-start mb-4"
                      onClick={() => {
                        if (isPending && inputRef.current) {
                          setInput(`elf submit ${pd.id}`);
                          inputRef.current.focus();
                        }
                      }}
                    >
                      <div className={cn(
                        "p-4 flex flex-col min-w-[300px] max-w-[85%] shadow-sm transition-colors cursor-pointer border",
                        isPending 
                          ? "border-red-500 bg-red-950/40 text-red-200 animate-[pulse_2s_ease-in-out_infinite] shadow-[0_0_15px_rgba(255,0,0,0.3)]" 
                          : "border-green-800/40 bg-[#001500]/80 text-green-400 hover:border-green-600/50"
                      )}>
                        <div className={cn(
                          "flex justify-between items-center text-[10px] uppercase font-bold tracking-widest mb-3 border-b pb-2 gap-4",
                          isPending ? "border-red-500/50" : "border-gray-500/50"
                        )}>
                          <span className={cn("flex items-center gap-2", isPending && "text-red-400")}>
                            <span className={cn("w-2 h-2 rounded-full", isPending ? "bg-red-500 animate-pulse shadow-[0_0_8px_rgba(255,0,0,0.8)]" : "bg-green-500")}></span>
                            {msg.content}
                          </span>
                          <span className="font-mono text-[9px] opacity-70">{msg.timestamp}</span>
                        </div>
                        
                        <div className="grid grid-cols-2 gap-4 text-xs font-mono mt-2">
                          <div className="flex flex-col gap-1">
                            <span className="opacity-50 text-[10px] uppercase tracking-wider">Candidate ID</span>
                            <span className={isPending ? "text-red-300" : "text-cyan-400"}>{pd.id}</span>
                          </div>
                          <div className="flex flex-col gap-1">
                            <span className="opacity-50 text-[10px] uppercase tracking-wider">Projected Score</span>
                            <span className={isPending ? "text-red-300" : "text-yellow-400"}>{pd.score.toFixed(2)}%</span>
                          </div>
                          <div className="col-span-2 flex flex-col gap-1">
                            <span className="opacity-50 text-[10px] uppercase tracking-wider">Included Tools</span>
                            <div className="flex gap-2 flex-wrap mt-1">
                              {pd.tools.map((t, i) => (
                                <span key={i} className={cn(
                                  "px-2 py-1 bg-black/40 border text-[10px]",
                                  isPending ? "border-red-900/50" : "border-gray-700/50 text-gray-300"
                                )}>
                                  {t}
                                </span>
                              ))}
                            </div>
                          </div>
                        </div>

                        {isPending && (
                          <div className="mt-4 pt-3 border-t border-red-500/30 flex justify-center w-full">
                            <div className="px-4 py-1.5 bg-red-950 border border-red-500/50 text-red-400 text-xs font-bold uppercase tracking-widest hover:bg-red-900 transition-colors flex items-center gap-2">
                              <span className="w-1.5 h-1.5 bg-red-500 rounded-full animate-ping"></span>
                              CLICK TO SUBMIT
                            </div>
                          </div>
                        )}
                      </div>
                    </div>
                  );
                }

                if (msg.type === 'tool_candidate' && msg.toolData) {
                  const td = msg.toolData;
                  const isPending = td.status === 'pending';
                  return (
                    <div
                      key={msg.id}
                      className="flex flex-col w-full items-start mb-4"
                    >
                      <div className={cn(
                        "p-4 flex flex-col min-w-[300px] max-w-[85%] shadow-sm transition-colors border",
                        isPending
                          ? "border-red-500 bg-red-950/40 text-red-200 animate-[pulse_2s_ease-in-out_infinite] shadow-[0_0_15px_rgba(255,0,0,0.3)]"
                          : td.status === 'approved'
                            ? "border-green-500 bg-green-950/40 text-green-200"
                            : "border-gray-700 bg-gray-950/40 text-gray-400"
                      )}>
                        <div className={cn(
                          "flex justify-between items-center text-[10px] uppercase font-bold tracking-widest mb-3 border-b pb-2 gap-4",
                          isPending ? "border-red-500/50" : td.status === 'approved' ? "border-green-500/50" : "border-gray-500/50"
                        )}>
                          <span className={cn("flex items-center gap-2", isPending && "text-red-400")}>
                            <span className={cn("w-2 h-2 rounded-full",
                              isPending ? "bg-red-500 animate-pulse shadow-[0_0_8px_rgba(255,0,0,0.8)]" :
                              td.status === 'approved' ? "bg-green-500" : "bg-gray-500"
                            )}></span>
                            {msg.content}
                          </span>
                          <span className="font-mono text-[9px] opacity-70">{msg.timestamp}</span>
                        </div>

                        <div className="grid grid-cols-2 gap-4 text-xs font-mono mt-2">
                          <div className="flex flex-col gap-1">
                            <span className="opacity-50 text-[10px] uppercase tracking-wider">Tool ID</span>
                            <span className={isPending ? "text-red-300" : "text-cyan-400"}>{td.id}</span>
                          </div>
                          <div className="flex flex-col gap-1">
                            <span className="opacity-50 text-[10px] uppercase tracking-wider">Tool Name</span>
                            <span className={isPending ? "text-red-300" : "text-white"}>{td.name}</span>
                          </div>
                          <div className="flex flex-col gap-1">
                            <span className="opacity-50 text-[10px] uppercase tracking-wider">Projected Score</span>
                            <span className={isPending ? "text-red-300" : "text-yellow-400"}>{td.score.toFixed(2)}</span>
                          </div>
                          <div className="flex flex-col gap-1">
                            <span className="opacity-50 text-[10px] uppercase tracking-wider">Status</span>
                            <span className={cn(
                              "uppercase",
                              isPending ? "text-red-300" :
                              td.status === 'approved' ? "text-green-400" : "text-gray-400"
                            )}>{td.status}</span>
                          </div>
                          <div className="col-span-2 flex flex-col gap-1">
                            <span className="opacity-50 text-[10px] uppercase tracking-wider">Description</span>
                            <span className={isPending ? "text-red-300/80" : "text-gray-300/80 text-[11px]"}>{td.description}</span>
                          </div>
                        </div>

                        {isPending && (
                          <div className="mt-4 pt-3 border-t border-red-500/30 flex gap-3 w-full">
                            <button
                              onClick={() => {
                                // Approve tool
                                setMessages(prev => prev.map(m =>
                                  m.id === msg.id && m.toolData
                                    ? { ...m, toolData: { ...m.toolData, status: 'approved' } }
                                    : m
                                ));
                                const toolJson = {
                                  id: td.id,
                                  type: 'tool',
                                  name: td.name,
                                  description: td.description,
                                  score: td.score,
                                  status: 'approved',
                                  timestamp: new Date().toISOString()
                                };
                                setCandidateJson(JSON.stringify(toolJson, null, 2));
                                setHeaderStatus('STABLE');

                                // Add result card
                                setTimeout(() => {
                                  setMessages(prev => [
                                    ...prev,
                                    {
                                      id: Date.now().toString() + "-res",
                                      type: 'result',
                                      content: 'PILOT Execution Completed & Tool Approved',
                                      timestamp: new Date().toLocaleTimeString('en-US', { hour12: false }),
                                      resultData: {
                                        score: td.score,
                                        flaggedSamples: 0,
                                        approvedAssets: 1,
                                        allFailed: false
                                      }
                                    }
                                  ]);
                                  setExecutingSessionId(null);
                                }, 600);
                              }}
                              className="flex-1 bg-green-900/40 hover:bg-green-700 text-green-400 border border-green-600/50 py-2 text-xs font-bold uppercase tracking-widest transition-colors shadow-[0_0_10px_rgba(74,222,128,0.2)] hover:shadow-[0_0_15px_rgba(74,222,128,0.4)]"
                            >
                              Approve Tool
                            </button>
                            <button
                              onClick={() => {
                                // Reject tool
                                setMessages(prev => prev.map(m =>
                                  m.id === msg.id && m.toolData
                                    ? { ...m, toolData: { ...m.toolData, status: 'rejected' } }
                                    : m
                                ));
                                setHeaderStatus('STABLE');
                                // Add result card for rejection
                                setTimeout(() => {
                                  setMessages(prev => [
                                    ...prev,
                                    {
                                      id: Date.now().toString() + "-res",
                                      type: 'result',
                                      content: 'PILOT Execution Completed - Tool Rejected',
                                      timestamp: new Date().toLocaleTimeString('en-US', { hour12: false }),
                                      resultData: {
                                        score: td.score,
                                        flaggedSamples: 0,
                                        approvedAssets: 0,
                                        allFailed: true
                                      }
                                    }
                                  ]);
                                  setExecutingSessionId(null);
                                }, 600);
                              }}
                              className="flex-1 bg-red-900/40 hover:bg-red-800 text-red-400 border border-red-600/50 py-2 text-xs font-bold uppercase tracking-widest transition-colors shadow-[0_0_10px_rgba(248,113,113,0.2)] hover:shadow-[0_0_15px_rgba(248,113,113,0.4)]"
                            >
                              Reject Tool
                            </button>
                          </div>
                        )}
                      </div>
                    </div>
                  );
                }

                return (
                  <div 
                    key={msg.id} 
                    className="flex flex-col w-full items-start"
                  >
                    <div 
                      className={`border p-3 flex flex-col min-w-[200px] max-w-[85%] shadow-sm
                        ${msg.type === 'user' 
                          ? 'border-gray-700/40 bg-black/80 text-gray-400' 
                          : 'border-cyan-900/40 bg-[#001111]/60 text-cyan-400'
                        }`}
                    >
                      <div className={`flex justify-between items-center text-[10px] uppercase font-bold tracking-widest mb-2 border-b pb-1 border-opacity-30 border-current gap-4 ${
                        msg.type === 'user' ? 'text-gray-500' : 'text-cyan-600/70'
                      }`}>
                        <span>{msg.type === 'user' ? 'USER MESSAGE' : 'SYSTEM NOTICE'}</span>
                        <span className="font-mono text-[9px] opacity-70">{msg.timestamp}</span>
                      </div>
                      <div className="text-xs leading-relaxed break-words whitespace-pre-wrap font-mono">
                        {msg.content}
                      </div>
                    </div>
                  </div>
                );
              })
              )}
            </div>
          </div>

          {/* Command Line Input */}
          <form
            onSubmit={handleSubmit}
            className={`mt-2 border flex items-start shrink-0 shadow-inner overflow-hidden transition-colors focus-within:border-opacity-100 ${
              replyingTo ? 'border-red-600/60 bg-[#1a0505]/80 focus-within:border-red-500' : 'border-gray-700/60 bg-black/60 focus-within:border-white'
            }`}
          >
            <span className={`px-3 font-bold flex items-center pt-3 ${replyingTo ? 'text-red-500 bg-red-950/20' : 'text-white bg-gray-900/20'}`}>
              {replyingTo ? 'REPLY >' : '>'}
            </span>
            <textarea
              ref={inputRef}
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !e.shiftKey) {
                  e.preventDefault();
                  handleSubmit(e);
                }
              }}
              disabled={!activeSession || Boolean(activeSession.locked) || (executingSessionId === activeSession?.id && !replyingTo)}
              className={`flex-1 bg-transparent font-mono p-3 outline-none text-sm resize-none overflow-hidden min-h-[2.5rem] ${
                replyingTo ? 'text-red-400 placeholder:text-red-900/50' : 'text-white placeholder:text-gray-600'
              } ${(!activeSession || activeSession.locked || (executingSessionId === activeSession?.id && !replyingTo)) ? 'opacity-50 cursor-not-allowed' : ''}`}
              placeholder={
                activeSession?.locked
                  ? "Session finished. Create a new session to continue..."
                  : replyingTo
                    ? "Replying to clarification..."
                    : "Enter command (e.g. elf run \"extract skills\") or natural language..."
              }
              rows={1}
              autoFocus
            />
            <button
              type="submit"
              disabled={!activeSession || Boolean(activeSession.locked)}
              className={`px-4 text-xs uppercase tracking-widest py-3 font-bold transition-colors cursor-pointer border-l self-stretch ${
                replyingTo
                  ? 'text-red-500 hover:text-red-300 border-red-900/60 hover:bg-red-900/40'
                  : 'text-gray-400 hover:text-white border-gray-700/60 hover:bg-gray-900/40'
              }`}
            >
              Execute
            </button>
          </form>
        </main>
        
        <RightSidebar 
          activeTool={activeTool} 
          candidateTools={candidateTools}
          datasets={catalogDatasets}
          tools={catalogTools}
          onCandidateToolClick={(sessionId, messageId) => {
            handleSelectSession(sessionId);
            setTimeout(() => {
              const msgElement = document.getElementById(`msg-${messageId}`);
              if (msgElement) {
                msgElement.scrollIntoView({ behavior: 'smooth', block: 'center' });
                msgElement.classList.add('bg-gray-900/30', 'ring-2', 'ring-white');
                setTimeout(() => {
                  msgElement.classList.remove('bg-gray-900/30', 'ring-2', 'ring-white');
                }, 2000);
              }
            }, 100);
          }}
        />
      </div>

      {/* Resizable divider */}
      <div
        className={cn(
          "h-1 bg-gray-700/40 hover:bg-gray-600 transition-colors cursor-ns-resize border-t border-b border-gray-700/60 relative group",
          isDragging && "bg-white"
        )}
        onMouseDown={() => setIsDragging(true)}
      >
        <div className="absolute inset-0 flex items-center justify-center">
          <div className="w-12 h-0.5 bg-gray-600 group-hover:bg-gray-400 transition-colors"></div>
        </div>
      </div>

      <Footer
        pipelineTools={pipelineTools}
        logs={logs}
        attempts={attempts}
        candidateJson={candidateJson}
        runResultData={runResultData}
        height={footerHeight}
      />
    </div>
  );
}

function checkpointSuggestions(payload: any): string[] {
  return extractCheckpointSuggestions(payload);
}

function extractPipelineTools(pipeline: string): string[] {
  const tools = Array.from(pipeline.matchAll(/run_tool\(\s*["']([^"']+)["']/g))
    .map(match => match[1]);
  if (tools.length > 0) {
    return Array.from(new Set(tools));
  }
  if (pipeline.includes('load_dataset')) {
    return ['DataElf Pipeline'];
  }
  return ['Backend Run'];
}
