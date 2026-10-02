from __future__ import annotations

import csv
import io
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from eso_object_types.database import SCHEMA
from eso_object_types.dashboard import DashboardReader, compact_moc, serve_dashboard


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
    assert first['total'] == 55 and len(first['rows']) == 10
    assert len(second['rows']) == 10
    assert {r['eso_dp_id'] for r in first['rows']}.isdisjoint(r['eso_dp_id'] for r in second['rows'])
    assert reader.results(params={'page':'999'})['page'] == 6
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
    reader = DashboardReader(database_path)
    coverage = reader.sky()
    assert coverage['mode'] == 'moc'
    assert coverage['covered_spectra'] == coverage['total_spectra'] == 5056
    assert not coverage['positions']
    assert sum(item['count'] for item in coverage['coverage']) == 5056
    data = reader.sky(params={'sky_mode':'points'})
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


@pytest.mark.parametrize('size,expected', [('10',10),('50',50),('100',55),('1000',55),('all',55)])
def test_page_sizes(database_path, size, expected):
    data = DashboardReader(database_path).results(params={'page_size':size})
    assert len(data['rows']) == expected
    assert data['page_size'] == (1000 if size=='all' else int(size))


def test_invalid_sizes_and_modes(database_path):
    reader = DashboardReader(database_path)
    with pytest.raises(ValueError,match='page_size'): reader.results(params={'page_size':'2000000'})
    with pytest.raises(ValueError,match='sky_mode'): reader.sky(params={'sky_mode':'invalid'})


def test_moc_merges_siblings_without_inventing_coverage(database_path):
    assert compact_moc(range(16),2) == {'0':[0]}
    assert compact_moc([0,1,2,4],2) == {'2':[0,1,2,4]}
    assert compact_moc([0,1,2,3,4],2) == {'1':[0], '2':[4]}
    data = DashboardReader(database_path).sky(params={'sky_mode':'moc','category':'Star'})
    assert data['covered_spectra'] == 1
    assert data['coverage'] == [{'category':'Star','count':1,'moc':{'5':[0]}}]


def test_cache_invalidates_on_wal_commit_and_returns_independent_data(database_path):
    reader = DashboardReader(database_path)
    writer = sqlite3.connect(database_path)
    try:
        writer.execute('PRAGMA journal_mode=WAL')
        before = reader.snapshot()
        before['stats']['observations'] = -1
        assert reader.snapshot()['stats']['observations'] == 55
        reader.results(); reader.sky()
        writer.execute("INSERT INTO observations VALUES ('added',NULL,'Added',20,-20,NULL,NULL,0.001,'NEW',NULL,2048,'now')")
        writer.execute("INSERT INTO run_observations VALUES ('new','added')")
        writer.commit()
        assert reader.snapshot()['stats']['observations'] == 56
        assert reader.results()['total'] == 56
        assert reader.sky()['total_positions'] == 55
    finally:
        writer.close()


def test_all_is_bounded_and_moc_handles_full_sky(database_path):
    with sqlite3.connect(database_path) as c:
        rows = [(f'big-{i}',None,'Extra',0,-30,None,None,0.001,'TEST',None,i*1024,'now') for i in range(12288)]
        c.executemany('INSERT INTO observations VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',rows)
        c.executemany('INSERT INTO run_observations VALUES (?,?)',[('new',row[0]) for row in rows])
    reader = DashboardReader(database_path)
    data = reader.results(params={'page_size':'all'})
    assert len(data['rows']) == 1000 and data['total'] == 12343
    sky = reader.sky()
    pending = next(item for item in sky['coverage'] if item['category']=='Pending')
    assert pending['moc'] == {'0':list(range(12))}
    assert sky['covered_spectra'] == 12343


def test_results_order_uses_run_membership_index(database_path):
    from eso_object_types.dashboard import JOIN, FIELDS
    reader = DashboardReader(database_path)
    with reader.connect() as c:
        plan = [row['detail'] for row in c.execute(
            f'EXPLAIN QUERY PLAN SELECT {FIELDS} {JOIN} WHERE r.run_id=? ORDER BY r.eso_dp_id LIMIT 10', ('new',))]
    assert not any('TEMP B-TREE FOR ORDER BY' in row for row in plan)


