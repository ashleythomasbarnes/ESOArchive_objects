from __future__ import annotations

import csv
import io
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from eso_object_types.database import SCHEMA
from eso_object_types.dashboard import DashboardReader, serve_dashboard


@pytest.fixture
def database_path(tmp_path):
    path = tmp_path / 'monitor.sqlite'
    c = sqlite3.connect(path)
    c.executescript(SCHEMA)
    c.execute("INSERT INTO pipeline_runs VALUES (?,?,?,?,?,?)", ('old', '2026-09-29T10:00:00+00:00', '2026-09-29T10:00:05+00:00', 'completed', '{}', None))
    c.execute("INSERT INTO pipeline_runs VALUES (?,?,?,?,?,?)", ('new', '2026-10-01T10:00:00+00:00', '2026-10-01T10:00:10+00:00', 'partial', '{}', None))
    observations = [(f'ESO-{i:03}', None, 'Literal_100%' if i == 0 else f'Target {i}', 10.0 if i < 2 else 10+i/100, -20.0, None, None, 1/3600, 'EFOSC' if i%2 else 'SOFI', None, 1, 'now') for i in range(55)]
    c.executemany('INSERT INTO observations VALUES (?,?,?,?,?,?,?,?,?,?,?,?)', observations)
    c.executemany('INSERT INTO run_observations VALUES (?,?)', [('new', r[0]) for r in observations] + [('old', observations[0][0])])
    # One selected match, one completed unmatched result; the other 53 are pending.
    for i, key in [(0, 'simbad:1'), (1, None)]:
        c.execute('''INSERT INTO observation_best_objects (
            run_id,eso_dp_id,best_object_key,best_object_name,broad_category,confidence,
            match_method,candidate_group_count,alias_complete,ranking_version,taxonomy_version,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''', ('new', f'ESO-{i:03}', key, 'Selected star' if key else None, 'Star' if key else 'Unknown', 'high' if key else 'none', 'position', 1 if key else 0, 1, '1','1','now'))
    c.execute("INSERT INTO catalog_objects (catalog,catalog_object_id,preferred_name,ra_deg,dec_deg,updated_at) VALUES ('simbad','1','Selected star',10,-20,'now')")
    c.execute("INSERT INTO observation_objects VALUES ('ESO-000','simbad','1',0.1,'new','new','now')")
    c.execute("INSERT INTO observation_best_object_members VALUES ('new','ESO-000','simbad','1','primary')")
    c.execute('''INSERT INTO service_calls (run_id,service,batch_hash,batch_number,input_count,attempt_count,status,started_at,finished_at,elapsed_seconds,error_type,error_message)
        VALUES ('new','simbad','batch',1,55,3,'failed','2026-10-01T10:00:01+00:00','2026-10-01T10:00:09+00:00',8,'TimeoutError','Service timed out')''')
    c.commit(); c.close()
    return path


def test_latest_health_is_independent_of_selected_history(database_path):
    reader = DashboardReader(database_path)
    data = reader.snapshot('old')
    assert data['selected_run']['run_id'] == 'old'
    assert data['health']['latest_run']['run_id'] == 'new'
    assert data['health']['failed_batches'] == 1
    assert data['health']['retries'] == 2
    assert data['totals']['observations'] == 55
    assert data['stats']['observations'] == 1
    assert data['stats']['pending'] == 1


@pytest.mark.parametrize('status', ['completed', 'partial', 'running'])
def test_run_states_and_pending_are_not_unmatched(database_path, status):
    with sqlite3.connect(database_path) as c:
        c.execute('UPDATE pipeline_runs SET status=?,finished_at=? WHERE run_id=?', (status,None if status=='running' else '2026-10-01T10:00:10+00:00','new'))
    data = DashboardReader(database_path).snapshot(now=datetime(2026,10,1,11,tzinfo=UTC))
    assert data['health']['latest_run']['status'] == status
    assert data['stats']['matches'] == 1
    assert data['stats']['unmatched'] == 1
    assert data['stats']['pending'] == 53
    assert sum(row['count'] for row in data['stats']['categories']) == 55
    assert data['history'][0]['duration_seconds'] == (3600 if status=='running' else 10)


def test_freshness_manual_and_daily_with_grace(database_path):
    now = datetime(2026,9,29,10,0,5,tzinfo=UTC)
    assert not DashboardReader(database_path).snapshot(now=now+timedelta(days=5))['health']['overdue']
    reader = DashboardReader(database_path,24)
    assert not reader.snapshot(now=now+timedelta(hours=26))['health']['overdue']
    assert reader.snapshot(now=now+timedelta(hours=26,seconds=1))['health']['overdue']
    with sqlite3.connect(database_path) as c:
        c.execute("UPDATE pipeline_runs SET status='partial'")
    assert reader.snapshot(now=now)['health']['overdue']


