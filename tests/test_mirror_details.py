from types import SimpleNamespace

from forever22.mirror import _mirror_body


def body(raw, summary='Planning meeting'):
    raw = {'start': {'dateTime': '2026-12-01T10:00:00Z'},
           'end': {'dateTime': '2026-12-01T11:00:00Z'}, **raw}
    return _mirror_body(SimpleNamespace(mirror=SimpleNamespace(mirror_title='Busy')),
                        'work', {'event_id': 'source-id', 'summary': summary}, raw)


def test_mirror_uses_original_title_and_google_meet_link():
    result = body({'hangoutLink': 'https://meet.google.com/abc-defg-hij'})
    assert result['summary'] == 'Planning meeting'
    assert 'https://meet.google.com/abc-defg-hij' in result['description']
    assert result['visibility'] == 'private'
    assert result['extendedProperties']['private']['mirroredFrom'] == 'work:source-id'


def test_mirror_uses_video_conference_entry_point():
    result = body({'conferenceData': {'entryPoints': [
        {'entryPointType': 'phone', 'uri': 'tel:+15555555555'},
        {'entryPointType': 'video', 'uri': 'https://example.zoom.us/j/123'},
    ]}})
    assert 'https://example.zoom.us/j/123' in result['description']
    assert 'tel:' not in result['description']


def test_no_title_or_link_has_a_readable_fallback():
    result = body({}, summary=None)
    assert result['summary'] == 'Busy — work'
    assert 'Join meeting:' not in result['description']


def test_invalid_link_scheme_is_not_copied():
    assert 'javascript:' not in body({'hangoutLink': 'javascript:alert(1)'})['description']


def test_existing_mirror_updates_without_time_change_and_then_stays_stable(monkeypatch):
    import json
    from contextlib import contextmanager
    from datetime import datetime, timedelta, timezone
    from unittest.mock import Mock
    from forever22 import db, mirror
    from forever22.d1 import D1Connection
    c = D1Connection(rpc=lambda *args: {'version': 0, 'tables': {}})
    start = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    end = (datetime.now(timezone.utc) + timedelta(days=1, hours=1)).isoformat()
    raw = {'start': {'dateTime': start}, 'end': {'dateTime': end},
           'hangoutLink': 'https://meet.google.com/abc-defg-hij'}
    old_copy = {'start': raw['start'], 'end': raw['end'], 'summary': 'Busy — a',
                'description': 'Auto-mirrored from a by forever22.',
                'extendedProperties': {'private': {'mirroredFrom': 'a:source'}}}
    for label, eid, ev in [('a', 'source', raw), ('b', 'copy', old_copy)]:
        db.upsert_event(c, account_label=label, event_id=eid, calendar_id='primary',
            start_iso=start, end_iso=end, summary='Planning meeting', status='confirmed',
            is_busy=True, raw_json=json.dumps(ev), synced_at=start)
    c.execute('INSERT INTO mirrors VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)',
              ('a', 'source', 'b', 'copy', start, end, 'Planning meeting', start, start))
    @contextmanager
    def connect():
        yield c
    monkeypatch.setattr(db, 'connect', connect)
    service = Mock()
    monkeypatch.setattr(mirror._ServiceCache, 'get', lambda *args: service)
    monkeypatch.setattr(mirror.bestmate, 'ingest', lambda *args, **kwargs: None)
    cfg = SimpleNamespace(mirror=SimpleNamespace(enabled=True, rules={'a': ['b']}, mirror_title='Busy'),
        sync=SimpleNamespace(future_days=30), bestmate=SimpleNamespace(visibility='private'))
    report = mirror.run(cfg=cfg)
    assert report.counts['a'].updated == 1
    patch = service.events.return_value.patch
    assert patch.call_args.kwargs['eventId'] == 'copy'
    updated = patch.call_args.kwargs['body']
    assert updated['summary'] == 'Planning meeting'
    assert raw['hangoutLink'] in updated['description']
    c.execute('UPDATE events SET raw_json=%s WHERE event_id=%s', (json.dumps(updated), 'copy'))
    patch.reset_mock()
    assert mirror.run(cfg=cfg).counts['a'].updated == 0
    patch.assert_not_called()
    raw['hangoutLink'] = 'https://meet.google.com/new-link-abc'
    c.execute('UPDATE events SET raw_json=%s WHERE event_id=%s', (json.dumps(raw), 'source'))
    assert mirror.run(cfg=cfg).counts['a'].updated == 1
    assert raw['hangoutLink'] in patch.call_args.kwargs['body']['description']
    c.close()
