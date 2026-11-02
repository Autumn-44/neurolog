import io
import json
import sqlite3
import urllib.error
import zipfile
from unittest.mock import patch

import pytest
from werkzeug.exceptions import BadGateway
from LogIntel_engine.api import create_app
from LogIntel_engine import github_integration as gh

REPO = 'owner/project'


def test_default_repository_survives_restart_without_duplicates(tmp_path):
    config = {'TESTING': True, 'LOCAL_MODE': True,
              'DATABASE': str(tmp_path / 'seed.db'),
              'DEFAULT_GITHUB_REPOSITORIES': ['shahrukh-210906/WONDERLUST']}
    for _ in range(2):
        client = create_app(config).test_client()
        response = client.get('/api/github/repositories')
        assert response.status_code == 200
        assert [row['repo'] for row in response.json] == ['shahrukh-210906/wonderlust']

RUN = {'id': 123, 'run_attempt': 1, 'name': 'Tests', 'head_branch': 'main',
       'head_sha': 'a' * 40, 'conclusion': 'failure', 'created_at': '2026-10-31T00:00:00Z',
       'pull_requests': [{'number': 7}]}


def archive(files=None):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as output:
        for name, content in (files or {'test/1_Run.txt': 'Starting tests\n##[error] AssertionError: expected 200\nFailed test_checkout\n'}).items():
            output.writestr(name, content)
    return buffer.getvalue()


@pytest.fixture
def app(tmp_path):
    return create_app({'TESTING': True, 'DATABASE': str(tmp_path / 'test.db'), 'LOCAL_MODE': True})


@pytest.fixture
def remote():
    def response(path, token='', archive=False):
        if path.endswith('/logs'):
            return globals()['archive']()
        if '/attempts/' in path:
            return RUN
        if '/actions/runs?' in path:
            return {'workflow_runs': [RUN]}
        return {'full_name': REPO}
    with patch.object(gh, 'github_request', side_effect=response) as mock:
        yield mock


def connect(client, headers=None):
    return client.post('/api/github/connect', json={'repository': REPO}, headers=headers)


def test_connect_list_import_and_duplicate(app, remote):
    client = app.test_client()
    assert connect(client).status_code == 200
    assert client.get('/api/github/repositories').json[0]['repo'] == REPO
    runs = client.post('/api/github/runs', json={'repository': REPO}).json['runs']
    assert runs[0]['commit_url'].endswith('a' * 40)
    result = client.post('/api/github/import', json={'repository': REPO, 'run_id': 123})
    assert result.status_code == 201
    assert result.json['records'] == 3
    finding = result.json['summary']['findings'][0]
    assert finding['title'] == 'Test failure'
    assert finding['evidence'][0]['record_id'] > 0
    assert result.json['run']['pull_requests'][0]['url'].endswith('/pull/7')
    logs = client.get('/api/recent-logs').json
    assert len(logs) == 3 and all(log['source'] == 'github:owner/project' for log in logs)
    assert logs[0]['trace_id'] == 'github:123:1'
    before = remote.call_count
    duplicate = client.post('/api/github/import', json={'repository': REPO, 'run_id': 123})
    assert duplicate.json['already_imported'] is True
    assert remote.call_count == before
    assert len(client.get('/api/recent-logs').json) == 3
    assert len(create_app(app.config).test_client().get('/api/github/imports').json) == 1


@pytest.mark.parametrize('value', ['', None, 'https://evil.test/owner/repo', '../repo', 'owner/..', 'owner/repo?x=1', 'owner/repo/extra'])
def test_invalid_repository(app, value):
    assert app.test_client().post('/api/github/connect', json={'repository': value}).status_code == 400


def test_normalizes_repository():
    assert gh.repository_name('https://github.com/Owner/Repo.git/') == 'owner/repo'


def test_credentials_not_persisted_and_redacted(app, remote):
    client = app.test_client()
    secret = 'ghp_testNeverStoreThisCredential'
    client.post('/api/github/connect', json={'repository': REPO, 'token': secret})
    original = remote.side_effect
    remote.side_effect = lambda path, token='', archive=False: globals()['archive']({'test.txt': f'##[error] token {secret}\npassword=hidden\n'}) if archive else original(path, token)
    result = client.post('/api/github/import', json={'repository': REPO, 'run_id': 123, 'token': secret})
    assert result.status_code == 201
    with sqlite3.connect(app.config['DATABASE']) as connection:
        dump = '\n'.join(connection.iterdump())
    assert secret not in dump and 'hidden' not in dump
    assert secret not in json.dumps(result.json)
    assert '[REDACTED]' in dump


