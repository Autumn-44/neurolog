"""Read-only GitHub Actions import. Credentials are request-only, never persisted."""
import io
import json
import re
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from flask import jsonify
from werkzeug.exceptions import BadGateway, NotFound

MAX_DOWNLOAD = 8 * 1024 * 1024
MAX_EXPANDED = 12 * 1024 * 1024
MAX_RECORDS = 1500
FAILURES = {'failure', 'timed_out', 'action_required', 'startup_failure'}
ANSI = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')
ERROR = re.compile(r'##\[error\]|\b(error|failed|failure|exception|traceback|fatal|\w+Error|\w+Exception|timeout|timed out|ECONNREFUSED|ENOTFOUND|ERESOLVE|TS\d{4})\b', re.I)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def repository_name(value):
    if not isinstance(value, str):
        raise ValueError('Enter a repository as owner/name or a GitHub repository URL.')
    value = value.strip()
    if value.startswith('https://github.com/'):
        value = value.removeprefix('https://github.com/').rstrip('/')
    value = value.removesuffix('.git')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}', value) or value.split('/')[1] in ('.', '..'):
        raise ValueError('Enter a repository as owner/name or a GitHub repository URL.')
    return value.lower()


def redact(text, token=''):
    if token:
        text = text.replace(token, '[REDACTED]')
    text = re.sub(r'\b(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+)\b', '[REDACTED]', text)
    return re.sub(r'(?i)\b(authorization\s*[:=]\s*(?:bearer|token)\s+|(?:password|secret|api[_-]?key|access[_-]?token)\s*[:=]\s*)[^\s,;]+', r'\1[REDACTED]', text)


