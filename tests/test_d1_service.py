"""Run against isolated local D1: F22_D1_TEST_URL=http://127.0.0.1:8788 pytest ..."""
import json
import os
import uuid
from pathlib import Path

import httpx
import pytest

from forever22.d1 import D1Connection, D1Conflict

pytestmark = pytest.mark.skipif(not os.getenv('F22_D1_TEST_URL'), reason='requires isolated local D1 service')


@pytest.fixture
def endpoint(monkeypatch):
    url = os.environ['F22_D1_TEST_URL']
    assert url.startswith('http://127.0.0.1:')
    monkeypatch.setenv('FOREVER22_D1_URL', url)
    monkeypatch.setenv('FOREVER22_D1_TOKEN', 'local-test-token')
    return url


def test_real_d1_transaction_and_conflicting_writer(endpoint):
    key = 'test-' + uuid.uuid4().hex
    a, b = D1Connection(), D1Connection()
    a.execute('INSERT INTO app_state VALUES (%s,%s)', (key, 'first'))
    a.commit()
    b.execute('INSERT INTO app_state VALUES (%s,%s)', (key, 'stale'))
    with pytest.raises(D1Conflict):
        b.commit()
    a.close(); b.close()
    with D1Connection() as c:
        assert c.execute('SELECT value FROM app_state WHERE key = %s', (key,)).fetchone()['value'] == 'first'
        c.execute('DELETE FROM app_state WHERE key = %s', (key,))


def test_real_d1_batch_failure_rolls_back_every_table(endpoint):
    key = 'test-' + uuid.uuid4().hex
    with httpx.Client(base_url=endpoint, headers={'Authorization':'Bearer local-test-token'}) as client:
        before = client.get('/snapshot').json()
        r = client.post('/commit', json={'version':before['version'], 'request_id':str(uuid.uuid4()), 'changes':{
            'app_state':{'upserts':[{'key':key, 'value':'must rollback'}], 'deletes':[]},
            'contacts':{'upserts':[{'id':999999999,'engagement_slug':'does-not-exist','name':'A','email':'','role':''}], 'deletes':[]},
        }})
        assert r.status_code == 422, r.text
        after = client.get('/snapshot').json()
        assert after == before


def test_real_d1_engagements_and_generated_ids(endpoint):
    from forever22 import engagements
    slug = 'test-' + uuid.uuid4().hex
    eng = engagements.Engagement(title='Integration', slug=slug, type='client',
        links=['https://example.com'], contacts=[engagements.Contact(name='A')])
    engagements.save(eng)
    assert engagements.load(slug).links == eng.links
    engagements.save(eng)
    entry = engagements.TimeLogEntry(date='2026-11-16', hours=2, source_event_id='calendar-1')
    assert engagements.add_time_entries(slug, [entry, entry]) == 1
    assert engagements.add_time_entries(slug, [entry]) == 0
    assert engagements.load(slug).total_hours == 2
    with D1Connection() as c:
        c.execute('DELETE FROM engagements WHERE slug = %s', (slug,))
    with D1Connection() as c:
        assert c.execute('SELECT * FROM contacts WHERE engagement_slug = %s', (slug,)).fetchall() == []


def test_real_d1_commit_uuid_is_idempotent(endpoint):
    key = 'test-' + uuid.uuid4().hex
    with httpx.Client(base_url=endpoint, headers={'Authorization':'Bearer local-test-token'}) as client:
        version = client.get('/snapshot').json()['version']
        body = {'version':version, 'request_id':str(uuid.uuid4()), 'changes':{
            'app_state':{'upserts':[{'key':key,'value':'once'}], 'deletes':[]}}}
        first = client.post('/commit', json=body)
        assert first.status_code == 200, first.text
        second = client.post('/commit', json=body)
        assert second.json() == first.json()
    with D1Connection() as c:
        c.execute('DELETE FROM app_state WHERE key = %s', (key,))
