import schema from './schema.json' with {type: 'json'};

const MAX_BODY = 8 * 1024 * 1024;
const MAX_SNAPSHOT = 16 * 1024 * 1024;
const MAX_ROWS = 50000;
const reply = (body, status = 200) => Response.json(body, {status, headers: {'Cache-Control': 'no-store'}});

async function authorized(request, token) {
  const bytes = new TextEncoder();
  const [a, b] = await Promise.all([
    crypto.subtle.digest('SHA-256', bytes.encode(request.headers.get('Authorization') || '')),
    crypto.subtle.digest('SHA-256', bytes.encode(`Bearer ${token}`)),
  ]);
  // Hashes have a fixed size; Workers exposes timingSafeEqual on SubtleCrypto.
  if (crypto.subtle.timingSafeEqual) return crypto.subtle.timingSafeEqual(a, b);
  // Node test runner fallback (the production runtime uses the branch above).
  const {timingSafeEqual} = await import('node:crypto');
  return timingSafeEqual(new Uint8Array(a), new Uint8Array(b));
}

async function bodyJSON(request) {
  if (!request.body) throw new Error('invalid');
  const reader = request.body.getReader();
  const chunks = [];
  let size = 0;
  while (true) {
    const {done, value} = await reader.read();
    if (done) break;
    size += value.byteLength;
    if (size > MAX_BODY) { await reader.cancel(); throw new Error('large'); }
    chunks.push(value);
  }
  const joined = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) {joined.set(chunk, offset); offset += chunk.length;}
  return JSON.parse(new TextDecoder().decode(joined));
}

const scalar = v => v === null || typeof v === 'string' || (typeof v === 'number' && Number.isFinite(v));
const object = v => v !== null && typeof v === 'object' && !Array.isArray(v);
function validRow(row, columns, keys) {
  return object(row) && Object.keys(row).length === columns.length &&
    columns.every(c => Object.hasOwn(row, c) && scalar(row[c])) && keys.every(k => row[k] !== null);
}
function validCommit(body) {
  if (!object(body) || !Number.isSafeInteger(body.version) || body.version < 0 ||
      typeof body.request_id !== 'string' || !/^[0-9a-f-]{36}$/.test(body.request_id) || !object(body.changes)) return false;
  let rows = 0;
  for (const [table, change] of Object.entries(body.changes)) {
    if (!Object.hasOwn(schema, table) || !object(change) || !Array.isArray(change.upserts) || !Array.isArray(change.deletes)) return false;
    const {columns, keys} = schema[table];
    if (!change.upserts.every(r => validRow(r, columns, keys)) || !change.deletes.every(r => validRow(r, keys, keys))) return false;
    rows += change.upserts.length + change.deletes.length;
  }
  return rows > 0 && rows <= MAX_ROWS;
}

function chunks(rows) {
  const groups = [];
  let group = [], size = 2;
  for (const row of rows) {
    const bytes = new TextEncoder().encode(JSON.stringify(row)).length + 1;
    if (bytes > 1000000) throw new Error('large');
    if (size + bytes > 1000000) {groups.push(group); group = []; size = 2;}
    group.push(row); size += bytes;
  }
  if (group.length) groups.push(group);
  return groups;
}

async function snapshot(db) {
  const names = Object.keys(schema);
  const result = await db.batch([
    db.prepare('SELECT version FROM _revision WHERE id = 1'),
    ...names.map(t => db.prepare(`SELECT * FROM "${t}" LIMIT ${MAX_ROWS + 1}`)),
  ]);
  const tables = {};
  let count = 0;
  names.forEach((name, i) => {tables[name] = result[i + 1].results; count += tables[name].length;});
  if (count > MAX_ROWS) return reply({error: 'Database exceeds snapshot row limit'}, 413);
  const data = JSON.stringify({version: result[0].results[0].version, tables});
  if (new TextEncoder().encode(data).length > MAX_SNAPSHOT) return reply({error: 'Database exceeds snapshot size limit'}, 413);
  return new Response(data, {headers: {'Content-Type': 'application/json', 'Cache-Control': 'no-store'}});
}

