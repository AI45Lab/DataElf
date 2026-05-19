import assert from 'node:assert/strict';
import test from 'node:test';

import {
  checkpointEventFromJob,
  extractCheckpointSuggestions,
  fetchJob,
  getDataElfApiBaseUrl,
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
