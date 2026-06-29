import assert from 'node:assert/strict';
import test from 'node:test';

import { buildModeCommandPromptMessage } from './sessionDisplay.js';

test('builds a fixed command prompt card after run mode selection', () => {
  const message = buildModeCommandPromptMessage('sess_1', 'run', '12:00:00');

  assert.equal(message.id, 'sess_1-mode-command-prompt');
  assert.equal(message.type, 'system');
  assert.equal(message.timestamp, '12:00:00');
  assert.match(message.content, /RUN MODE READY/);
  assert.match(message.content, /elf run "run security_audit on security_audit_samples"/);
});

test('builds a fixed command prompt card after pilot mode selection', () => {
  const message = buildModeCommandPromptMessage('sess_2', 'pilot', '12:00:01');

  assert.equal(message.id, 'sess_2-mode-command-prompt');
  assert.match(message.content, /PILOT MODE READY/);
  assert.match(message.content, /elf pilot "run security_audit on security_audit_samples"/);
});