def github_request(path, token='', archive=False):
    headers = {'Accept': 'application/vnd.github+json', 'User-Agent': 'NeuroLog-Actions', 'X-GitHub-Api-Version': '2026-03-10'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    opener = urllib.request.build_opener(NoRedirect)
    try:
        try:
            response = opener.open(urllib.request.Request('https://api.github.com' + path, headers=headers), timeout=12)
        except urllib.error.HTTPError as error:
            if archive and error.code in (301, 302, 307):
                location = error.headers.get('Location', '')
                parsed = urllib.parse.urlparse(location)
                host = parsed.hostname or ''
                if parsed.scheme != 'https' or parsed.username or parsed.password or parsed.port not in (None, 443) or not (host.endswith('.blob.core.windows.net') or host.endswith('.githubusercontent.com')):
                    raise BadGateway('GitHub returned an unsupported log download location.') from None
                # Never forward the GitHub credential to archive storage.
                response = opener.open(urllib.request.Request(location, headers={'User-Agent': 'NeuroLog-Actions'}), timeout=12)
            else:
                raise
        with response:
            content = response.read(MAX_DOWNLOAD + 1)
        if len(content) > MAX_DOWNLOAD:
            raise BadGateway('This log archive exceeds the 8 MB import limit. Choose a smaller run.')
        return content if archive else json.loads(content)
    except urllib.error.HTTPError as error:
        messages = {401: 'GitHub rejected the token. Enter a valid token.',
                    403: 'GitHub denied access or its rate limit was reached. Check Actions read permission and try again later.',
                    404: 'Repository, run, or logs not found. Check access; expired logs cannot be imported.',
                    410: 'These workflow logs have expired or were deleted.',
                    429: 'GitHub rate limit reached. Try again later.'}
        raise BadGateway(messages.get(error.code, 'GitHub could not complete the request. Try again later.')) from None
    except (OSError, ValueError, urllib.error.URLError):
        raise BadGateway('GitHub could not be reached or returned an invalid response. Try again.') from None


def run_details(repo, run):
    run_id = int(run['id'])
    sha = run.get('head_sha', '')
    sha = sha if re.fullmatch(r'[0-9a-fA-F]{40}', sha) else ''
    return {'id': run_id, 'attempt': int(run.get('run_attempt', 1)),
            'workflow_id': run.get('workflow_id'), 'event': run.get('event'),
            'head_repository_id': (run.get('head_repository') or {}).get('id'),
            'run_number': run.get('run_number'), 'status': run.get('status'),
            'name': str(run.get('name') or 'Workflow'), 'branch': str(run.get('head_branch') or 'Unknown'),
            'conclusion': run.get('conclusion'), 'created_at': run.get('created_at'),
            'sha': sha, 'url': f'https://github.com/{repo}/actions/runs/{run_id}',
            'commit_url': f'https://github.com/{repo}/commit/{sha}' if sha else None,
            'pull_requests': [{'number': pr['number'], 'url': f"https://github.com/{repo}/pull/{pr['number']}"}
                              for pr in run.get('pull_requests', []) if type(pr.get('number')) is int]}


def unchecked_resolution():
    return {'state': 'unverified', 'label': 'Not verified', 'checked_at': None,
            'reason': 'Check later commits to see whether this workflow recovered.', 'later_run': None}


def workflow_identity(run):
    """Do not mix workflows, branches, triggers, or fork pull requests."""
    fields = (run.get('workflow_id'), run.get('branch'), run.get('event'), run.get('head_repository_id'))
    if not all(fields) or run.get('branch') == 'Unknown':
        return None
    prs = tuple(sorted(pr['number'] for pr in run.get('pull_requests', [])))
    if run['event'].startswith('pull_request') and not prs:
        return None
    return (*fields, prs)


def later_candidate(original, recent):
    identity = workflow_identity(original)
    if not identity or not original.get('sha') or not original.get('created_at'):
        return None
    candidates = [run for run in recent if workflow_identity(run) == identity
                  and run.get('sha') and run['sha'] != original['sha']
                  and run.get('created_at', '') > original['created_at']
                  and run['id'] != original['id']]
    return max(candidates, key=lambda run: (run['created_at'], run['id']), default=None)


def check_resolutions(repo, originals, token, checked_at):
    """Bounded snapshot, with commit ancestry verified before inferring recovery."""
    def result(reason, candidate=None, state='unverified'):
        labels = {'unverified': 'Not verified', 'likely_resolved': 'Likely resolved',
                  'still_failing': 'Workflow still failing'}
        return {'state': state, 'label': labels[state], 'reason': reason, 'checked_at': checked_at,
                'later_run': candidate, 'scope': 'workflow', 'scan_limit': 100}

    try:
        response = github_request(f'/repos/{repo}/actions/runs?per_page=100', token)
        recent = [run_details(repo, run) for run in response['workflow_runs']]
    except BadGateway as error:
        return [result('Could not check later runs. ' + error.description) for _ in originals]

    candidates = [later_candidate(run, recent) for run in originals]
    pairs = {(run['sha'], candidate['sha']) for run, candidate in zip(originals, candidates)
             if candidate and candidate.get('status') == 'completed'
             and candidate.get('conclusion') in FAILURES | {'success'}}

    def compare(pair):
        try:
            comparison = github_request(f'/repos/{repo}/compare/{pair[0]}...{pair[1]}?per_page=1', token)
            return pair, comparison.get('status') == 'ahead', None
        except BadGateway:
            return pair, False, 'Could not verify commit ancestry. Check repository access and Contents: Read permission, then retry.'

    # Network reads only; database writes stay on the request thread.
    with ThreadPoolExecutor(max_workers=4) as pool:
        ancestry = {pair: (ahead, error) for pair, ahead, error in pool.map(compare, pairs)}
    results = []
    for original, candidate in zip(originals, candidates):
        if not workflow_identity(original):
            results.append(result('Workflow, branch, trigger, or source repository metadata is missing; recovery cannot be verified.'))
        elif not candidate:
            results.append(result('No comparable run on a different later commit was found among the latest 100 repository runs. Reruns of the same commit do not count as a fix.'))
        elif candidate.get('status') != 'completed' or candidate.get('conclusion') not in FAILURES | {'success'}:
            results.append(result('The latest comparable run on a different commit is pending, skipped, or cancelled; it does not verify a fix.', candidate))
        else:
            ahead, error = ancestry[(original['sha'], candidate['sha'])]
            if not ahead:
                results.append(result(error or 'The later run is not on a descendant of this commit. Its result does not verify a fix.', candidate))
            elif candidate['conclusion'] == 'success':
                results.append(result('The same workflow passed on a later descendant commit. This suggests recovery; individual error fixes and the exact fixing commit are not proven.', candidate, 'likely_resolved'))
            else:
                results.append(result('The same workflow still fails on the latest comparable descendant commit. The failing error may have changed; individual error fixes are not verified.', candidate, 'still_failing'))
    return results


def parse_archive(content, token):
    records = []
    total = 0
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            files = [f for f in archive.infolist() if not f.is_dir() and f.filename.endswith('.txt')]
            if len(files) > 500 or sum(f.file_size for f in files) > MAX_EXPANDED:
                raise ValueError('Expanded logs exceed the 12 MB / 500 file import limit.')
            # GitHub includes a combined job file plus individual step files. Prefer steps.
            steps = [f for f in files if '/' in f.filename]
            for file in sorted(steps or files, key=lambda f: f.filename):
                with archive.open(file) as stream:
                    raw = stream.read(MAX_EXPANDED + 1)
                total += len(raw)
                if total > MAX_EXPANDED:
                    raise ValueError('Expanded logs exceed the 12 MB import limit.')
                for number, line in enumerate(raw.decode('utf-8', errors='replace').splitlines(), 1):
                    line = redact(ANSI.sub('', line), token).strip()
                    if not line:
                        continue
                    level = 3 if ERROR.search(line) else 4 if '##[warning]' in line.lower() else 6
                    records.append({'message': line[:8000], 'file': redact(file.filename, token)[:300], 'line': number, 'level': level})
                    if len(records) > 100000:
                        raise ValueError('This run exceeds the 100,000 line scan limit. Choose a smaller run.')
        if not records:
            raise ValueError('No readable text logs were found in this run.')
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError):
        raise ValueError('GitHub returned an unreadable log archive.') from None
    count = len(records)
    # Preserve error evidence first when a run is larger than the ingestion budget.
    if count > MAX_RECORDS:
        chosen = sorted(sorted(range(count), key=lambda i: (records[i]['level'], i))[:MAX_RECORDS])
        records = [records[i] for i in chosen]
    return records, count


