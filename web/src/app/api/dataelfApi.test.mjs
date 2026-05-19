import assert from 'node:assert/strict';
import test from 'node:test';

import { getDataElfApiBaseUrl } from './dataelfApi.js';

test('defaults DataElf API base URL to forwarded backend port', () => {
  assert.equal(getDataElfApiBaseUrl(), 'http://127.0.0.1:8001');
});
