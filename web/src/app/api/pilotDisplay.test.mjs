import assert from 'node:assert/strict';
import test from 'node:test';

import {
  applyPilotEventToAttempts,
  buildPilotAttemptsFromBackendAttempts,
  buildPilotAttemptClarificationMessage,
  buildPilotCompletionResultMessage,
  buildRejectedPilotToolCandidateMessage,
  buildPilotSummaryMessage,
  pilotCheckpointAnswerPayload,
  parsePilotAttemptCount,
} from './pilotDisplay.js';

test('builds and parses the local pilot attempt-count clarification', () => {
  const message = buildPilotAttemptClarificationMessage({
    sessionId: 'sess_1',
    command: 'run security_audit on security_audit_samples',
    timestamp: '12:00:00',
  });

  assert.equal(message.type, 'clarification');
  assert.equal(message.checkpointType, 'pilot_attempt_count');
  assert.equal(message.question, 'PILOT 模式已开启，你希望执行几轮 attempt？（例如：3）');
  assert.equal(message.pendingCommand, 'run security_audit on security_audit_samples');
  assert.deepEqual(message.suggestions, ['1', '2', '3']);
  assert.deepEqual(parsePilotAttemptCount('3'), { ok: true, value: 3 });
  assert.deepEqual(parsePilotAttemptCount('0'), { ok: false, error: 'Attempt count must be between 1 and 10.' });
});

test('builds a lifecycle attempt from real pilot events', () => {
  let attempts = [];
  attempts = applyPilotEventToAttempts(attempts, {
    type: 'pilot.attempt_started',
    job_id: 'job_1',
    attempt_id: 'attempt_01',
    index: 1,
    budget_steps: 3,
  });
  attempts = applyPilotEventToAttempts(attempts, {
    type: 'pilot.planner',
    attempt_id: 'attempt_01',
    action: { action_type: 'propose_pipeline' },
    llm: { status: 'success', model: 'fake-model' },
  });
  attempts = applyPilotEventToAttempts(attempts, {
    type: 'pilot.pipeline',
    attempt_id: 'attempt_01',
    pipeline: 'save_result({"ok": true})',
    llm: { status: 'success', model: 'fake-model', elapsed_seconds: 0.2 },
  });
  attempts = applyPilotEventToAttempts(attempts, {
    type: 'pilot.judge',
    attempt_id: 'attempt_01',
    judge: {
      goal_satisfied: true,
      score: 0.91,
      failure_type: 'none',
      recommended_next_action: 'stop_success',
    },
  });

  assert.equal(attempts.length, 1);
  assert.equal(attempts[0].id, 'attempt_01');
  assert.equal(attempts[0].action_type, 'propose_pipeline');
  assert.equal(attempts[0].score, 91);
  assert.equal(attempts[0].validation, 'passed');
  assert.match(attempts[0].planningLog, /action=propose_pipeline/);
  assert.match(attempts[0].pipelineLog, /elapsed=0.2s/);
  assert.equal(attempts[0].dslCode, 'save_result({"ok": true})');
});

test('builds lifecycle attempts from backend polling attempts', () => {
  const attempts = buildPilotAttemptsFromBackendAttempts([
    {
      attempt_id: 'attempt_01',
      action: { action_type: 'propose_pipeline' },
      pipeline: 'save_result({"ok": true})',
      planner_llm: { status: 'success', model: 'fake-planner' },
      pipeline_llm: { status: 'success', model: 'fake-pipeline', elapsed_seconds: 0.4 },
      judge: {
        goal_satisfied: false,
        score: 0.41,
        failure_type: 'insufficient_security_coverage',
        recommended_next_action: 'mutate_pipeline',
        reason: 'Needs broader coverage.',
      },
      execution: { success: true, result: { ok: true } },
      candidates: [{ candidate_id: 'cand_1', name: 'candidate_one' }],
    },
  ], 3);

  assert.equal(attempts.length, 1);
  assert.deepEqual(attempts[0], {
    id: 'attempt_01',
    action_type: 'propose_pipeline',
    score: 41,
    status: 'failed',
    validation: 'failed',
    produced_candidate: true,
    candidateApproval: 'NEEDED',
    attemptNumber: 1,
    totalAttempts: 3,
    planningLog: 'attempt_01 Planner: status=success · model=fake-planner · action=propose_pipeline',
    pipelineLog: 'attempt_01 Pipeline: status=success · model=fake-pipeline elapsed=0.4s',
    dslCode: 'save_result({"ok": true})',
    model: 'fake-pipeline',
    elapsed: 0.4,
    derivedToolName: 'candidate_one',
    derivedToolType: '',
    result: {
      goal_satisfied: false,
      score: 0.41,
      failure_type: 'insufficient_security_coverage',
      recommended_next_action: 'mutate_pipeline',
      capability_gap: {},
      reason: 'Needs broader coverage.',
    },
  });
});