async function commit(db, body) {
  // A request UUID makes an immediate retry idempotent without replaying writes.
  const previous = await db.prepare('SELECT version FROM _commits WHERE request_id = ?').bind(body.request_id).first();
  if (previous) return reply({version: previous.version});
  const statements = [
    db.prepare('INSERT INTO _commit_guard (id, valid) SELECT 1, CASE WHEN version = ? THEN 1 ELSE 0 END FROM _revision WHERE id = 1').bind(body.version),
    db.prepare('PRAGMA defer_foreign_keys = ON'),
  ];
  // Delete children before parents. Deferred FKs protect final consistency.
  for (const table of Object.keys(schema).reverse()) {
    const change = body.changes[table];
    if (!change) continue;
    const {keys} = schema[table];
    for (const rows of chunks(change.deletes)) {
      const where = keys.map(k => `"${table}"."${k}" = json_extract(j.value, '$.${k}')`).join(' AND ');
      statements.push(db.prepare(`DELETE FROM "${table}" WHERE EXISTS (SELECT 1 FROM json_each(?) j WHERE ${where})`).bind(JSON.stringify(rows)));
    }
  }
  for (const table of Object.keys(schema)) {
    const change = body.changes[table];
    if (!change) continue;
    const {columns, keys} = schema[table];
    const fields = columns.map(c => `"${c}"`).join(',');
    const values = columns.map(c => `json_extract(value, '$.${c}')`).join(',');
    const updates = columns.filter(c => !keys.includes(c)).map(c => `"${c}"=excluded."${c}"`).join(',');
    for (const rows of chunks(change.upserts)) {
      statements.push(db.prepare(`INSERT INTO "${table}" (${fields}) SELECT ${values} FROM json_each(?) WHERE true ON CONFLICT (${keys.map(k => `"${k}"`).join(',')}) DO UPDATE SET ${updates}`).bind(JSON.stringify(rows)));
    }
  }
  statements.push(
    db.prepare('UPDATE _revision SET version = version + 1 WHERE id = 1'),
    db.prepare('INSERT INTO _commits (request_id, version) SELECT ?, version FROM _revision WHERE id = 1').bind(body.request_id),
    db.prepare('DELETE FROM _commits WHERE version < (SELECT version - 100 FROM _revision WHERE id = 1)'),
    db.prepare('DELETE FROM _commit_guard WHERE id = 1'),
  );
  try {
    await db.batch(statements);
    return reply({version: body.version + 1});
  } catch (error) {
    // Another writer may have committed or the same UUID may have completed.
    const prior = await db.prepare('SELECT version FROM _commits WHERE request_id = ?').bind(body.request_id).first();
    if (prior) return reply({version: prior.version});
    if (String(error).includes('_commit_guard') || String(error).includes('valid = 1')) return reply({error: 'Concurrent update'}, 409);
    return reply({error: 'Database transaction rejected'}, 422);
  }
}

export default {
  async fetch(request, env) {
    if (!env.SERVICE_TOKEN) return reply({error: 'Storage service is not configured'}, 503);
    if (!await authorized(request, env.SERVICE_TOKEN)) return reply({error: 'Unauthorized'}, 401);
    const path = new URL(request.url).pathname;
    try {
      if (request.method === 'GET' && path === '/snapshot') return await snapshot(env.DB);
      if (request.method === 'POST' && path === '/commit') {
        let body;
        try {body = await bodyJSON(request);} catch (e) {return reply({error: 'Invalid request'}, e.message === 'large' ? 413 : 400);}
        if (!validCommit(body)) return reply({error: 'Invalid request'}, 400);
        return await commit(env.DB, body);
      }
      return reply({error: 'Not found'}, 404);
    } catch {
      return reply({error: 'Storage service unavailable'}, 503);
    }
  },
};
