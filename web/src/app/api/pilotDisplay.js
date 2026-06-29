export function pilotLifecycleMessageId(jobId) {
  return `${jobId || 'pilot'}-lifecycle`;
}

export function buildPilotAttemptClarificationMessage({ sessionId, command, timestamp }) {
  return {
    id: `${sessionId || 'pilot'}-attempt-count`,
    type: 'clarification',
    content: '',
    question: 'PILOT 模式已开启，你希望执行几轮 attempt？（例如：3）',
    status: 'pending',
    suggestions: ['1', '2', '3'],
    index: 1,
    total: 1,
    checkpointType: 'pilot_attempt_count',
    pendingCommand: command,
    timestamp,
  };
}

export function parsePilotAttemptCount(reply) {
  const match = String(reply || '').trim().match(/\d+/);
  const value = match ? Number(match[0]) : NaN;
  if (!Number.isInteger(value) || value < 1 || value > 10) {
    return { ok: false, error: 'Attempt count must be between 1 and 10.' };
  }
  return { ok: true, value };
}

export function buildPilotLifecycleMessage(jobId, timestamp) {
  return {
    id: pilotLifecycleMessageId(jobId),
    type: 'lifecycle',
    content: '',
    timestamp,
    attempts: [],
  };
}

export function applyPilotEventToAttempts(existingAttempts, event) {
  const attemptId = event?.attempt_id;
  if (!attemptId) return existingAttempts || [];

  const attempts = [...(existingAttempts || [])];
  const attemptIndex = attempts.findIndex(attempt => attempt.id === attemptId);
  const current = attemptIndex >= 0
    ? { ...attempts[attemptIndex] }
    : buildInitialAttempt(event);
  const next = applyEventToAttempt(current, event);

  if (attemptIndex >= 0) {
    attempts[attemptIndex] = next;
  } else {
    attempts.push(next);
  }
  return attempts;
}

export function buildPilotAttemptsFromBackendAttempts(backendAttempts, totalAttempts) {
  return (backendAttempts || []).filter(Boolean).map((attempt, index) => {
    const attemptId = attempt.attempt_id || attempt.id || `attempt_${String(index + 1).padStart(2, '0')}`;
    const judge = attempt.judge || {};
    const action = attempt.action || {};
    const plannerLlm = attempt.planner_llm || {};
    const pipelineLlm = attempt.pipeline_llm || {};
    const execution = attempt.execution || {};
    const candidates = Array.isArray(attempt.candidates) ? attempt.candidates : [];
    const firstCandidate = candidates[0] || {};
    const goalSatisfied = Boolean(judge.goal_satisfied);
    const executionSucceeded = execution.success !== false;
    const elapsed = pipelineLlm.elapsed_seconds;
    const elapsedText = elapsed !== undefined && elapsed !== null ? ` elapsed=${elapsed}s` : '';

    return {
      id: attemptId,
      action_type: action.action_type || attempt.action_type || '',
      score: scoreToPercent(judge.score),
      status: goalSatisfied ? 'success' : (executionSucceeded ? 'failed' : 'failed'),
      validation: goalSatisfied ? 'passed' : 'failed',
      produced_candidate: candidates.length > 0,
      candidateApproval: candidates.length > 0 ? 'NEEDED' : 'NA',
      attemptNumber: attemptNumberFromId(attemptId, index),
      totalAttempts: Number(totalAttempts || backendAttempts.length || 1),
      planningLog: `${attemptId} Planner: status=${plannerLlm.status || 'unknown'} · model=${plannerLlm.model || 'n/a'} · action=${action.action_type || attempt.action_type || ''}`,
      pipelineLog: `${attemptId} Pipeline: status=${pipelineLlm.status || 'unknown'} · model=${pipelineLlm.model || 'n/a'}${elapsedText}`,
      dslCode: attempt.pipeline || '',
      model: pipelineLlm.model || plannerLlm.model || '',
      elapsed: Number(elapsed || 0),
      derivedToolName: firstCandidate.name || '',
      derivedToolType: firstCandidate.candidate_type || '',
      result: {
        goal_satisfied: goalSatisfied,
        score: Number(judge.score || 0),
        failure_type: judge.failure_type || '',
        recommended_next_action: judge.recommended_next_action || '',
        capability_gap: judge.capability_gap || {},
        reason: judge.reason || '',
      },
    };
  });
}

