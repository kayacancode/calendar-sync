import {test} from 'node:test';
import assert from 'node:assert/strict';
import worker from '../worker.js';

test('missing credentials fail closed', async () => {
  const r = await worker.fetch(new Request('https://test/snapshot'), {});
  assert.equal(r.status, 503);
});
test('unauthenticated request does not touch database', async () => {
  const r = await worker.fetch(new Request('https://test/snapshot'), {SERVICE_TOKEN:'secret'});
  assert.equal(r.status, 401);
});
test('unknown tables are rejected before database access', async () => {
  const r = await worker.fetch(new Request('https://test/commit', {method:'POST', headers:{Authorization:'Bearer secret'}, body:JSON.stringify({version:0, request_id:crypto.randomUUID(), changes:{_revision:{upserts:[],deletes:[]}}})}), {SERVICE_TOKEN:'secret'});
  assert.equal(r.status, 400);
});
test('malformed JSON returns a sanitized client error', async () => {
  const r = await worker.fetch(new Request('https://test/commit', {method:'POST', headers:{Authorization:'Bearer secret'}, body:'nope'}), {SERVICE_TOKEN:'secret'});
  assert.equal(r.status, 400);
  assert.deepEqual(await r.json(), {error:'Invalid request'});
});
