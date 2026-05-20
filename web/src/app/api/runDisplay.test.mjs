import assert from 'node:assert/strict';
import test from 'node:test';

import {
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