def test_filters_pagination_literal_search_and_csv(database_path):
    reader = DashboardReader(database_path)
    first = reader.results(); second = reader.results(params={'page':'2'})
    assert first['total'] == 55 and len(first['rows']) == 50
    assert len(second['rows']) == 5
    assert {r['eso_dp_id'] for r in first['rows']}.isdisjoint(r['eso_dp_id'] for r in second['rows'])
    assert reader.results(params={'page':'999'})['page'] == 2
    assert reader.results(params={'search':'_100%'})['total'] == 1
    assert reader.results(params={'search':"' OR 1=1 --"})['total'] == 0
    params = {'instrument':'SOFI','confidence':'pending'}
    filtered = reader.results(params=params)
    assert filtered['total'] == 27
    rows = list(csv.DictReader(io.StringIO(b''.join(reader.export_csv(params=params)).decode())))
    assert len(rows) == filtered['total']
    assert all(row['instrument_name']=='SOFI' and row['confidence']=='pending' for row in rows)
    assert reader.results(params={'category':'Unknown'})['total'] == 1
    with pytest.raises(ValueError): reader.results(params={'page':'NaN'})


def test_sky_grouping_filter_and_catalogue_layer(database_path):
    reader = DashboardReader(database_path)
    sky = reader.sky()
    assert sky['total_positions'] == 54
    assert sky['positions'][0]['count'] == 2
    assert sky['positions'][0]['category'] == 'Mixed'
    assert sky['total_objects'] == 1
    assert len(reader.sky(params={'category':'Unknown'})['positions']) == 1
    detail = reader.detail('new','ESO-000')
    assert detail['search_radius_deg'] == pytest.approx(1/3600)
    assert detail['coincident_products'] == ['ESO-000','ESO-001']
    assert detail['candidates'][0]['preferred_name'] == 'Selected star'
    with pytest.raises(ValueError,match='not found'): reader.detail('old','ESO-001')


def test_sky_limit_is_explicit(database_path):
    with sqlite3.connect(database_path) as c:
        extra = [(f'X-{i}',None,'Extra',i/100,-30,None,None,1/3600,'TEST',None,1,'now') for i in range(5001)]
        c.executemany('INSERT INTO observations VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',extra)
        c.executemany('INSERT INTO run_observations VALUES (?,?)',[('new',row[0]) for row in extra])
    data = DashboardReader(database_path).sky()
    assert data['total_positions'] == 5055
    assert len(data['positions']) == 5000


def test_reader_is_read_only_and_does_not_migrate(database_path):
    original = database_path.read_bytes()
    reader = DashboardReader(database_path)
    reader.snapshot(); reader.results(); reader.sky(); reader.detail(None,'ESO-000')
    with reader.connect() as connection:
        with pytest.raises(sqlite3.OperationalError,match='readonly'):
            connection.execute('DELETE FROM pipeline_runs')
    assert database_path.read_bytes() == original


def test_empty_missing_and_incompatible_database(tmp_path):
    missing = tmp_path / 'missing.sqlite'
    with pytest.raises(ValueError,match='Database not found'): DashboardReader(missing).snapshot()
    assert not missing.exists()
    empty = tmp_path/'empty.sqlite'
    with sqlite3.connect(empty) as c: c.executescript(SCHEMA)
    reader = DashboardReader(empty)
    assert reader.snapshot()['selected_run'] is None
    assert reader.results()['total'] == 0
    assert reader.sky()['positions'] == []
    with pytest.raises(ValueError,match='does not exist'): reader.snapshot('bad-run')
    bad = tmp_path/'bad.sqlite'
    with sqlite3.connect(bad) as c: c.execute('CREATE TABLE pipeline_runs(run_id TEXT)')
    original = bad.read_bytes()
    with pytest.raises(ValueError,match='Incompatible'): DashboardReader(bad).snapshot()
    assert bad.read_bytes() == original


@pytest.mark.parametrize('port,interval', [(0,None),(65536,None),(8765,0),(8765,float('nan')),(8765,float('inf'))])
def test_invalid_server_options_rejected_before_binding(port,interval):
    with pytest.raises(ValueError): serve_dashboard('unused.sqlite',port,interval)