def test_archive_deduplicates_combined_logs_and_limits():
    rows, count = gh.parse_archive(archive({'0_job.txt': 'duplicate', 'job/1_step.txt': 'actual'}), '')
    assert count == 1 and rows[0]['message'] == 'actual'
    content = '\n'.join(['ordinary'] * 1600 + ['##[error] important'])
    rows, count = gh.parse_archive(archive({'step.txt': content}), '')
    assert count == 1601 and len(rows) == 1500
    assert rows[-1]['message'] == '##[error] important'
    with pytest.raises(ValueError, match='unreadable'):
        gh.parse_archive(b'not a zip', '')
    with patch.object(gh, 'MAX_EXPANDED', 5), pytest.raises(ValueError, match='limit'):
        gh.parse_archive(archive({'step.txt': 'too much content'}), '')


def test_failure_is_atomic_and_retryable(app, remote):
    client = app.test_client()
    connect(client)
    original = remote.side_effect
    remote.side_effect = lambda path, token='', archive=False: b'invalid' if archive else original(path, token)
    assert client.post('/api/github/import', json={'repository': REPO, 'run_id': 123}).status_code == 400
    assert client.get('/api/recent-logs').json == []
    assert client.get('/api/github/imports').json == []
    remote.side_effect = original
    assert client.post('/api/github/import', json={'repository': REPO, 'run_id': 123}).status_code == 201


def test_requires_connection_and_valid_failed_run(app, remote):
    client = app.test_client()
    assert client.post('/api/github/import', json={'repository': REPO, 'run_id': 123}).status_code == 404
    connect(client)
    for invalid in (True, -1, '123', 2**64):
        assert client.post('/api/github/import', json={'repository': REPO, 'run_id': invalid}).status_code == 400
    remote.side_effect = lambda *args, **kwargs: dict(RUN, conclusion='success')
    assert client.post('/api/github/import', json={'repository': REPO, 'run_id': 123}).status_code == 400
    assert client.get('/api/recent-logs').json == []


def test_user_isolation(app, remote):
    import firebase_admin
    from firebase_admin import auth
    app.config['LOCAL_MODE'] = False
    client = app.test_client()
    for url in ('/api/github/repositories', '/api/github/imports'):
        assert client.get(url).status_code == 401
    with patch.object(firebase_admin, 'get_app'), patch.object(auth, 'verify_id_token', side_effect=lambda token, **kw: {'uid': token}):
        a, b = {'Authorization': 'Bearer a'}, {'Authorization': 'Bearer b'}
        connect(client, a)
        assert client.get('/api/github/repositories', headers=b).json == []
        assert client.post('/api/github/import', json={'repository': REPO, 'run_id': 123}, headers=b).status_code == 404
        assert client.post('/api/github/import', json={'repository': REPO, 'run_id': 123}, headers=a).status_code == 201
        assert client.get('/api/github/imports', headers=b).json == []


@pytest.mark.parametrize('status, message', [(401, 'rejected'), (403, 'denied'), (404, 'not found'), (410, 'expired'), (429, 'rate limit')])
def test_http_errors_are_safe(status, message):
    with patch('urllib.request.OpenerDirector.open', side_effect=urllib.error.HTTPError('https://api.github.com', status, 'secret response', {}, None)):
        with pytest.raises(BadGateway, match=message):
            gh.github_request('/repos/owner/project', 'private-token')


def test_archive_redirect_does_not_forward_token():
    redirect = urllib.error.HTTPError('https://api.github.com', 302, '', {'Location': 'https://logs.blob.core.windows.net/archive?signature=x'}, None)
    response = io.BytesIO(b'zip bytes')
    with patch('urllib.request.OpenerDirector.open', side_effect=[redirect, response]) as opened:
        assert gh.github_request('/repos/owner/project/actions/runs/1/logs', 'private-token', archive=True) == b'zip bytes'
    assert opened.call_args_list[0].args[0].get_header('Authorization') == 'Bearer private-token'
    assert opened.call_args_list[1].args[0].get_header('Authorization') is None