export function buildPilotSummaryMessage(event, timestamp) {
  const summary = event?.pilot_summary || {};
  const attempts = Array.isArray(event?.attempts) ? event.attempts : [];
  const bestAttempt = event?.best_attempt || attempts.find(
    attempt => attempt?.attempt_id === summary.best_attempt_id,
  ) || attempts[attempts.length - 1] || {};
  const judge = bestAttempt?.judge || event?.judge || {};
  const metrics = bestAttempt?.attempt_metrics || {};
  const domainMetrics = judge?.domain_metrics || {};
  const candidateIds = summary.candidate_ids || event?.candidate_asset_ids || [];
  const approvedAssetIds = event?.approved_asset_ids || [];

  return {
    id: `${event?.job_id || 'pilot'}-pilot-summary`,
    type: 'pilot_summary',
    content: 'PILOT Execution Summary',
    timestamp,
    summaryData: {
      jobId: event?.job_id || '',
      pilotStatus: event?.status || 'unknown',
      totalAttempts: Number(summary.attempt_count || attempts.length || 0),
      bestAttemptId: summary.best_attempt_id || bestAttempt?.attempt_id || '',
      judgeScore: Number(judge?.score || 0),
      securityScore: Number(
        metrics.security_score ??
        domainMetrics.security_score ??
        0
      ),
      derivedCandidates: Array.isArray(candidateIds) ? candidateIds.length : 0,
      approvedAssets: Array.isArray(approvedAssetIds) ? approvedAssetIds.length : 0,
      attemptSummaries: buildAttemptSummaries(attempts.length ? attempts : [bestAttempt]),
    },
  };
}

export function buildPilotCompletionResultMessage(event, timestamp) {
  const bestAttempt = event?.best_attempt || {};
  const judge = bestAttempt?.judge || event?.judge || {};
  const metrics = bestAttempt?.attempt_metrics || {};
  const domainMetrics = judge?.domain_metrics || {};
  const approvedAssetIds = event?.approved_asset_ids || [];
  const score = scoreToPercent(judge?.score ?? event?.final_score ?? 0);

  return {
    id: `${event?.job_id || 'pilot'}-result`,
    type: 'result',
    content: 'PILOT Execution Completed',
    timestamp,
    resultData: {
      score,
      flaggedSamples: Number(
        metrics.flagged_samples ??
        domainMetrics.flagged_samples ??
        event?.result?.flagged_samples ??
        0
      ),
      approvedAssets: Array.isArray(approvedAssetIds) ? approvedAssetIds.length : 0,
      allFailed: event?.type === 'job.failed' || event?.status !== 'success',
      rawResult: event?.result ?? null,
      artifacts: event?.execution?.artifacts || {},
      metadata: event?.execution?.metadata || {},
      clarification: event?.clarification || {},
      capabilityGap: event?.capability_gap || {},
      logs: Array.isArray(event?.execution?.logs) ? event.execution.logs : [],
      error: event?.error || null,
      jobId: event?.job_id || null,
    },
  };
}

export function buildRejectedPilotToolCandidateMessage(event, timestamp) {
  const bestAttempt = event?.best_attempt || {};
  const candidates = Array.isArray(bestAttempt?.candidates) ? bestAttempt.candidates : [];
  const candidate = candidates[0] || {};
  const score = scoreToPercent(
    bestAttempt?.judge?.score ??
    event?.final_score ??
    0
  );
  return {
    id: `${event?.job_id || 'pilot'}-tool-candidate`,
    type: 'tool_candidate',
    content: 'Candidate Tool Generated',
    timestamp,
    toolData: {
      id: candidate.id || candidate.candidate_id || `${event?.job_id || 'pilot'}-mock-derived-tool`,
      name: candidate.name || bestAttempt?.derived_tool_name || 'policy_safe_semantic_risk_audit',
      description: 'Mocked derived tool candidate from pilot mode. Default decision: rejected.',
      status: 'rejected',
      score,
    },
  };
}

export function pilotCheckpointAnswerPayload(message, reply) {
  const answer = String(reply || '').trim();
  const normalized = answer.toLowerCase();
  if (message?.checkpointType === 'candidate_approval') {
    const decision = ['approve', 'reject', 'continue'].includes(normalized)
      ? normalized
      : 'continue';
    return { decision, answer, approved: decision === 'approve' };
  }
  if (message?.checkpointType === 'write_approval') {
    if (['allow', 'approve', 'yes', 'y', 'ok', 'okay'].includes(normalized)) {
      return { decision: 'allow', answer, approved: true };
    }
    if (['deny', 'reject', 'no', 'n'].includes(normalized)) {
      return { decision: 'deny', answer, approved: false };
    }
    return { decision: 'answer', answer, approved: false };
  }
  return {
    decision: 'answer',
    answer,
    approved: /^(allow|approve|yes|y|ok|okay|好|可以|是)$/i.test(answer),
  };
}

