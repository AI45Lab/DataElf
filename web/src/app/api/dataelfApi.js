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
  source.addEventListener('log.appended', (message) => dispatchEventPayload(message, handlers));
  source.addEventListener('job.completed', (message) => dispatchEventPayload(message, handlers));
  source.addEventListener('job.failed', (message) => dispatchEventPayload(message, handlers));
  source.onerror = (error) => {
    handlers.onError?.(error);
  };
  return source;
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

function apiBaseUrl() {
  return getDataElfApiBaseUrl();
}
