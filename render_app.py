"""One Render service: React dashboard, API, and a mounted Python log source.

Only fresh hosted data is used. One Gunicorn worker owns the sender/generator.
"""
import atexit
from pathlib import Path
from flask import send_from_directory, abort, request, jsonify
from werkzeug.middleware.dispatcher import DispatcherMiddleware
from LogIntel_engine.api import create_app as create_api
from live_app.app import create_app as create_source
from live_app.log_shipper import NeuroLogHandler

ROOT = Path(__file__).resolve().parent


def create_hosted_app(database=None, tasks_database=None, start_sender=True, frontend_dir=None):
    config = {'LOCAL_MODE': True, 'DEFAULT_GITHUB_REPOSITORIES': ['shahrukh-210906/WONDERLUST']}
    if database:
        config['DATABASE'] = str(database)
    api = create_api(config)

    def deliver(payload):
        with api.test_client() as client:
            response = client.post('/api/ingest', json=payload, headers={'x-api-key': key})
            if response.status_code != 201:
                raise RuntimeError(f'Ingestion failed with status {response.status_code}')

    data_dir = Path(database).parent if database else ROOT / 'data'
    handler = NeuroLogHandler('http://localhost/api/ingest', 'task-service',
                             data_dir / 'hosted-outbox.db', data_dir / 'unused.key', transport=deliver)
    source = create_source({'DATABASE': str(tasks_database or data_dir / 'tasks.db')}, handler)
    source.config['DASHBOARD_URL'] = '/dashboard'
    def shutdown():
        source.extensions['generator'].stop()
        handler.close()
    atexit.register(shutdown)

    @api.route('/api/scenario-generator', methods=['GET', 'POST', 'DELETE'])
    def scenarios():
        with source.test_client() as client:
            response = client.open('/generator', method=request.method,
                                   json=request.get_json(silent=True) if request.method == 'POST' else None)
            return jsonify(response.json), response.status_code

    @api.get('/')
    @api.get('/<path:path>')
    def frontend(path=''):
        if path.startswith('api/'):
            abort(404)
        dist = Path(frontend_dir) if frontend_dir else ROOT / 'logintel_ui' / 'dist'
        target = (dist / path).resolve()
        if target.is_relative_to(dist.resolve()) and target.is_file():
            return send_from_directory(dist, path)
        if Path(path).suffix:
            abort(404)
        return send_from_directory(dist, 'index.html')

    with api.test_client() as client:
        key = client.post('/api/generate-key', json={'app_name': 'hosted-python-source'}).json['key']
    if start_sender:
        handler.start()
    application = DispatcherMiddleware(api, {'/source': source})
    return application, api, source, handler


