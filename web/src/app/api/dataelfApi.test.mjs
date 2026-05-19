import assert from 'node:assert/strict';
import test from 'node:test';

import { fetchJob, getDataElfApiBaseUrl } from './dataelfApi.js';

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