function buildInitialAttempt(event) {
  return {
    id: event.attempt_id,
    action_type: '',
    score: 0,
    status: 'running',
    validation: 'pending',
    produced_candidate: false,
    candidateApproval: 'NA',
    attemptNumber: Number(event.index || 1),
    totalAttempts: Number(event.budget_steps || 1),
    planningLog: '',
    pipelineLog: '',
    dslCode: '',
    model: '',
    elapsed: 0,
    result: undefined,
  };
}

function attemptNumberFromId(attemptId, fallbackIndex) {
  const match = String(attemptId || '').match(/(\d+)$/);
  return match ? Number(match[1]) : fallbackIndex + 1;
}

function applyEventToAttempt(attempt, event) {
  if (event.type === 'pilot.attempt_started') {
    return {
      ...attempt,
      attemptNumber: Number(event.index || attempt.attemptNumber || 1),
      totalAttempts: Number(event.budget_steps || attempt.totalAttempts || 1),
      status: 'running',
    };
  }
  if (event.type === 'pilot.planner') {
    const llm = event.llm || {};
    const actionType = event.action?.action_type || '';
    return {
      ...attempt,
      action_type: actionType,
      model: llm.model || attempt.model,
      planningLog: `${event.attempt_id} Planner: status=${llm.status || 'unknown'} · model=${llm.model || 'n/a'} · action=${actionType}`,
    };
  }
  if (event.type === 'pilot.pipeline') {
    const llm = event.llm || {};
    const elapsed = llm.elapsed_seconds;
    const elapsedText = elapsed !== undefined && elapsed !== null ? ` elapsed=${elapsed}s` : '';
    return {
      ...attempt,
      model: llm.model || attempt.model,
      elapsed: Number(elapsed || attempt.elapsed || 0),
      pipelineLog: `${event.attempt_id} Pipeline: status=${llm.status || 'unknown'} · model=${llm.model || 'n/a'}${elapsedText}`,
      dslCode: event.pipeline || '',
    };
  }
  if (event.type === 'pilot.judge') {
    const judge = event.judge || {};
    const goalSatisfied = Boolean(judge.goal_satisfied);
    return {
      ...attempt,
      score: scoreToPercent(judge.score),
      status: goalSatisfied ? 'success' : 'failed',
      validation: goalSatisfied ? 'passed' : 'failed',
      result: {
        goal_satisfied: goalSatisfied,
        score: Number(judge.score || 0),
        failure_type: judge.failure_type || '',
        recommended_next_action: judge.recommended_next_action || '',
        capability_gap: judge.capability_gap || {},
        reason: judge.reason || '',
      },
    };
  }
  if (event.type === 'pilot.candidate_saved') {
    const candidate = event.candidate || {};
    return {
      ...attempt,
      produced_candidate: true,
      candidateApproval: 'NEEDED',
      derivedToolName: candidate.name || attempt.derivedToolName,
      derivedToolType: candidate.candidate_type || attempt.derivedToolType,
    };
  }
  return attempt;
}

function buildAttemptSummaries(attempts) {
  return (attempts || []).filter(Boolean).map((attempt) => {
    const judge = attempt.judge || {};
    const metrics = attempt.attempt_metrics || {};
    const domainMetrics = judge.domain_metrics || {};
    const candidates = Array.isArray(attempt.candidates) ? attempt.candidates : [];
    return {
      id: attempt.attempt_id || attempt.id || '',
      action: attempt.action?.action_type || attempt.action_type || '',
      success: Boolean(judge.goal_satisfied || attempt.execution?.success),
      judgeScore: Number(judge.score || attempt.result?.score || 0),
      securityScore: nullableNumber(metrics.security_score ?? domainMetrics.security_score),
      latency: Number(metrics.total_attempt_latency_s || metrics.attempt_total_latency_s || 0),
      tools: Number(metrics.tool_count || metrics.tools || 0),
      derived: Number(metrics.derived_candidate_count || candidates.length || 0),
      nextStep: judge.recommended_next_action || attempt.result?.recommended_next_action || '',
      failureType: judge.failure_type || attempt.result?.failure_type || '',
      importantLog: importantLogLine(attempt),
    };
  });
}

function importantLogLine(attempt) {
  const excerpt = attempt.execution_log_excerpt || attempt.execution?.log_excerpt || [];
  const first = Array.isArray(excerpt) ? excerpt[0] : null;
  if (!first) return '';
  return `${first.level || 'INFO'} · ${first.step || 'runtime'} · ${first.message || ''}`;
}

function nullableNumber(value) {
  if (value === null || value === undefined || value === '') return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function scoreToPercent(value) {
  const score = Number(value || 0);
  if (!Number.isFinite(score)) return 0;
  return score <= 1 ? score * 100 : score;
}