test('builds pilot summary and result messages from completion event', () => {
  const event = {
    job_id: 'job_1',
    status: 'success',
    result: { ok: true },
    best_attempt: {
      attempt_id: 'attempt_01',
      judge: {
        score: 0.91,
        goal_satisfied: true,
        failure_type: 'none',
        recommended_next_action: 'stop_success',
      },
      attempt_metrics: { security_score: 62, flagged_samples: 3 },
    },
    pilot_summary: {
      attempt_count: 1,
      best_attempt_id: 'attempt_01',
      final_attempt_id: 'attempt_01',
      candidate_ids: ['cand_pipe_1'],
    },
    approved_asset_ids: ['asset_1'],
  };

  const summary = buildPilotSummaryMessage(event, '12:00:00');
  const result = buildPilotCompletionResultMessage(event, '12:00:01');

  assert.equal(summary.type, 'pilot_summary');
  assert.equal(summary.summaryData.judgeScore, 0.91);
  assert.equal(summary.summaryData.securityScore, 62);
  assert.equal(summary.summaryData.derivedCandidates, 1);
  assert.equal(summary.summaryData.approvedAssets, 1);
  assert.equal(result.type, 'result');
  assert.equal(result.resultData.score, 91);
  assert.equal(result.resultData.flaggedSamples, 3);
  assert.equal(result.resultData.approvedAssets, 1);
});

test('builds a default rejected candidate tool card for pilot completion', () => {
  const message = buildRejectedPilotToolCandidateMessage({
    job_id: 'job_1',
    best_attempt: {
      attempt_id: 'attempt_03',
      action: { action_type: 'derive_python_tool_draft' },
      candidates: [{ id: 'cand_tool_1', name: 'policy_safe_semantic_risk_audit' }],
      judge: { score: 0.88 },
    },
  }, '12:00:02');

  assert.equal(message.id, 'job_1-tool-candidate');
  assert.equal(message.type, 'tool_candidate');
  assert.equal(message.content, 'Candidate Tool Generated');
  assert.deepEqual(message.toolData, {
    id: 'cand_tool_1',
    name: 'policy_safe_semantic_risk_audit',
    description: 'Mocked derived tool candidate from pilot mode. Default decision: rejected.',
    status: 'rejected',
    score: 88,
  });
});

test('maps pilot approval checkpoints to controller decisions', () => {
  assert.deepEqual(
    pilotCheckpointAnswerPayload({ checkpointType: 'candidate_approval' }, 'approve'),
    { decision: 'approve', answer: 'approve', approved: true },
  );
  assert.deepEqual(
    pilotCheckpointAnswerPayload({ checkpointType: 'candidate_approval' }, 'continue'),
    { decision: 'continue', answer: 'continue', approved: false },
  );
  assert.deepEqual(
    pilotCheckpointAnswerPayload({ checkpointType: 'write_approval' }, 'allow'),
    { decision: 'allow', answer: 'allow', approved: true },
  );
  assert.deepEqual(
    pilotCheckpointAnswerPayload({ checkpointType: 'goal_clarification' }, 'use defaults'),
    { decision: 'answer', answer: 'use defaults', approved: false },
  );
});
