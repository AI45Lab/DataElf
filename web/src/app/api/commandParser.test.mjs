import assert from 'node:assert/strict';
import test from 'node:test';

import { parseUserCommand } from './commandParser.js';

test('parses quoted elf run command', () => {
  const parsed = parseUserCommand('elf run "run security_audit on security_audit_samples"');

  assert.equal(parsed.mode, 'RUN');
  assert.equal(parsed.task, 'run security_audit on security_audit_samples');
});

test('defaults plain language to run mode', () => {
  const parsed = parseUserCommand('audit security_audit_samples with default checkers');

  assert.equal(parsed.mode, 'RUN');
  assert.equal(parsed.task, 'audit security_audit_samples with default checkers');
});

test('keeps pilot mode on the mock path', () => {
  const parsed = parseUserCommand('elf pilot improve audit coverage');

  assert.equal(parsed.mode, 'PILOT');
  assert.equal(parsed.task, 'improve audit coverage');
});
