"""Audit and resume source embedding publications without restarting live tasks."""
import argparse
import json
import logging
import re
import time
from pathlib import Path

from fastapi import HTTPException

from mindex.admin.curation import Library
from mindex.admin.source_tasks import PublishOptions, SourceTasks
from mindex.settings.runtime_settings import RuntimeConfig
from mindex.shared.config import load_config
from mindex.shared.storage import Store


def retryable(error):
    value = str(error).lower()
    permanent = ('模型内容安全', '不安全或敏感', 'content_policy', 'content_filter',
                 'invalid api key', 'unauthorized', 'forbidden', 'invalid argument',
                 'not supported', '不支持', '配置不符', '维度不匹配', '维度不符合',
                 '文件缺失', '媒体缺失', '找不到素材')
    if any(word in value for word in permanent):
        return False
    # Unknown failures remain retryable until there is concrete evidence that
    # repeating the same request cannot succeed.
    return re.search(r"(?:http(?: error| status)?|status(?: code)?)[ :＝=]*(400|401|403|404|422)\b", value) is None



def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--state', required=True)
    args = parser.parse_args()
    config = load_config(args.config)
    store = Store(config.data_dir)
    manager = SourceTasks(RuntimeConfig(config, store), store)
    path = Path(args.state)
    path.parent.mkdir(parents=True, exist_ok=True)
    report = {'status': 'auditing', 'jobs': [], 'prerequisites': []}

    def save():
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        temporary.replace(path)

    save()
    for source in store.sources():
        work = Library(store).source(source['id'])['work']
        for scope, field, names in [('shot_moment', 'search', ('shots', 'dialogue', 'analysis')),
                                    ('scene', 'scene_search', ('shots', 'dialogue', 'analysis'))]:
            for name in names:
                if work[name].get(field, {}).get('expected_segments', 0) and not work[name][field]['current']:
                    report['jobs'].append({'source_id': source['id'], 'name': source['name'], 'work': name,
                                           'scope': scope, 'status': 'waiting', 'attempts': 0})
        if work['persons']['status'] != 'complete' and work['persons'].get('setup', {}).get('missing'):
            report['prerequisites'].append({'name': source['name'], 'work': 'persons',
                                            'reasons': work['persons']['setup']['missing']})
        save()
    report['status'] = 'repairing'
    save()
    while any(job['status'] not in ('complete', 'blocked') for job in report['jobs']):
        for job in report['jobs']:
            if job['status'] in ('complete', 'blocked'):
                continue
            if job.get('task_id'):
                task = manager.get(job['task_id'])
                job['progress'] = task.get('progress')
                if task['status'] in ('queued', 'running'):
                    job['status'] = task['status']
                    continue
                error = task.get('error') or (task.get('publication') or {}).get('error')
                if task['status'] == 'complete' and not error:
                    job['status'] = 'complete'
                    job.pop('error', None)
                    continue
                if not retryable(error):
                    job.update(status='blocked', error=error)
                    continue
                job.pop('task_id')
                job['status'] = 'retrying'
                job['next_attempt_at'] = time.time() + min(300, 2 ** min(job['attempts'], 8))
            if time.time() < job.get('next_attempt_at', 0):
                continue
            if store.one("SELECT id FROM source_tasks WHERE source_id=? AND work='publish' "
                         "AND json_extract(options,'$.work')=? "
                         "AND json_extract(options,'$.scope')=? "
                         "AND status IN ('queued','running')", (job['source_id'], job['work'], job['scope'])):
                continue
            # A user's existing task owns the same lock: wait for it instead of
            # interrupting it or resetting its progress. Recheck after it finishes.
            try:
                current = Library(store).source(job['source_id'])['work']
                field = 'search' if job['scope'] == 'shot_moment' else 'scene_search'
                coverage = current[job['work']][field]
                if not coverage['expected_segments'] or coverage['current']:
                    job['status'] = 'complete'
                    continue
                task = manager.start(job['source_id'], 'publish', PublishOptions(work=job['work'], scope=job['scope']))
                job.update(task_id=task['id'], status=task['status'], attempts=job['attempts'] + 1)
            except HTTPException as exc:
                if exc.status_code != 409:
                    job.update(status='blocked', error=str(exc.detail))
            save()
        save()
        if any(job['status'] not in ('complete', 'blocked') for job in report['jobs']):
            time.sleep(10)
    report['status'] = 'complete' if all(j['status'] == 'complete' for j in report['jobs']) else 'needs_attention'
    save()


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    main()
