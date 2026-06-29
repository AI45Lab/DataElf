import assert from 'node:assert/strict';
import test from 'node:test';

import {
  buildRunSummaryRows,
  buildRunCompletionResultMessage,
  buildRunFailureResultMessage,
  executionStepFromBackendLog,
  formatBackendLog,
  logsFromRunCompletion,
  mergeRunLogLines,
  normalizeRunResultData,
} from './runDisplay.js';

test('normalizes run completion details without dropping raw output', () => {
  const normalized = normalizeRunResultData({
    job_id: 'job_1',
    result: {
      security_score: 72,
      flagged_samples: 3,
      sample_results: [{ id: 1 }],
    },
    execution: {
      result: {
        security_score: 72,
        flagged_samples: 3,
        sample_results: [{ id: 1 }],
      },
      artifacts: { report: 'outputs/report.json' },
      metadata: { checker_count: 4 },
      logs: [{ step: 'step_1', level: 'INFO', message: 'ran' }],
      error: null,
    },
    clarification: { resolved_task: 'run audit on data' },
    capability_gap: {},
  });

  assert.equal(normalized.score, 72);
  assert.equal(normalized.flaggedSamples, 3);
  assert.equal(normalized.approvedAssets, 0);
  assert.deepEqual(normalized.rawResult.sample_results, [{ id: 1 }]);
  assert.deepEqual(normalized.artifacts, { report: 'outputs/report.json' });
  assert.deepEqual(normalized.metadata, { checker_count: 4 });
  assert.deepEqual(normalized.clarification, { resolved_task: 'run audit on data' });
  assert.deepEqual(normalized.logs, [{ step: 'step_1', level: 'INFO', message: 'ran' }]);
});

test('extracts and deduplicates run completion logs for footer display', () => {
  const completionLogs = logsFromRunCompletion({
    execution: {
      logs: [
        { timestamp: 't1', step: 'step_1', level: 'INFO', duration_ms: 0, message: 'load' },
        { timestamp: 't2', step: 'step_2', level: 'WARNING', duration_ms: 2, message: 'warn' },
      ],
    },
  });

  assert.deepEqual(completionLogs, [
    '[step_1 · 0ms] load',
    '❓ [step_2 · 2ms] warn',
  ]);
  assert.deepEqual(
    mergeRunLogLines(['[step_1 · 0ms] load'], completionLogs),
    ['[step_1 · 0ms] load', '❓ [step_2 · 2ms] warn'],
  );
});

test('formats stage logs and runtime logs like CLI output', () => {
  assert.equal(
    formatBackendLog({
      source: 'stage',
      level: 'INFO',
      message: 'Execution: Running pipeline',
    }),
    'Execution: Running pipeline',
  );
  assert.equal(
    formatBackendLog({
      source: 'stage',
      level: 'SUCCESS',
      message: 'Execution: status=success',
      icon: '✅',
    }),
    '✅ Execution: status=success',
  );
  assert.equal(
    formatBackendLog({
      step: 'job_end',
      level: 'INFO',
      duration_ms: 1,
      message: 'Job completed',
    }),
    '🏆 [job_end · 1ms] Job completed',
  );
  assert.equal(
    formatBackendLog({
      step: 'step_16',
      level: 'INFO',
      duration_ms: 0,
      message: 'Completed tool: security_audit',
    }),
    '✅ [step_16 · 0ms] Completed tool: security_audit',
  );
});

test('builds CLI-style summary rows from a run result object', () => {
  const rows = buildRunSummaryRows({
    security_score: 0.9778,
    flagged_samples: 1,
    safe_samples: 44,
    total_samples: 45,
    flagged_rate: 0.0222,
    risk_distribution: {
      pii: { total: 45, flagged: 1 },
    },
  });

  assert.deepEqual(rows.slice(0, 5), [
    { key: 'security_score', value: '0.9778', isNested: false },
    { key: 'flagged_samples', value: '1', isNested: false },
    { key: 'safe_samples', value: '44', isNested: false },
    { key: 'total_samples', value: '45', isNested: false },
    { key: 'flagged_rate', value: '0.0222', isNested: false },
  ]);
  assert.deepEqual(rows[5], {
    key: 'risk_distribution',
    value: '{\n  "pii": {\n    "total": 45,\n    "flagged": 1\n  }\n}',
    isNested: true,
  });
});

test('builds middle conversation result messages for completed and failed run jobs', () => {
  const resultData = {
    score: 97.78,
    flaggedSamples: 1,
    approvedAssets: 0,
    rawResult: { security_score: 0.9778 },
    jobId: 'job_1',
  };

  assert.deepEqual(
    buildRunCompletionResultMessage('job_1', resultData, '12:00:00'),
    {
      id: 'job_1-result',
      type: 'result',
      content: 'RUN Execution Completed',
      timestamp: '12:00:00',
      resultData,
    },
  );
  assert.deepEqual(
    buildRunFailureResultMessage('job_2', 'failed', '12:01:00'),
    {
      id: 'job_2-failed',
      type: 'result',
      content: 'RUN Execution Failed',
      timestamp: '12:01:00',
      resultData: {
        score: 0,
        flaggedSamples: 0,
        approvedAssets: 0,
        allFailed: true,
        rawResult: null,
        artifacts: {},
        metadata: {},
        clarification: {},
        capabilityGap: {},
        logs: [],
        error: 'failed',
        jobId: 'job_2',
      },
    },
  );
});

test('maps backend log entries into execution timeline steps', () => {
  assert.deepEqual(
    executionStepFromBackendLog({
      step: 'step_4',
      level: 'INFO',
      message: 'Running security audit',
    }),
    {
      id: 'runtime-step_4',
      name: 'step_4',
      status: 'running',
      log: 'Running security audit',
    },
  );
  assert.equal(
    executionStepFromBackendLog({
      step: 'step_16',
      level: 'ERROR',
      message: 'Pipeline failed',
    }).status,
    'error',
  );
  assert.equal(
    executionStepFromBackendLog({
      step: 'job_end',
      level: 'INFO',
      message: 'Job completed',
    }).status,
    'success',
  );
  assert.deepEqual(
    executionStepFromBackendLog({
      attempt_id: 'attempt_02',
      step: 'step_4',
      level: 'INFO',
      message: 'Running security audit',
    }),
    {
      id: 'runtime-attempt_02-step_4',
      name: 'attempt_02 step_4',
      status: 'running',
      log: 'Running security audit',
    },
  );
});