def summarize(records):
    errors = [r for r in records if r['level'] == 3]
    patterns = [
        (r'ModuleNotFoundError|Cannot find module|No matching distribution|ERESOLVE', 'Dependency problem', 'Check the dependency manifest, lockfile, and installation step.'),
        (r'AssertionError|assert .*|\bFAIL(?:ED)?\s+\S*test|test.*failed', 'Test failure', 'Reproduce the named test at this commit and compare its expected and actual results.'),
        (r'SyntaxError|TS\d{4}|compilation failed|build failed', 'Build or syntax error', 'Run the same build command locally and inspect the reported file and line.'),
        (r'permission denied|unauthorized|forbidden|authentication failed', 'Access problem', 'Check workflow permissions and whether required secrets are available to this run.'),
        (r'timed? ?out|timeout|ECONNREFUSED|ENOTFOUND', 'Network or timeout problem', 'Check service availability, runner connectivity, and timeout settings.'),
    ]
    findings = []
    for pattern, title, next_step in patterns:
        matches = [r for r in errors if re.search(pattern, r['message'], re.I)]
        if matches:
            findings.append({'title': title, 'count': len(matches), 'next_step': next_step, 'evidence': matches[:3]})
    if not findings:
        findings = [{'title': 'Workflow failure needs investigation', 'count': len(errors),
                     'next_step': 'Review the failed step on GitHub. The available log text does not establish a root cause.', 'evidence': errors[:3]}]
    return {'mode': 'local', 'error_lines': len(errors), 'findings': findings,
            'note': 'Rule-based summary of imported evidence. Suggested causes need verification.'}


