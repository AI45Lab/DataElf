const DEFAULT_API_BASE_URL = 'http://127.0.0.1:8001';

export async function createRun(command, sessionId) {
  const response = await fetch(`${apiBaseUrl()}/api/v1/runs`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ command, session_id: sessionId }),
  });
  if (!response.ok) {
    throw new Error(await response.text());
  }
  return response.json();
}

export async function listSessions() {
  const response = await fetch(`${apiBaseUrl()}/api/v1/sessions`);
  if (!response.ok) {
    throw new Error(await response.text());
  }
  const payload = await response.json();
  return Array.isArray(payload.sessions) ? payload.sessions : [];
}

export async function createSession(payload = {}) {
  const response = await fetch(`${apiBaseUrl()}/api/v1/sessions`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    throw new Error(await response.text());
  }
  return response.json();
}

export async function getSession(sessionId) {
  const response = await fetch(
    `${apiBaseUrl()}/api/v1/sessions/${encodeURIComponent(sessionId)}`,
  );
  if (!response.ok) {
    throw new Error(await response.text());
  }
  return response.json();
}

export async function updateSession(sessionId, payload = {}) {
  const response = await fetch(
    `${apiBaseUrl()}/api/v1/sessions/${encodeURIComponent(sessionId)}`,
    {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    },
  );
  if (!response.ok) {
    throw new Error(await response.text());
  }
  return response.json();
}

export async function deleteSession(sessionId) {
  const response = await fetch(
    `${apiBaseUrl()}/api/v1/sessions/${encodeURIComponent(sessionId)}`,
    { method: 'DELETE' },
  );
  if (!response.ok) {
    throw new Error(await response.text());
  }
  return response.json();
}

export async function setSessionMode(sessionId, mode) {
  const response = await fetch(
    `${apiBaseUrl()}/api/v1/sessions/${encodeURIComponent(sessionId)}/mode`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode }),
    },
  );
  if (!response.ok) {
    throw new Error(await response.text());
  }
  return response.json();
}

export async function saveSessionSnapshot(sessionId, snapshot) {
  const response = await fetch(
    `${apiBaseUrl()}/api/v1/sessions/${encodeURIComponent(sessionId)}/snapshot`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ snapshot }),
    },
  );
  if (!response.ok) {
    throw new Error(await response.text());
  }
  return response.json();
}

export async function createSessionRun(sessionId, command, options = {}) {
  const payload = { command };
  if (options.budgetSteps !== undefined && options.budgetSteps !== null) {
    payload.budget_steps = options.budgetSteps;
  }
  const response = await fetch(
    `${apiBaseUrl()}/api/v1/sessions/${encodeURIComponent(sessionId)}/runs`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    },
  );
  if (!response.ok) {
    throw new Error(await response.text());
  }
  return response.json();
}

export async function answerCheckpoint(jobId, checkpointId, answer) {
  const response = await fetch(
    `${apiBaseUrl()}/api/v1/jobs/${encodeURIComponent(jobId)}/checkpoints/${encodeURIComponent(checkpointId)}/answer`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(answer),
    },
  );
  if (!response.ok) {
    throw new Error(await response.text());
  }
  return response.json();
}

export async function fetchJob(jobId) {
  const response = await fetch(
    `${apiBaseUrl()}/api/v1/jobs/${encodeURIComponent(jobId)}`,
  );
  if (!response.ok) {
    throw new Error(await response.text());
  }
  return response.json();
}

export async function replayRunEvents(jobId, afterEventId = null) {
  const query = afterEventId !== null && afterEventId !== undefined
    ? `?after_event_id=${encodeURIComponent(String(afterEventId))}`
    : '';
  const response = await fetch(
    `${apiBaseUrl()}/api/v1/jobs/${encodeURIComponent(jobId)}/events/replay${query}`,
  );
  if (!response.ok) {
    throw new Error(await response.text());
  }
  const payload = await response.json();
  return Array.isArray(payload.events) ? payload.events : [];
}

export async function listDatasets() {
  const response = await fetch(`${apiBaseUrl()}/api/v1/datasets`);
  if (!response.ok) {
    throw new Error(await response.text());
  }
  const payload = await response.json();
  return Array.isArray(payload.datasets) ? payload.datasets : [];
}