def test_untrusted_archive_redirect_blocked():
    redirect = urllib.error.HTTPError('https://api.github.com', 302, '', {'Location': 'http://localhost/secrets'}, None)
    with patch('urllib.request.OpenerDirector.open', side_effect=redirect):
        with pytest.raises(BadGateway, match='unsupported'):
            gh.github_request('/repos/owner/project/actions/runs/1/logs', archive=True)


def test_delete_logs_clears_import_evidence_and_allows_reimport(app, remote):
    client = app.test_client()
    connect(client)
    assert client.post('/api/github/import', json={'repository': REPO, 'run_id': 123}).status_code == 201
    assert client.delete('/api/logs').status_code == 200
    assert client.get('/api/github/imports').json == []
    assert len(client.get('/api/github/repositories').json) == 1
    assert client.post('/api/github/import', json={'repository': REPO, 'run_id': 123}).status_code == 201


def test_rerun_attempt_is_separate(app, remote):
    client = app.test_client()
    connect(client)
    assert client.post('/api/github/import', json={'repository': REPO, 'run_id': 123}).status_code == 201
    original = remote.side_effect
    remote.side_effect = lambda path, token='', archive=False: dict(RUN, run_attempt=2) if path.endswith('/attempts/2') else original(path, token, archive)
    assert client.post('/api/github/import', json={'repository': REPO, 'run_id': 123, 'attempt': 2}).status_code == 201
    assert len(client.get('/api/github/imports').json) == 2
    assert len(client.get('/api/recent-logs').json) == 6


def test_unannotated_exception_is_evidence():
    rows, _ = gh.parse_archive(archive({'step.txt': "ModuleNotFoundError: No module named 'requests'"}), '')
    assert rows[0]['level'] == 3
    assert gh.summarize(rows)['findings'][0]['title'] == 'Dependency problem'


def tracked_run(**overrides):
    values = dict(RUN, workflow_id=77, head_repository={'id': 88}, event='push', status='completed')
    values.update(overrides)
    return values


def later_run(**overrides):
    values = dict(tracked_run(), id=124, run_attempt=1, head_sha='b' * 40,
                  created_at='2026-11-01T00:00:00Z', conclusion='success')
    values.update(overrides)
    return values


def assess(recent, original=None, comparison='ahead'):
    def response(path, token='', archive=False):
        if '/compare/' in path:
            return {'status': comparison}
        return {'workflow_runs': recent}
    with patch.object(gh, 'github_request', side_effect=response) as remote:
        result = gh.check_resolutions(REPO, [gh.run_details(REPO, original or tracked_run())], '', '2026-11-01T12:00:00Z')[0]
    return result, remote


def test_later_success_requires_descendant_commit():
    result, remote = assess([later_run()])
    assert result['state'] == 'likely_resolved'
    assert result['later_run']['sha'] == 'b' * 40
    assert result['checked_at'] and result['scope'] == 'workflow'
    assert 'individual error fixes' in result['reason']
    assert any('/compare/' in call.args[0] for call in remote.call_args_list)
    for status in ('diverged', 'behind', 'identical'):
        result, _ = assess([later_run()], comparison=status)
        assert result['state'] == 'unverified'


@pytest.mark.parametrize('changes', [
    {'head_sha': 'a' * 40}, {'head_branch': 'different'}, {'workflow_id': 78},
    {'event': 'pull_request'}, {'head_repository': {'id': 999}},
    {'created_at': '2026-10-30T00:00:00Z'}, {'id': 123, 'run_attempt': 2},
])
def test_unrelated_or_same_commit_run_never_resolves(changes):
    result, remote = assess([later_run(**changes)])
    assert result['state'] == 'unverified'
    assert not any('/compare/' in call.args[0] for call in remote.call_args_list)


@pytest.mark.parametrize('conclusion', ['failure', 'timed_out', 'action_required'])
def test_latest_failure_overrides_earlier_success(conclusion):
    newest = later_run(id=125, head_sha='c' * 40, created_at='2026-11-01T01:00:00Z', conclusion=conclusion)
    result, _ = assess([later_run(), newest])
    assert result['state'] == 'still_failing'
    assert result['later_run']['id'] == 125
    assert 'error may have changed' in result['reason']