def register_github(app, db, uid, body, now):
    with app.app_context():
        db().executescript('''
            CREATE TABLE IF NOT EXISTS github_repositories (
                uid TEXT NOT NULL, repo TEXT NOT NULL, connected_at TEXT NOT NULL, PRIMARY KEY(uid,repo));
            CREATE TABLE IF NOT EXISTS github_imports (
                uid TEXT NOT NULL, repo TEXT NOT NULL, run_id INTEGER NOT NULL, attempt INTEGER NOT NULL,
                imported_at TEXT NOT NULL, result TEXT NOT NULL, PRIMARY KEY(uid,repo,run_id,attempt));
            CREATE TABLE IF NOT EXISTS github_resolutions (
                uid TEXT NOT NULL, repo TEXT NOT NULL, run_id INTEGER NOT NULL, attempt INTEGER NOT NULL,
                result TEXT NOT NULL, PRIMARY KEY(uid,repo,run_id,attempt));
        ''')
        if app.config.get('LOCAL_MODE'):
            for repo in app.config.get('DEFAULT_GITHUB_REPOSITORIES', []):
                db().execute('INSERT OR IGNORE INTO github_repositories VALUES(?,?,?)',
                             ('local-workspace', repository_name(repo), now()))
        db().commit()

    def credentials(data):
        token = data.get('token', '')
        if not isinstance(token, str) or len(token) > 500 or any(c.isspace() for c in token):
            raise ValueError('Enter a GitHub token without spaces.')
        return token

    def connected(owner, repo):
        if not db().execute('SELECT 1 FROM github_repositories WHERE uid=? AND repo=?', (owner, repo)).fetchone():
            raise NotFound('Connect this repository first.')

    def saved_resolution(owner, repo, run):
        row = db().execute('SELECT result FROM github_resolutions WHERE uid=? AND repo=? AND run_id=? AND attempt=?',
                           (owner, repo, run['id'], run['attempt'])).fetchone()
        return json.loads(row['result']) if row else unchecked_resolution()

    def save_resolution(owner, repo, run, resolution):
        db().execute('INSERT OR REPLACE INTO github_resolutions VALUES(?,?,?,?,?)',
                     (owner, repo, run['id'], run['attempt'], json.dumps(resolution)))

    @app.post('/api/github/resolution')
    def resolution():
        owner, data = uid(), body()
        repo, token = repository_name(data.get('repository')), credentials(data)
        connected(owner, repo)
        run_id, attempt = data.get('run_id'), data.get('attempt', 1)
        if type(run_id) is not int or not 0 < run_id < 2**63 or type(attempt) is not int or not 0 < attempt < 10000:
            raise ValueError('Choose a valid workflow run and attempt.')
        run = run_details(repo, github_request(f'/repos/{repo}/actions/runs/{run_id}/attempts/{attempt}', token))
        if run['id'] != run_id or run['attempt'] != attempt or run['conclusion'] not in FAILURES:
            raise ValueError('Choose the original failed workflow attempt.')
        status = check_resolutions(repo, [run], token, now())[0]
        save_resolution(owner, repo, run, status)
        db().commit()
        return jsonify(status)

    @app.get('/api/github/repositories')
    def repositories():
        return jsonify([dict(r) for r in db().execute('SELECT repo,connected_at FROM github_repositories WHERE uid=? ORDER BY connected_at DESC', (uid(),))])

    @app.post('/api/github/connect')
    def connect():
        owner, data = uid(), body()
        repo, token = repository_name(data.get('repository')), credentials(data)
        info = github_request(f'/repos/{repo}', token)
        repo = repository_name(info['full_name'])
        db().execute('INSERT OR IGNORE INTO github_repositories VALUES(?,?,?)', (owner, repo, now()))
        db().commit()
        return jsonify(repo=repo, url=f'https://github.com/{repo}')

    @app.post('/api/github/runs')
    def runs():
        owner, data = uid(), body()
        repo, token = repository_name(data.get('repository')), credentials(data)
        connected(owner, repo)
        # Failure filter avoids hiding failures behind many successful runs.
        response = github_request(f'/repos/{repo}/actions/runs?status=failure&per_page=20', token)
        runs = [run_details(repo, run) for run in response['workflow_runs']]
        statuses = check_resolutions(repo, runs, token, now()) if runs else []
        for run, status in zip(runs, statuses):
            run['resolution'] = status
            save_resolution(owner, repo, run, status)
        db().commit()
        return jsonify(runs=runs, note='Latest 20 failed runs. Tags compare later commits within the latest 100 repository runs; refresh to check again.')

    @app.get('/api/github/imports')
    def imports():
        owner = uid()
        items = [json.loads(r['result']) for r in db().execute('SELECT result FROM github_imports WHERE uid=? ORDER BY imported_at DESC LIMIT 30', (owner,))]
        for item in items:
            item['resolution'] = saved_resolution(owner, item['repository'], item['run'])
        return jsonify(items)

    @app.post('/api/github/import')
    def import_run():
        owner, data = uid(), body()
        repo, token = repository_name(data.get('repository')), credentials(data)
        connected(owner, repo)
        run_id, attempt = data.get('run_id'), data.get('attempt', 1)
        if type(run_id) is not int or not 0 < run_id < 2**63 or type(attempt) is not int or not 0 < attempt < 10000:
            raise ValueError('Choose a valid workflow run and attempt.')
        key = (owner, repo, run_id, attempt)
        existing = db().execute('SELECT result FROM github_imports WHERE uid=? AND repo=? AND run_id=? AND attempt=?', key).fetchone()
        if existing:
            return jsonify(**json.loads(existing['result']), already_imported=True)
        path = f'/repos/{repo}/actions/runs/{run_id}/attempts/{attempt}'
        run = run_details(repo, github_request(path, token))
        if run['conclusion'] not in FAILURES:
            raise ValueError('Only completed failed workflow runs can be imported.')
        if run['id'] != run_id or run['attempt'] != attempt:
            raise BadGateway('GitHub returned a different run attempt. Refresh and try again.')
        records, available = parse_archive(github_request(path + '/logs', token, archive=True), token)
        source, timestamp = f'github:{repo}', now()
        # A single transaction makes retries/concurrent imports safe.
        db().execute('BEGIN IMMEDIATE')
        existing = db().execute('SELECT result FROM github_imports WHERE uid=? AND repo=? AND run_id=? AND attempt=?', key).fetchone()
        if existing:
            db().rollback()
            return jsonify(**json.loads(existing['result']), already_imported=True)
        for record in records:
            row = db().execute('INSERT INTO logs(uid,timestamp,source,severity_level,severity_label,message,entity_id,trace_id) VALUES(?,?,?,?,?,?,?,?)',
                               (owner, timestamp, source, record['level'], {3: 'ERROR', 4: 'WARNING', 6: 'INFO'}[record['level']],
                                f"[GitHub run {run_id} attempt {attempt} | {record['file']}:{record['line']}] {record['message']}",
                                run['sha'], f'github:{run_id}:{attempt}'))
            record['record_id'] = row.lastrowid
        result = {'repository': repo, 'run': run, 'source': source, 'imported_at': timestamp,
                  'records': len(records), 'available_lines': available, 'truncated': available > len(records),
                  'summary': summarize(records)}
        db().execute('INSERT INTO github_imports VALUES(?,?,?,?,?,?)', (*key, timestamp, json.dumps(result)))
        db().commit()
        return jsonify(result), 201
