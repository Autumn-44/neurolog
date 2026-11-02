import sqlite3
from datetime import datetime, timezone, timedelta
from LogIntel_engine.api import create_app
from LogIntel_engine.intelligence import investigate, vector_space


def sample(id, source='web', generated=False, entity='person', minutes=0):
    return dict(_id=id, timestamp=(datetime.now(timezone.utc)-timedelta(minutes=minutes)).isoformat(),
        source=source, entity_id=entity, trace_id='trace-1', severity_level=3,
        severity_label='ERROR', ml_anomaly=0, cluster_id=None,
        message=('[GENERATED] ' if generated else '')+'Database connection timeout duration_ms='+str(id*10))


def test_recurrence_window_origin_entity_and_explainable_score():
    records=[sample(1),sample(2),sample(3),sample(4,generated=True),sample(5,minutes=10)]
    result=investigate(records)
    first=result['records'][0]
    assert first['recurrence'] == 3
    assert first['risk_score'] == 70
    assert first['risk_score'] == sum(f['points'] for f in first['factors'])
    assert result['records'][3]['recurrence'] == 1
    muted=investigate(records,[first['signature']])
    assert muted['records'][0]['risk_score'] == 25
    assert len(result['incidents']) == 2
    assert result['insights'][0]['count'] == 3


def test_real_vectors_neighbors_and_empty_vocabulary():
    records=[sample(1),sample(2),{**sample(3),'message':'Health check passed successfully'}]
    vectors=vector_space(records,1)
    assert vectors['neighbors'][0]['id'] == 2
    assert vectors['neighbors'][0]['similarity'] > .99
    assert vectors['tokens']
    assert len(vectors['points']) == 3
    assert vector_space([])['points'] == []
    assert vector_space([{**sample(1),'message':'the and a'}])['dimensions'] == 0


def test_rules_metadata_suppression_and_persistence(tmp_path):
    config={'TESTING':True,'LOCAL_MODE':True,'DATABASE':str(tmp_path/'intel.db')}
    app=create_app(config); client=app.test_client()
    key=client.post('/api/generate-key',json={}).json['key']
    for i in range(3):
        assert client.post('/api/ingest',headers={'x-api-key':key},json=dict(message='Database timeout '+str(i), source='web',severity_level=3,entity_id='entity-1',trace_id='trace-1')).status_code == 201
    assert client.post('/api/alert-rules',json={'condition':'unknown','action':'slack'}).status_code == 400
    client.post('/api/alert-rules',json={'condition':'database_error','action':'slack'})
    result=client.get('/api/intelligence').json
    assert result['records'][0]['entity'] == 'entity-1'
    assert result['records'][0]['trace_id'] == 'trace-1'
    assert result['alerts'][0]['status'] == 'Payload preview; not dispatched'
    sig=result['records'][0]['signature']
    assert client.post('/api/suppressions',json={'signature':sig,'suppressed':True}).status_code == 200
    new_client=create_app(config).test_client()
    assert new_client.get('/api/intelligence').json['records'][0]['suppressed']
    new_client.post('/api/suppressions',json={'signature':sig,'suppressed':False})
    assert not new_client.get('/api/intelligence').json['records'][0]['suppressed']
    rule=new_client.get('/api/alert-rules').json[0]
    new_client.delete('/api/alert-rules/'+str(rule['id']))
    assert new_client.get('/api/alert-rules').json == []
    assert new_client.get('/api/vector-space?record_id=bad').status_code == 400
    assert new_client.get('/api/vector-space?record_id=1').json['tokens']


def test_existing_database_migrates_without_losing_logs(tmp_path):
    path=tmp_path/'old.db'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE logs (_id INTEGER PRIMARY KEY, uid TEXT NOT NULL, timestamp TEXT NOT NULL, source TEXT NOT NULL, severity_level INTEGER NOT NULL,severity_label TEXT NOT NULL,message TEXT NOT NULL,ml_anomaly INTEGER DEFAULT 0,cluster_id INTEGER DEFAULT NULL)')
        db.execute('INSERT INTO logs(uid,timestamp,source,severity_level,severity_label,message) VALUES(?,?,?,?,?,?)',('local-workspace',datetime.now(timezone.utc).isoformat(),'old',6,'INFO','Preserved log'))
    client=create_app({'LOCAL_MODE':True,'DATABASE':str(path)}).test_client()
    assert client.get('/api/recent-logs').json[0]['message'] == 'Preserved log'
    assert client.get('/api/intelligence').json['records'][0]['entity'] == 'old'

def test_auth_patterns_and_metadata_validation(tmp_path):
    client=create_app({'LOCAL_MODE':True,'DATABASE':str(tmp_path/'auth.db')}).test_client()
    key=client.post('/api/generate-key',json={}).json['key']
    headers={'x-api-key':key}
    for i in range(3):
        client.post('/api/ingest',headers=headers,json={'message':'[GENERATED] Authentication failed invalid token attempt='+str(i),'severity_level':3,'entity_id':'scenario-user','source':'auth'})
    client.post('/api/alert-rules',json={'condition':'auth_repeated','action':'in_app'})
    data=client.get('/api/intelligence').json
    assert data['incidents'][0]['category'] == 'auth'
    assert data['records'][0]['escalated']
    assert data['alerts'][0]['status'] == 'In-app notification'
    assert client.post('/api/ingest',headers=headers,json={'message':'bad','entity_id':[]}).status_code == 400


def burst(start, count, generated=False, source='web'):
    minute=(datetime.now(timezone.utc)-timedelta(minutes=2)).replace(second=0,microsecond=0)
    return [{**sample(start+i,source=source,generated=generated),'timestamp':(minute+timedelta(seconds=5*i)).isoformat()} for i in range(count)]


def test_volume_spike_against_own_baseline():
    quiet=[sample(i,minutes=m) for i,m in enumerate(range(6,21),start=100)]
    result=investigate(burst(1,8)+quiet)
    assert len(result['spikes']) == 1 and result['metrics']['spikes'] == 1
    spike=result['spikes'][0]
    assert spike['count'] == 8 and spike['baseline_median'] == 1 and spike['threshold'] == 5
    first=result['records'][0]
    assert first['spike'] and first['factors'][-1]['points'] == 15
    assert first['risk_score'] == sum(f['points'] for f in first['factors']) == 85
    assert not result['records'][-1]['spike']
    assert result['incidents'][0]['spike']
    assert any(i['title'] == 'Error volume spike' and i['count'] == 8 for i in result['insights'])


def test_spike_needs_baseline_and_keeps_generated_separate():
    assert investigate(burst(1,8))['spikes'] == []
    quiet=[sample(i,minutes=m) for i,m in enumerate(range(6,21),start=100)]
    assert investigate(burst(1,4)+quiet)['spikes'] == []
    result=investigate(burst(1,8,generated=True)+quiet)
    assert [s['generated'] for s in result['spikes']] == [True]
    assert not any(r['spike'] for r in result['records'] if not r['generated'])