def fix_test_healpix(path):
    from astropy import units as u
    from astropy_healpix import HEALPix
    hpx = HEALPix(nside=1024, order='nested')
    with sqlite3.connect(path) as c:
        rows = c.execute('SELECT eso_dp_id,ra_deg,dec_deg FROM observations').fetchall()
        c.executemany('UPDATE observations SET healpix_order10=? WHERE eso_dp_id=?',
                      [(int(hpx.lonlat_to_healpix(ra*u.deg,dec*u.deg)),product) for product,ra,dec in rows])


def test_adaptive_sky_refines_then_loads_clickable_positions(database_path):
    fix_test_healpix(database_path)
    reader = DashboardReader(database_path)
    assert reader.sky(params={'sky_mode':'moc'})['moc_order'] == 5
    params = {'sky_mode':'moc','view_ra':'10','view_dec':'-20','view_radius':'20'}
    finer = reader.sky(params=params)
    assert finer['moc_order'] == 7 and finer['covered_spectra'] == 55
    params['view_radius'] = '1'
    points = reader.sky(params=params)
    assert points['mode'] == 'points' and points['total_spectra'] == 55
    assert points['positions'][0]['eso_dp_id'] == 'ESO-000'
    params['view_ra'] = '180'
    assert reader.sky(params=params)['positions'] == []


@pytest.mark.parametrize('params',[{'view_ra':'10'}, {'view_ra':'NaN','view_dec':'0','view_radius':'1'}, {'view_ra':'0','view_dec':'91','view_radius':'1'}, {'view_ra':'0','view_dec':'0','view_radius':'0'}])
def test_invalid_viewport(database_path, params):
    with pytest.raises(ValueError): DashboardReader(database_path).sky(params=params)


def test_viewport_handles_ra_wrap_and_pole(database_path):
    with sqlite3.connect(database_path) as c:
        c.execute("UPDATE observations SET ra_deg=359.9,dec_deg=0 WHERE eso_dp_id='ESO-000'")
        c.execute("UPDATE observations SET ra_deg=0.1,dec_deg=0 WHERE eso_dp_id='ESO-001'")
        c.execute("UPDATE observations SET ra_deg=180,dec_deg=89.99 WHERE eso_dp_id='ESO-002'")
    fix_test_healpix(database_path)
    reader = DashboardReader(database_path)
    wrap = reader.sky(params={'view_ra':'0','view_dec':'0','view_radius':'0.2'})
    assert {row['eso_dp_id'] for row in wrap['positions']} == {'ESO-000','ESO-001'}
    pole = reader.sky(params={'view_ra':'0','view_dec':'90','view_radius':'0.1'})
    assert [row['eso_dp_id'] for row in pole['positions']] == ['ESO-002']


def test_dense_viewport_stays_bounded_and_refines_to_order10(database_path):
    with sqlite3.connect(database_path) as c:
        rows = [(f'dense-{i}',None,'Extra',10+i*0.00001,-20,None,None,0.001,'TEST',None,1,'now') for i in range(2501)]
        c.executemany('INSERT INTO observations VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',rows)
        c.executemany('INSERT INTO run_observations VALUES (?,?)',[('new',row[0]) for row in rows])
    fix_test_healpix(database_path)
    reader = DashboardReader(database_path)
    data = reader.sky(params={'view_ra':'10','view_dec':'-20','view_radius':'1'})
    assert data['mode'] == 'moc' and data['moc_order'] == 10
    assert data['covered_spectra'] == 2556 and not data['positions']
    data = reader.sky(params={'view_ra':'10','view_dec':'-20','view_radius':'0.001'})
    assert data['mode'] == 'points' and len(data['positions']) < 2000


def test_region_query_uses_spatial_index(database_path):
    from eso_object_types.dashboard import sky_region, JOIN
    region, values, _ = sky_region({'view_ra':'10','view_dec':'-20','view_radius':'1'})
    with DashboardReader(database_path).connect() as c:
        plan = [row['detail'] for row in c.execute(f'EXPLAIN QUERY PLAN SELECT COUNT(*) {JOIN} WHERE r.run_id=? {region}', ['new',*values])]
    assert any('idx_observations_hpx10' in row for row in plan)
