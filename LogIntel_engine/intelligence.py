"""Measured incident investigation over a bounded received-log window."""
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
import hashlib
import statistics
import time
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.decomposition import TruncatedSVD
from sklearn.metrics.pairwise import cosine_similarity
try:
    from LogIntel_engine.analysis import RULES, normalize_message
except ImportError:
    from analysis import RULES, normalize_message


def category(message):
    text = message.lower()
    rule = next((r for r in RULES if any(term in text for term in r[1])), None)
    return (rule[0], rule[2], rule[3]) if rule else ('routine', 'Routine / unclassified', 'Compare this event with request traces and service metrics.')


def signature(record):
    return hashlib.sha256(('generated' if record['message'].startswith('[GENERATED]') else 'actual').encode() + ('|' + record['source'] + '|' + normalize_message(record['message'])).encode()).hexdigest()[:24]


def received_minute(record):
    return datetime.fromisoformat(record['timestamp'].replace('Z', '+00:00')).replace(second=0, microsecond=0)


def detect_spikes(records, window_minutes=5, baseline_minutes=30, minimum=5, min_baseline=5):
    """Flag a warning/error burst far above the same source's own per-minute baseline.

    The baseline counts quiet minutes as zero, so it only uses minutes after the oldest
    received record; with fewer than min_baseline observed minutes no spike is claimed.
    """
    if not records:
        return []
    window_start = datetime.now(timezone.utc).replace(second=0, microsecond=0) - timedelta(minutes=window_minutes - 1)
    observed = max(min(received_minute(r) for r in records) + timedelta(minutes=1),
                   window_start - timedelta(minutes=baseline_minutes))
    span = int((window_start - observed).total_seconds() // 60)
    if span < min_baseline:
        return []
    baseline_span = [window_start - timedelta(minutes=i) for i in range(1, span + 1)]
    counts, members = defaultdict(Counter), defaultdict(list)
    for r in records:
        at = received_minute(r)
        if r['severity_level'] <= 4 and at >= observed:
            key = (r['source'], r['message'].startswith('[GENERATED]'))
            counts[key][at] += 1
            members[key, at].append(r['_id'])
    spikes = []
    for (source, generated), per_minute in counts.items():
        baseline = [per_minute[m] for m in baseline_span]
        median = statistics.median(baseline)
        spread = statistics.median(abs(c - median) for c in baseline) * 1.4826
        threshold = max(minimum, median + 3 * max(spread, 1))
        peak_at, peak = max(((m, c) for m, c in per_minute.items() if m >= window_start),
                            key=lambda item: item[1], default=(None, 0))
        if peak >= threshold:
            spikes.append(dict(source=source, generated=generated, minute=peak_at.isoformat(), count=peak,
                               baseline_median=median, threshold=round(threshold, 1), baseline_minutes=span,
                               records=members[(source, generated), peak_at]))
    return sorted(spikes, key=lambda s: s['count'], reverse=True)


def investigate(records, suppressed=(), rules=(), window_minutes=5):
    started = time.perf_counter()
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
    spikes = detect_spikes(records, window_minutes)
    spiking = {i: s for s in spikes for i in s['records']}
    counts = Counter()
    for r in records:
        if datetime.fromisoformat(r['timestamp'].replace('Z', '+00:00')) >= cutoff:
            counts[(category(r['message'])[0], r.get('entity_id') or r['source'], r['message'].startswith('[GENERATED]'))] += 1
    enriched = []
    for r in records:
        kind, title, advice = category(r['message'])
        entity = r.get('entity_id') or r['source']
        generated = r['message'].startswith('[GENERATED]')
        count = counts[(kind, entity, generated)] if datetime.fromisoformat(r['timestamp'].replace('Z', '+00:00')) >= cutoff else 0
        base = {0:80, 1:75, 2:70, 3:50, 4:25, 5:10, 6:5, 7:0}[r['severity_level']]
        factors = [dict(name='Severity', points=base)]
        if r['severity_level'] <= 4 and count >= 3:
            factors.append(dict(name=f'Recurrence ({count} in {window_minutes} min)', points=20))
        spike = spiking.get(r['_id'])
        if spike:
            factors.append(dict(name=f"Volume spike ({spike['count']}/min vs baseline {spike['baseline_median']:g})", points=15))
        if r['ml_anomaly']:
            factors.append(dict(name='Stored clustering outlier', points=10))
        muted = signature(r) in suppressed
        if muted:
            factors.append(dict(name='Analyst suppression', points=-45))
        score = max(0, min(100, sum(f['points'] for f in factors)))
        enriched.append(dict(**r, category=kind, category_label=title, entity=entity,
            generated=generated, signature=signature(r), recurrence=count,
            escalated=r['severity_level'] <= 4 and count >= 3, spike=bool(spike),
            risk_score=score, factors=factors, suppressed=muted, next_step=advice))
    groups = {}
    for r in enriched:
        if r['severity_level'] > 4:
            continue
        key = (r['category'], r['entity'], r['generated'])
        group = groups.setdefault(key, dict(id=hashlib.sha256(str(key).encode()).hexdigest()[:16],
            category=r['category'], title=r['category_label'], entity=r['entity'], generated=r['generated'],
            count=0, risk_score=0, evidence=[], sources=set(), spike=False, next_step=r['next_step']))
        group['count'] += 1
        group['spike'] = group['spike'] or r['spike']
        group['risk_score'] = max(group['risk_score'], r['risk_score'])
        group['sources'].add(r['source'])
        group['evidence'].append(r['_id'])
    incidents = sorted(groups.values(), key=lambda g:g['risk_score'], reverse=True)
    alerts = []
    for incident in incidents:
        incident['sources'] = sorted(incident['sources'])
        for rule in rules:
            matched = ((rule['condition'] == 'risk_gt_80' and incident['risk_score'] > 80) or
                       (rule['condition'] == 'database_error' and incident['category'] == 'database') or
                       (rule['condition'] == 'auth_repeated' and incident['category'] == 'auth' and
                        any(r['escalated'] and r['_id'] in incident['evidence'] for r in enriched)))
            if matched:
                alerts.append(dict(id=f"{rule['id']}:{incident['id']}", rule_id=rule['id'],
                    action=rule['action'], incident_id=incident['id'], evidence=incident['evidence'],
                    status='In-app notification' if rule['action'] == 'in_app' else 'Payload preview; not dispatched'))
    insights = []
    for kind in ('database', 'upstream', 'auth'):
        for generated in (False, True):
            rows = [r for r in enriched if r['category'] == kind and r['generated'] == generated and
                    r['severity_level'] <= 4 and datetime.fromisoformat(r['timestamp'].replace('Z', '+00:00')) >= cutoff]
            sources = sorted({r['source'] for r in rows})
            if len(rows) >= 3:
                insights.append(dict(title=('Repeated access rejection' if kind == 'auth' else 'Correlated '+kind+' errors'),
                    count=len(rows), sources=sources, generated=generated,
                    evidence=[r['_id'] for r in rows[:6]],
                    description='Shared timing and category are correlation evidence; dependency causation is unconfirmed.'))
    for s in spikes:
        insights.append(dict(title='Error volume spike', count=s['count'], sources=[s['source']], generated=s['generated'],
            evidence=s['records'][:6],
            description=f"Peak of {s['count']} warnings/errors in one minute against a median of {s['baseline_median']:g} "
                        f"over the previous {s['baseline_minutes']} minutes. A spike is a statistical signal, not a confirmed outage."))
    baseline = sum(r['severity_level'] <= 3 for r in enriched)
    return dict(records=enriched, incidents=incidents, alerts=alerts, insights=insights,
        spikes=[{k: v for k, v in s.items() if k != 'records'} | dict(evidence=s['records'][:6]) for s in spikes],
        metrics=dict(records=len(records), raw_alerts=baseline, grouped_incidents=len(incidents),
                     reduction_percent=round(max(0, 1-len(incidents)/baseline)*100, 1) if baseline else 0,
                     suppressed=sum(r['suppressed'] for r in enriched), spikes=len(spikes),
                     analysis_ms=round((time.perf_counter()-started)*1000,2)),
        window_minutes=window_minutes, method='Transparent severity + recurrence + volume spike + outlier heuristic; not XGBoost')


def vector_space(records, selected_id=None):
    started = time.perf_counter()
    if not records:
        return dict(points=[], neighbors=[], tokens=[], method='TF-IDF / truncated SVD', dimensions=0)
    try:
        vectorizer = TfidfVectorizer(stop_words='english', max_features=1500)
        matrix = vectorizer.fit_transform([normalize_message(r['message']) for r in records])
    except ValueError:
        return dict(points=[], neighbors=[], tokens=[], method='No usable vocabulary', dimensions=0)
    if matrix.shape[1] >= 2 and matrix.shape[0] >= 2 and any((matrix[i] != matrix[0]).nnz for i in range(1, matrix.shape[0])):
        coords = TruncatedSVD(n_components=2, random_state=42).fit_transform(matrix)
    else:
        coords = [[float(matrix[i].sum()), 0] for i in range(len(records))]
    points = [dict(id=r['_id'], x=round(float(c[0]),5), y=round(float(c[1]),5),
                   category=category(r['message'])[0], source=r['source'], message=r['message'],
                   generated=r['message'].startswith('[GENERATED]')) for r,c in zip(records,coords)]
    index = next((i for i,r in enumerate(records) if r['_id'] == selected_id), 0)
    similarities = cosine_similarity(matrix[index], matrix).ravel()
    order = sorted((i for i in range(len(records)) if i != index), key=lambda i:float(similarities[i]), reverse=True)[:5]
    features = vectorizer.get_feature_names_out()
    row = matrix[index].toarray().ravel()
    tokens = sorted((dict(term=str(features[i]), weight=round(float(value),4)) for i,value in enumerate(row) if value), key=lambda t:t['weight'], reverse=True)[:12]
    return dict(points=points, neighbors=[dict(id=records[i]['_id'], similarity=round(float(similarities[i]),4), message=records[i]['message']) for i in order],
                tokens=tokens, selected_id=records[index]['_id'], dimensions=matrix.shape[1],
                method='TF-IDF cosine similarity; 2D truncated SVD projection (not distance-preserving)',
                analysis_ms=round((time.perf_counter()-started)*1000,2))
