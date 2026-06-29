import assert from 'node:assert/strict';
import test from 'node:test';

import {
  checkpointEventFromJob,
  createSession,
  createSessionRun,
  extractCheckpointSuggestions,
  fetchJob,
  getDataElfApiBaseUrl,
  getSession,
  listDatasets,
  listSessions,
  listTools,
  replayRunEvents,
  saveSessionSnapshot,
  setSessionMode,
  shouldUseRunStatusPollingFallback,
  subscribeRunEvents,
  updateSession,
  deleteSession,
} from './dataelfApi.js';

test('defaults DataElf API base URL to forwarded backend port', () => {
  assert.equal(getDataElfApiBaseUrl(), 'http://127.0.0.1:8001');
});

test('fetches job state from the backend job endpoint', async () => {
  const originalFetch = globalThis.fetch;
  let requestedUrl = '';
  globalThis.fetch = async (url) => {
    requestedUrl = String(url);
    return {
      ok: true,
      json: async () => ({ job_id: 'job_1', status: 'failed', error: 'bad task' }),
    };
  };

  try {
    const job = await fetchJob('job_1');

    assert.equal(requestedUrl, 'http://127.0.0.1:8001/api/v1/jobs/job_1');
    assert.deepEqual(job, { job_id: 'job_1', status: 'failed', error: 'bad task' });
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('replays job events from the backend event replay endpoint', async () => {
  const originalFetch = globalThis.fetch;
  let requestedUrl = '';
  globalThis.fetch = async (url) => {
    requestedUrl = String(url);
    return {
      ok: true,
      json: async () => ({
        events: [
          { event_id: 2, type: 'log.appended', log: { message: 'hello' } },
          { event_id: 3, type: 'job.completed' },
        ],
      }),
    };
  };

  try {
    const events = await replayRunEvents('job_1', 1);

    assert.equal(
      requestedUrl,
      'http://127.0.0.1:8001/api/v1/jobs/job_1/events/replay?after_event_id=1',
    );
    assert.deepEqual(events, [
      { event_id: 2, type: 'log.appended', log: { message: 'hello' } },
      { event_id: 3, type: 'job.completed' },
    ]);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('fetches dataset catalog from the backend datasets endpoint', async () => {
  const originalFetch = globalThis.fetch;
  let requestedUrl = '';
  globalThis.fetch = async (url) => {
    requestedUrl = String(url);
    return {
      ok: true,
      json: async () => ({
        datasets: [{ name: 'security_audit_samples', rows: '45', nesting: '3', size: '18 KB' }],
      }),
    };
  };

  try {
    const datasets = await listDatasets();

    assert.equal(requestedUrl, 'http://127.0.0.1:8001/api/v1/datasets');
    assert.deepEqual(datasets, [
      { name: 'security_audit_samples', rows: '45', nesting: '3', size: '18 KB' },
    ]);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('fetches tool catalog from the backend tools endpoint', async () => {
  const originalFetch = globalThis.fetch;
  let requestedUrl = '';
  globalThis.fetch = async (url) => {
    requestedUrl = String(url);
    return {
      ok: true,
      json: async () => ({
        tools: [{ name: 'security_audit', description: 'Audit data', parameters: {} }],
      }),
    };
  };

  try {
    const tools = await listTools();

    assert.equal(requestedUrl, 'http://127.0.0.1:8001/api/v1/tools');
    assert.deepEqual(tools, [
      { name: 'security_audit', description: 'Audit data', parameters: {} },
    ]);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('manages persisted web sessions through backend endpoints', async () => {
  const originalFetch = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url: String(url), options });
    const path = String(url).replace('http://127.0.0.1:8001', '');
    if (path === '/api/v1/sessions' && !options.method) {
      return {
        ok: true,
        json: async () => ({ sessions: [{ session_id: 'sess_1', name: 'A' }] }),
      };
    }
    if (path === '/api/v1/sessions' && options.method === 'POST') {
      return {
        ok: true,
        json: async () => ({ session_id: 'sess_2', name: 'B' }),
      };
    }
    if (path === '/api/v1/sessions/sess_1' && !options.method) {
      return {
        ok: true,
        json: async () => ({ session_id: 'sess_1', name: 'A' }),
      };
    }
    if (path === '/api/v1/sessions/sess_1' && options.method === 'PATCH') {
      return {
        ok: true,
        json: async () => ({ session_id: 'sess_1', name: 'Renamed' }),
      };
    }
    if (path === '/api/v1/sessions/sess_1/mode') {
      return {
        ok: true,
        json: async () => ({ session_id: 'sess_1', mode: 'pilot', backend_mode: 'pilot' }),
      };
    }
    if (path === '/api/v1/sessions/sess_1/snapshot') {
      return {
        ok: true,
        json: async () => ({ session_id: 'sess_1', snapshot: { messages: [] } }),
      };
    }
    if (path === '/api/v1/sessions/sess_1/runs') {
      return {
        ok: true,
        json: async () => ({ job_id: 'job_1', status: 'running' }),
      };
    }
    if (path === '/api/v1/sessions/sess_1' && options.method === 'DELETE') {
      return {
        ok: true,
        json: async () => ({ deleted: true }),
      };
    }
    throw new Error(`unexpected request: ${path}`);
  };

  try {
    assert.deepEqual(await listSessions(), [{ session_id: 'sess_1', name: 'A' }]);
    assert.deepEqual(await createSession({ name: 'B' }), { session_id: 'sess_2', name: 'B' });
    assert.deepEqual(await getSession('sess_1'), { session_id: 'sess_1', name: 'A' });
    assert.deepEqual(await updateSession('sess_1', { name: 'Renamed' }), {
      session_id: 'sess_1',
      name: 'Renamed',
    });
    assert.deepEqual(await setSessionMode('sess_1', 'pilot'), {
      session_id: 'sess_1',
      mode: 'pilot',
      backend_mode: 'pilot',
    });
    assert.deepEqual(await saveSessionSnapshot('sess_1', { messages: [] }), {
      session_id: 'sess_1',
      snapshot: { messages: [] },
    });
    assert.deepEqual(await createSessionRun('sess_1', 'run', { budgetSteps: 5 }), {
      job_id: 'job_1',
      status: 'running',
    });
    assert.deepEqual(await deleteSession('sess_1'), { deleted: true });

    assert.deepEqual(
      calls.map(call => [call.url, call.options.method || 'GET']),
      [
        ['http://127.0.0.1:8001/api/v1/sessions', 'GET'],
        ['http://127.0.0.1:8001/api/v1/sessions', 'POST'],
        ['http://127.0.0.1:8001/api/v1/sessions/sess_1', 'GET'],
        ['http://127.0.0.1:8001/api/v1/sessions/sess_1', 'PATCH'],
        ['http://127.0.0.1:8001/api/v1/sessions/sess_1/mode', 'POST'],
        ['http://127.0.0.1:8001/api/v1/sessions/sess_1/snapshot', 'POST'],
        ['http://127.0.0.1:8001/api/v1/sessions/sess_1/runs', 'POST'],
        ['http://127.0.0.1:8001/api/v1/sessions/sess_1', 'DELETE'],
      ],
    );
    assert.deepEqual(JSON.parse(calls[6].options.body), {
      command: 'run',
      budget_steps: 5,
    });
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('extracts checkpoint suggestions from payload options before defaults', () => {
  const suggestions = extractCheckpointSuggestions({
    options: ['security_audit_samples', 'companies'],
    suggested_defaults: { dataset_name: 'security_audit_samples' },
  });

  assert.deepEqual(suggestions, ['security_audit_samples', 'companies']);
});

test('creates checkpoint event from paused job state', () => {
  const event = checkpointEventFromJob({
    job_id: 'job_1',
    status: 'paused',
    checkpoint_type: 'dataset_selection',
    checkpoint_state: 'pending',
    checkpoint_payload: {
      checkpoint_id: 'chk_123',
      options: ['security_audit_samples'],
      prompt: '请选择数据集',
    },
  });

  assert.deepEqual(event, {
    type: 'checkpoint.created',
    job_id: 'job_1',
    checkpoint_id: 'chk_123',
    checkpoint_type: 'dataset_selection',
    payload: {
      checkpoint_id: 'chk_123',
      options: ['security_audit_samples'],
      prompt: '请选择数据集',
    },
  });
});

test('polling completion fallback does not override an open SSE stream', () => {
  assert.equal(shouldUseRunStatusPollingFallback(1), false);
  assert.equal(shouldUseRunStatusPollingFallback(0), true);
  assert.equal(shouldUseRunStatusPollingFallback(2), true);
  assert.equal(shouldUseRunStatusPollingFallback(undefined), true);
});

test('subscribes to pilot lifecycle SSE events', () => {
  const originalEventSource = globalThis.EventSource;
  const listeners = {};
  let openedUrl = '';
  globalThis.EventSource = class {
    constructor(url) {
      openedUrl = String(url);
      this.readyState = 1;
    }

    addEventListener(type, handler) {
      listeners[type] = handler;
    }

    close() {}
  };

  try {
    const seen = [];
    subscribeRunEvents('job_1', {
      onEvent: event => seen.push(event),
    });

    assert.equal(openedUrl, 'http://127.0.0.1:8001/api/v1/jobs/job_1/events');
    assert.ok(listeners['pilot.attempt_started']);
    assert.ok(listeners['pilot.planner']);
    assert.ok(listeners['pilot.pipeline']);
    assert.ok(listeners['pilot.judge']);
    listeners['pilot.attempt_started']({
      data: JSON.stringify({
        type: 'pilot.attempt_started',
        attempt_id: 'attempt_01',
        budget_steps: 3,
      }),
    });
    assert.deepEqual(seen, [{
      type: 'pilot.attempt_started',
      attempt_id: 'attempt_01',
      budget_steps: 3,
    }]);
  } finally {
    globalThis.EventSource = originalEventSource;
  }
});
