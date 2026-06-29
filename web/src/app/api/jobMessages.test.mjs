import assert from 'node:assert/strict';
import test from 'node:test';

import {
  isJobMessage,
  upsertJobMessages,
} from './jobMessages.js';

test('orders job cards by execution phase even when events arrive out of order', () => {
  const ordered = upsertJobMessages([
    { id: 'user_1', type: 'user', content: 'run', timestamp: '12:00:00' },
    { id: 'job_1-result', type: 'result', content: 'done', timestamp: '12:00:06' },
  ], 'job_1', [
    { id: 'job_1-lifecycle', type: 'lifecycle', content: '', timestamp: '12:00:03' },
    { id: 'job_1-pipeline', type: 'pipeline', content: 'pipeline', timestamp: '12:00:01' },
    { id: 'exec_1', type: 'execution', content: '', timestamp: '12:00:02', jobId: 'job_1' },
  ]);

  assert.deepEqual(
    ordered.map(message => message.id),
    ['user_1', 'job_1-pipeline', 'exec_1', 'job_1-lifecycle', 'job_1-result'],
  );
});

test('updates existing job cards in place without moving unrelated messages', () => {
  const ordered = upsertJobMessages([
    { id: 'before', type: 'system', content: 'before', timestamp: '12:00:00' },
    { id: 'job_1-pipeline', type: 'pipeline', content: 'old', timestamp: '12:00:01' },
    { id: 'after', type: 'system', content: 'after', timestamp: '12:00:02' },
  ], 'job_1', [
    { id: 'job_1-pipeline', type: 'pipeline', content: 'new', timestamp: '12:00:03' },
    { id: 'job_1-result', type: 'result', content: 'done', timestamp: '12:00:04' },
  ]);

  assert.deepEqual(
    ordered.map(message => [message.id, message.content]),
    [
      ['before', 'before'],
      ['job_1-pipeline', 'new'],
      ['job_1-result', 'done'],
      ['after', 'after'],
    ],
  );
});

test('identifies job messages from job id fields and stable id prefixes', () => {
  assert.equal(isJobMessage({ id: 'exec_1', jobId: 'job_1' }, 'job_1'), true);
  assert.equal(isJobMessage({ id: 'job_1-result' }, 'job_1'), true);
  assert.equal(isJobMessage({ id: 'other-result' }, 'job_1'), false);
});