export async function listTools() {
  const response = await fetch(`${apiBaseUrl()}/api/v1/tools`);
  if (!response.ok) {
    throw new Error(await response.text());
  }
  const payload = await response.json();
  return Array.isArray(payload.tools) ? payload.tools : [];
}

export function subscribeRunEvents(jobId, handlers) {
  const source = new EventSource(
    `${apiBaseUrl()}/api/v1/jobs/${encodeURIComponent(jobId)}/events`,
  );
  source.onmessage = (message) => dispatchEventPayload(message, handlers);
  source.addEventListener('job.created', (message) => dispatchEventPayload(message, handlers));
  source.addEventListener('job.running', (message) => dispatchEventPayload(message, handlers));
  source.addEventListener('checkpoint.created', (message) => dispatchEventPayload(message, handlers));
  source.addEventListener('checkpoint.resolved', (message) => dispatchEventPayload(message, handlers));
  source.addEventListener('pipeline.generated', (message) => dispatchEventPayload(message, handlers));
  source.addEventListener('backend.stage_started', (message) => dispatchEventPayload(message, handlers));
  source.addEventListener('backend.stage_completed', (message) => dispatchEventPayload(message, handlers));
  source.addEventListener('backend.checkpoint_paused', (message) => dispatchEventPayload(message, handlers));
  source.addEventListener('backend.checkpoint_resolved', (message) => dispatchEventPayload(message, handlers));
  source.addEventListener('pilot.attempt_started', (message) => dispatchEventPayload(message, handlers));
  source.addEventListener('pilot.planner', (message) => dispatchEventPayload(message, handlers));
  source.addEventListener('pilot.pipeline', (message) => dispatchEventPayload(message, handlers));
  source.addEventListener('pilot.judge', (message) => dispatchEventPayload(message, handlers));
  source.addEventListener('pilot.candidate_saved', (message) => dispatchEventPayload(message, handlers));
  source.addEventListener('pilot.candidate_validated', (message) => dispatchEventPayload(message, handlers));
  source.addEventListener('pilot.candidate_error', (message) => dispatchEventPayload(message, handlers));
  source.addEventListener('log.appended', (message) => dispatchEventPayload(message, handlers));
  source.addEventListener('job.completed', (message) => dispatchEventPayload(message, handlers));
  source.addEventListener('job.failed', (message) => dispatchEventPayload(message, handlers));
  source.onerror = (error) => {
    handlers.onError?.(error);
  };
  return source;
}

export function shouldUseRunStatusPollingFallback(streamReadyState) {
  return streamReadyState !== 1;
}

function dispatchEventPayload(message, handlers) {
  try {
    handlers.onEvent?.(JSON.parse(message.data));
  } catch (error) {
    handlers.onError?.(error);
  }
}

export function getDataElfApiBaseUrl() {
  return import.meta.env?.VITE_DATAELF_API_BASE_URL || DEFAULT_API_BASE_URL;
}

export function extractCheckpointSuggestions(payload) {
  const suggestions = [];
  if (Array.isArray(payload?.options)) {
    suggestions.push(...payload.options.map((item) => String(item)));
  }
  if (Array.isArray(payload?.datasets)) {
    suggestions.push(...payload.datasets.map((item) => String(item)));
  }
  const defaults = payload?.suggested_defaults || {};
  Object.values(defaults).forEach((value) => {
    if (Array.isArray(value)) {
      suggestions.push(...value.map((item) => String(item)));
    } else if (value !== undefined && value !== null && value !== '') {
      suggestions.push(String(value));
    }
  });
  return Array.from(new Set(suggestions)).slice(0, 8);
}

export function checkpointEventFromJob(job) {
  if (
    job?.status !== 'paused' ||
    job?.checkpoint_state !== 'pending' ||
    !job?.checkpoint_type ||
    job.checkpoint_type === 'none' ||
    !job?.checkpoint_payload
  ) {
    return null;
  }
  return {
    type: 'checkpoint.created',
    job_id: job.job_id,
    checkpoint_id: job.checkpoint_payload.checkpoint_id,
    checkpoint_type: job.checkpoint_type,
    payload: job.checkpoint_payload,
  };
}

function apiBaseUrl() {
  return getDataElfApiBaseUrl();
}
