export function normalizeRunResultData(event) {
  const execution = event?.execution && typeof event.execution === 'object'
    ? event.execution
    : {};
  const result = event?.result ?? execution.result ?? {};
  const metadata = execution.metadata || event?.metadata || {};
  const securityScore = Number(
    result?.security_score ??
    result?.score ??
    metadata?.security_score ??
    100
  );
  const flaggedSamples = Number(
    result?.flagged_samples ??
    result?.flagged_count ??
    result?.flaggedSamples ??
    metadata?.flagged_samples ??
    0
  );
  const approvedAssets = Array.isArray(result?.approved_assets)
    ? result.approved_assets.length
    : Number(result?.approved_assets ?? result?.approvedAssets ?? metadata?.approved_asset_count ?? 0);

  return {
    score: Number.isFinite(securityScore) ? securityScore : 100,
    flaggedSamples: Number.isFinite(flaggedSamples) ? flaggedSamples : 0,
    approvedAssets: Number.isFinite(approvedAssets) ? approvedAssets : 0,
    allFailed: event?.type === 'job.failed' || execution.success === false,
    rawResult: result,
    artifacts: execution.artifacts || event?.artifacts || {},
    metadata,
    clarification: event?.clarification || {},
    capabilityGap: event?.capability_gap || event?.capabilityGap || {},
    logs: Array.isArray(execution.logs) ? execution.logs : [],
    error: execution.error || event?.error || null,
    jobId: event?.job_id || event?.jobId || null,
  };
}

export function buildRunCompletionResultMessage(jobId, resultData, timestamp) {
  return {
    id: `${jobId}-result`,
    type: 'result',
    content: 'RUN Execution Completed',
    timestamp,
    resultData,
  };
}

export function buildRunFailureResultMessage(jobId, error, timestamp) {
  return {
    id: `${jobId}-failed`,
    type: 'result',
    content: 'RUN Execution Failed',
    timestamp,
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
      error,
      jobId,
    },
  };
}

export function buildRunSummaryRows(result) {
  if (!result || typeof result !== 'object' || Array.isArray(result)) {
    return [];
  }

  return Object.entries(result).map(([key, value]) => ({
    key,
    value: formatRunSummaryValue(value),
    isNested: isNestedRunSummaryValue(value),
  }));
}

function formatRunSummaryValue(value) {
  if (value === null || value === undefined) {
    return 'NA';
  }
  if (typeof value === 'string') {
    return value;
  }
  if (typeof value === 'number' || typeof value === 'boolean') {
    return String(value);
  }
  return JSON.stringify(value, null, 2);
}

function isNestedRunSummaryValue(value) {
  return Boolean(value && typeof value === 'object');
}

export function executionStepFromBackendLog(log) {
  if (!log || typeof log !== 'object') {
    return null;
  }
  const step = String(log.step || log.source || 'runtime');
  const attemptId = log.attempt_id ? String(log.attempt_id) : '';
  const level = String(log.level || '').toUpperCase();
  const message = String(log.message || '');
  const stepId = attemptId ? `${attemptId}-${step}` : step;
  return {
    id: `runtime-${stepId}`,
    name: attemptId ? `${attemptId} ${step}` : (step === 'stage' ? 'Execution' : step),
    status: backendLogStatus(level, step, message),
    log: message,
  };
}

function backendLogStatus(level, step, message) {
  const lowered = message.toLowerCase();
  if (level === 'ERROR' || level === 'CRITICAL' || lowered.includes('failed')) {
    return 'error';
  }
  if (
    step === 'job_end' ||
    level === 'SUCCESS' ||
    lowered.includes('completed') ||
    lowered.includes('status=success')
  ) {
    return 'success';
  }
  return 'running';
}

export function formatBackendLog(log) {
  if (!log || typeof log !== 'object') {
    return String(log || '');
  }
  const level = log.level || 'INFO';
  const message = log.message || '';
  const icon = log.icon || logIcon(log);
  const attemptPrefix = log.attempt_id ? `${log.attempt_id} ` : '';

  if (log.source === 'stage') {
    const line = icon ? `${icon} ${message}` : message;
    return `${attemptPrefix}${line}`.trim();
  }

  if (log.step) {
    const duration = Number.isFinite(Number(log.duration_ms))
      ? Number(log.duration_ms)
      : 0;
    const prefix = `${icon ? `${icon} ` : ''}[${log.step} · ${duration}ms]`;
    return `${attemptPrefix}${prefix} ${message}`.trim();
  }

  const timestamp = log.timestamp || new Date().toLocaleTimeString('en-US', { hour12: false });
  return `${attemptPrefix}[${timestamp}] [${level}] ${message}`.trim();
}

function logIcon(log) {
  const level = String(log.level || '').toUpperCase();
  const message = String(log.message || '').toLowerCase();
  const step = String(log.step || '');

  if (step === 'job_end') return '🏆';
  if (level === 'WARNING') return '❓';
  if (level === 'ERROR' || level === 'CRITICAL') return '❌';
  if (level === 'SUCCESS') return '✅';
  if (
    message.includes('completed') ||
    message.includes('success') ||
    message.includes('passed')
  ) {
    return '✅';
  }
  return '';
}

export function logsFromRunCompletion(event) {
  const logs = event?.execution?.logs;
  if (!Array.isArray(logs)) {
    return [];
  }
  return logs.map(formatBackendLog);
}

export function mergeRunLogLines(existing, incoming) {
  const seen = new Set(existing);
  const merged = [...existing];
  incoming.forEach((line) => {
    if (!seen.has(line)) {
      seen.add(line);
      merged.push(line);
    }
  });
  return merged;
}
