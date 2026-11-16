import pytest
from forever22.d1 import D1Connection


def test_transaction_diff_and_read_your_writes():
    calls = []
    def rpc(path, body=None):
        calls.append((path, body))
        if path == '/snapshot':
            return {'version': 1, 'tables': {'app_state': [{'key': 'old', 'value': 'a'}]}}
        return {'version': 2}
    c = D1Connection(rpc=rpc)
    c.execute('DELETE FROM app_state WHERE key = %s', ('old',))
    c.execute('INSERT INTO app_state VALUES (%s, %s)', ('new', 'b'))
    assert c.execute('SELECT value FROM app_state WHERE key = %s', ('new',)).fetchone() == {'value': 'b'}
    c.commit()
    change = calls[-1][1]['changes']['app_state']
    assert change == {'upserts': [{'key': 'new', 'value': 'b'}], 'deletes': [{'key': 'old'}]}
    c.close()


def test_context_exception_does_not_commit():
    calls = []
    def rpc(path, body=None):
        calls.append(path)
        return {'version': 0, 'tables': {}}
    with pytest.raises(ValueError):
        with D1Connection(rpc=rpc) as c:
            c.execute('INSERT INTO app_state VALUES (%s,%s)', ('x', 'y'))
            raise ValueError('cancel')
    assert calls == ['/snapshot']


def test_placeholder_inside_string_is_preserved():
    c = D1Connection(rpc=lambda *args: {'version': 0, 'tables': {}})
    assert c.execute("SELECT 'literal %s' AS value").fetchone()['value'] == 'literal %s'
    c.close()


def test_read_only_transaction_does_not_post():
    calls = []
    def rpc(path, body=None):
        calls.append(path)
        return {'version': 0, 'tables': {}}
    with D1Connection(rpc=rpc) as c:
        assert c.execute('SELECT * FROM events').fetchall() == []
    assert calls == ['/snapshot']


def test_engagement_roundtrip_and_dedup(monkeypatch):
    from forever22 import engagements
    snapshot = {'version': 0, 'tables': {}}
    def rpc(path, body=None):
        if path == '/snapshot':
            return snapshot
        for table, change in body['changes'].items():
            assert not change['deletes']
            snapshot['tables'][table] = change['upserts']
        snapshot['version'] += 1
        return {'version': snapshot['version']}
    monkeypatch.setattr(engagements, '_connect', lambda: D1Connection(rpc=rpc))
    engagements.save(engagements.Engagement(title='Example', slug='example', type='client',
        links=['https://example.com'], tags=['test'], start_date='2026-11-16',
        contacts=[engagements.Contact(name='A')]))
    eng = engagements.load('example')
    assert eng.links == ['https://example.com']
    assert eng.tags == ['test']
    assert eng.start_date == '2026-11-16'
    assert eng.contacts[0].name == 'A'
    entry = engagements.TimeLogEntry(date='2026-11-16', hours=1, source_event_id='ev1')
    assert engagements.add_time_entries('example', [entry, entry]) == 1
    assert engagements.load('example').total_hours == 1


def test_failed_commit_cannot_be_replayed():
    from forever22.d1 import D1Conflict, D1Error
    calls = []
    def rpc(path, body=None):
        calls.append(path)
        if path == '/snapshot':
            return {'version': 0, 'tables': {}}
        raise D1Conflict('conflict')
    c = D1Connection(rpc=rpc)
    c.execute('INSERT INTO app_state VALUES (%s,%s)', ('x', 'y'))
    with pytest.raises(D1Conflict):
        c.commit()
    with pytest.raises(D1Error):
        c.commit()
    assert calls.count('/commit') == 1
    c.close()


def test_schema_catalog_matches_python_schema():
    import json
    from pathlib import Path
    c = D1Connection(rpc=lambda *args: {'version': 0, 'tables': {}})
    catalog = json.loads((Path(__file__).parents[1] / 'cloudflare/schema.json').read_text())
    assert catalog == {t: {'columns': cols, 'keys': keys} for t, (cols, keys) in c._tables.items()}
    c.close()


def test_fresh_db_adopts_existing_mirror(monkeypatch):
    import json
    from contextlib import contextmanager
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace
    from unittest.mock import Mock
    from forever22 import db, mirror
    c = D1Connection(rpc=lambda *args: {'version':0, 'tables':{}})
    start = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    end = (datetime.now(timezone.utc) + timedelta(days=1, hours=1)).isoformat()
    raw = {'start':{'dateTime':start}, 'end':{'dateTime':end}}
    for label, event_id, extra in [('a','source',{}), ('b','existing-mirror', {'extendedProperties':{'private':{'mirroredFrom':'a:source'}}})]:
        db.upsert_event(c, account_label=label, event_id=event_id, calendar_id='primary',
            start_iso=start, end_iso=end, summary='Test', status='confirmed', is_busy=True,
            raw_json=json.dumps(raw | extra), synced_at=start)
    @contextmanager
    def connect():
        yield c
    monkeypatch.setattr(db, 'connect', connect)
    service = Mock()
    monkeypatch.setattr(mirror._ServiceCache, 'get', lambda *args:service)
    cfg = SimpleNamespace(mirror=SimpleNamespace(enabled=True, rules={'a':['b']}, mirror_title='Busy'),
        sync=SimpleNamespace(future_days=30), bestmate=SimpleNamespace(visibility='private'))
    report = mirror.run(cfg=cfg)
    assert report.ok
    assert report.counts['a'].adopted == 1
    assert report.counts['a'].created == 0
    service.events.assert_not_called()
    c.close()


def test_fresh_db_adopts_existing_aggregate_calendar_and_event(monkeypatch):
    import json
    from contextlib import contextmanager
    from types import SimpleNamespace
    from unittest.mock import Mock
    from forever22 import db, aggregate
    c = D1Connection(rpc=lambda *args: {'version':0, 'tables':{}})
    raw = {'start':{'dateTime':'2026-12-01T10:00:00Z'}, 'end':{'dateTime':'2026-12-01T11:00:00Z'}, 'iCalUID':'uid'}
    db.upsert_event(c, account_label='a', event_id='source', calendar_id='primary',
        start_iso=raw['start']['dateTime'], end_iso=raw['end']['dateTime'], summary='Test',
        status='confirmed', is_busy=True, raw_json=json.dumps(raw), synced_at='now')
    @contextmanager
    def connect():
        yield c
    monkeypatch.setattr(db, 'connect', connect)
    service = Mock()
    service.calendarList.return_value.list.return_value.execute.return_value = {'items':[{'id':'existing-cal', 'summary':'Unified'}]}
    service.events.return_value.list.return_value.execute.return_value = {'items':[{'id':'existing-copy', 'extendedProperties':{'private':{'aggregatedFrom':'a:source'}}}]}
    monkeypatch.setattr(aggregate, 'load_credentials', lambda *args, **kwargs:None)
    monkeypatch.setattr(aggregate, 'build', lambda *args, **kwargs:service)
    cfg = SimpleNamespace(aggregate=SimpleNamespace(enabled=True, host_account='a', calendar_name='Unified'))
    report = aggregate.run(cfg=cfg)
    assert report.ok
    assert report.adopted == 1
    assert report.created == 0
    assert report.calendar_id == 'existing-cal'
    service.calendars.return_value.insert.assert_not_called()
    service.events.return_value.insert.assert_not_called()
    c.close()