@pytest.mark.parametrize('changes', [{'status': 'in_progress', 'conclusion': None}, {'conclusion': 'cancelled'}, {'conclusion': 'skipped'}, {'conclusion': 'neutral'}])
def test_inconclusive_latest_run_does_not_reuse_old_success(changes):
    newest = later_run(id=125, head_sha='c' * 40, created_at='2026-11-01T01:00:00Z', **changes)
    result, _ = assess([later_run(), newest])
    assert result['state'] == 'unverified'
    assert result['later_run']['id'] == 125


def test_pr_numbers_must_match():
    original = tracked_run(event='pull_request')
    result, _ = assess([later_run(event='pull_request', pull_requests=[{'number': 8}])], original)
    assert result['state'] == 'unverified'
    result, _ = assess([later_run(event='pull_request')], original)
    assert result['state'] == 'likely_resolved'


def test_missing_metadata_and_empty_history_are_unknown():
    for original in (RUN, tracked_run(head_repository=None)):
        result, _ = assess([later_run()], original)
        assert result['state'] == 'unverified'
    result, _ = assess([])
    assert result['state'] == 'unverified' and 'latest 100' in result['reason']


def test_resolution_api_failure_is_unknown_not_a_fix():
    with patch.object(gh, 'github_request', side_effect=BadGateway('Rate limit reached')):
        result = gh.check_resolutions(REPO, [gh.run_details(REPO, tracked_run())], '', 'now')[0]
    assert result['state'] == 'unverified' and 'Rate limit' in result['reason']
    def response(path, *args, **kwargs):
        if '/compare/' in path:
            raise BadGateway('Denied')
        return {'workflow_runs': [later_run()]}
    with patch.object(gh, 'github_request', side_effect=response):
        result = gh.check_resolutions(REPO, [gh.run_details(REPO, tracked_run())], '', 'now')[0]
    assert result['state'] == 'unverified' and 'Contents: Read' in result['reason']


def test_statuses_persist_refresh_and_clear(app, remote):
    client = app.test_client()
    connect(client)
    original = remote.side_effect
    current = [later_run()]
    def response(path, token='', archive=False):
        if 'status=failure' in path:
            return {'workflow_runs': [tracked_run()]}
        if '/compare/' in path:
            return {'status': 'ahead'}
        if '/actions/runs?' in path:
            return {'workflow_runs': current}
        if '/attempts/' in path and not archive:
            return tracked_run()
        return original(path, token, archive)
    remote.side_effect = response
    listed = client.post('/api/github/runs', json={'repository': REPO})
    assert listed.json['runs'][0]['resolution']['state'] == 'likely_resolved'
    assert client.post('/api/github/import', json={'repository': REPO, 'run_id': 123}).status_code == 201
    assert client.get('/api/github/imports').json[0]['resolution']['state'] == 'likely_resolved'
    current[0] = later_run(conclusion='failure')
    updated = client.post('/api/github/resolution', json={'repository': REPO, 'run_id': 123})
    assert updated.json['state'] == 'still_failing'
    assert create_app(app.config).test_client().get('/api/github/imports').json[0]['resolution']['state'] == 'still_failing'
    for invalid in (True, -1, '123'):
        assert client.post('/api/github/resolution', json={'repository': REPO, 'run_id': invalid}).status_code == 400
    client.delete('/api/logs')
    with sqlite3.connect(app.config['DATABASE']) as connection:
        assert connection.execute('SELECT COUNT(*) FROM github_resolutions').fetchone()[0] == 0


def test_resolution_endpoint_respects_workspace_access(app, remote):
    import firebase_admin
    from firebase_admin import auth
    app.config['LOCAL_MODE'] = False
    client = app.test_client()
    payload = {'repository': REPO, 'run_id': 123}
    assert client.post('/api/github/resolution', json=payload).status_code == 401
    with patch.object(firebase_admin, 'get_app'), patch.object(auth, 'verify_id_token', side_effect=lambda token, **kw: {'uid': token}):
        connect(client, {'Authorization': 'Bearer a'})
        assert client.post('/api/github/resolution', json=payload, headers={'Authorization': 'Bearer b'}).status_code == 404
