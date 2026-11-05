#!/usr/bin/env python3
"""独立持久卷 PG/basebackup/WAL 与 S3 对象保护恢复演练；只清理本轮创建的服务/卷。

默认工程数据规模不能替代代表性规模 RPO≤24h、RTO≤8h。证据保留实际对象数/字节、LSN、
源码、组件版本、日志、持久卷类型和恢复时间，绝不使用组件专项的 tmpfs 下结论。
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import socket
import subprocess
import time
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]


def port():
    with socket.socket() as server:
        server.bind(('127.0.0.1', 0))
        return server.getsockname()[1]


def stop(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def command(args, *, timeout=60):
    return subprocess.check_output(args, cwd=ROOT, stderr=subprocess.STDOUT, text=True, timeout=timeout).strip()


def wait(action, *, timeout=45):
    until = time.monotonic() + timeout
    while True:
        try:
            return action()
        except Exception:
            if time.monotonic() >= until:
                raise
            time.sleep(.1)


def protect_objects(context, target):
    from mindex.maintenance.backup_files import safe_path
    from mindex.shared import jsonio
    from mindex.shared.resource_registry import required_objects

    manifest = {}
    for key in sorted(set(required_objects(context.store))):
        obj = context.media.stat(key)
        path = safe_path(target / 'objects', key)
        path.parent.mkdir(parents=True, exist_ok=True)
        hashed, size = hashlib.sha256(), 0
        # 远端 pin 与维护门闩保护这份权威对象，摘要来自字节而不是 ETag。
        with context.media.open_read(key) as source, path.open('wb') as sink:
            while chunk := source.read(1024 * 1024):
                hashed.update(chunk)
                size += len(chunk)
                sink.write(chunk)
            sink.flush()
            os.fsync(sink.fileno())
        if size != obj.size:
            raise ValueError('保护期间对象大小变化')
        manifest[key] = {'size': size, 'sha256': hashed.hexdigest(), 'source_version_id': obj.version_id}
    jsonio.save(target / 'objects-manifest.json', manifest)
    return manifest


def restore_objects(context, source):
    from mindex.maintenance.backup_files import safe_path

    manifest = json.loads((source / 'objects-manifest.json').read_text())
    if list(context.media.iter_prefix('')):
        raise ValueError('对象恢复目标非空，拒绝覆盖')
    for key, entry in manifest.items():
        path = safe_path(source / 'objects', key)
        with path.open('rb') as body:
            digest = hashlib.file_digest(body, 'sha256').hexdigest()
        if digest != entry['sha256'] or path.stat().st_size != entry['size']:
            raise ValueError('对象保护副本损坏')
        context.media.put_immutable(key, path, sha256=digest)
        with context.media.open_read(key) as body:
            checked = hashlib.sha256()
            while chunk := body.read(1024 * 1024):
                checked.update(chunk)
        if checked.hexdigest() != digest:
            raise ValueError('对象恢复摘要不一致')
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--minio-binary', default=shutil.which('minio'))
    parser.add_argument('--objects', type=int, default=8)
    parser.add_argument('--object-bytes', type=int, default=1024 * 1024)
    args = parser.parse_args()
    if not args.minio_binary or not shutil.which('docker') or args.objects < 2 or args.object_bytes < 1:
        parser.error('需要 Docker/MinIO、至少2个对象和正对象大小')
    spec = importlib.util.spec_from_file_location('scaling_verifier', ROOT / 'scripts/verify-scaling.py')
    verifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verifier)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    backup = output / 'protection'
    archive = backup / 'wal-archive'
    archive.mkdir(parents=True)
    # 仅本轮随机隔离目录允许容器 postgres 写入，绝不改业务卷权限。
    backup.chmod(0o777)
    archive.chmod(0o777)
    report = {'started_at': datetime.now(UTC).isoformat(), 'status': 'running',
              'code_version': command(['git','rev-parse','HEAD']), 'source_sha256': verifier.source_identity(),
              'postgres_image': verifier.PG_IMAGE, 'minio_version': command([args.minio_binary,'--version']),
              'postgres_storage': '独立 Docker 持久命名卷，禁止 tmpfs',
              'object_storage': '独立 live/protection/restored 持久目录；恢复不读取 live 目录',
              'rpo_gate_passed': False, 'rto_gate_passed': False,
              'scope': '单机工程数据持久卷灾备；代表性规模 RPO/RTO 仍独立待验收'}
    report_path = output / 'report.json'
    def save():
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    save()
    run_id = uuid4().hex[:12]
    containers, volumes = [], []
    result_code = 1
    try:
        import boto3
        import httpx
        import psycopg

        from mindex.maintenance.migration import database_manifest, offline
        from mindex.shared.backends import open_context
        from mindex.shared.config import Config
        from mindex.shared.db.migrate_pg import migrate_pg

        password, access = uuid4().hex + uuid4().hex, 'mindex-recovery'
        bucket = 'mindex-recovery-' + run_id
        for label in ('source', 'restored'):
            volume = f'mindex-recovery-{run_id}-{label}'
            command(['docker','volume','create',volume])
            volumes.append(volume)
        report['persistent_volumes'] = volumes
        def start_pg(label, volume, extra=()):
            name = f'mindex-recovery-{run_id}-{label}'
            containers.append(name)
            command(['docker','run','-d','--name',name,'-p','127.0.0.1::5432',
                '-e','POSTGRES_USER=mindex','-e',f'POSTGRES_PASSWORD={password}','-e','POSTGRES_DB=mindex',
                '--mount',f'source={volume},target=/var/lib/postgresql/data',
                '--mount',f'type=bind,source={backup},target=/protection',
                '--mount',f'type=bind,source={archive},target=/wal-archive',verifier.PG_IMAGE,*extra])
            bound = command(['docker','port',name,'5432']).rsplit(':',1)[1]
            dsn = f'postgresql://mindex:{password}@127.0.0.1:{bound}/mindex'
            def ready():
                with psycopg.connect(dsn, connect_timeout=1) as db:
                    db.execute('SELECT 1')
            wait(ready)
            return name, dsn
        source_name, source_dsn = start_pg('source', volumes[0], ['postgres','-c','wal_level=replica',
            '-c','archive_mode=on','-c','archive_timeout=1','-c',"archive_command=test ! -f /wal-archive/%f && cp %p /wal-archive/%f"])
        report['postgres_version'] = command(['docker','exec',source_name,'postgres','--version'])
        with psycopg.connect(source_dsn) as db:
            migrate_pg(db)
        os.environ['MINDEX_RECOVERY_DSN'] = source_dsn
        os.environ['MINDEX_RECOVERY_ACCESS'] = access
        os.environ['MINDEX_RECOVERY_SECRET'] = password
        with ExitStack() as resources:
            def start_minio(label):
                endpoint = f'http://127.0.0.1:{port()}'
                directory = output / f'objects-{label}'
                directory.mkdir()
                log = resources.enter_context((output / f'minio-{label}.log').open('w'))
                env = {key:value for key,value in os.environ.items() if not key.startswith(('MINDEX_','MINIO_'))}
                process = subprocess.Popen([args.minio_binary,'server',str(directory),'--address',endpoint.removeprefix('http://'),
                    '--console-address',f'127.0.0.1:{port()}','--quiet'], env={**env,'MINIO_ROOT_USER':access,'MINIO_ROOT_PASSWORD':password},
                    stdout=log, stderr=subprocess.STDOUT)
                resources.callback(stop, process)
                def ready():
                    response = httpx.get(endpoint + '/minio/health/live', timeout=1, trust_env=False)
                    response.raise_for_status()
                wait(ready)
                client = boto3.client('s3', endpoint_url=endpoint, aws_access_key_id=access,
                                      aws_secret_access_key=password, region_name='us-east-1')
                client.create_bucket(Bucket=bucket)
                client.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={'Status':'Enabled'})
                resources.callback(client.close)
                return endpoint, process, client
            endpoint, live_minio, client = start_minio('live')
            config = Config(data_dir=output / 'source-node', database={'backend':'postgres','postgres':{'dsn_env':'MINDEX_RECOVERY_DSN'}},
                queue={'backend':'postgres'}, locks={'backend':'postgres'}, storage={'backend':'s3','s3':{'endpoint_url':endpoint,
                'bucket':bucket,'prefix':'dr','region':'us-east-1','access_key_id_env':'MINDEX_RECOVERY_ACCESS',
                'secret_access_key_env':'MINDEX_RECOVERY_SECRET','min_free_gb':0,'staging_quota_gb':.25}})
            with open_context(config) as source:
                def write_objects(start, end):
                    for number in range(start,end):
                        payload = bytes([number % 251]) * args.object_bytes
                        digest = hashlib.sha256(payload).hexdigest()
                        key = f'sources/dr-{number}.bin'
                        source.media.put_immutable(key, __import__('io').BytesIO(payload), sha256=digest)
                        source.store.execute('INSERT INTO sources(id,name,path,media,layout_key) VALUES(?,?,?,?,?)',
                                             (f'source-{number}',f'恢复样本 {number}',key,'{}','recovery'))
                split = args.objects // 2
                write_objects(0,split)
                with offline(source,'disaster-protect'):
                    report['basebackup_started_at'] = datetime.now(UTC).isoformat()
                    command(['docker','exec','--user','postgres',source_name,'pg_basebackup','-U','mindex',
                             '-D','/protection/base','-X','stream','-c','fast'], timeout=60)
                # 必须通过 WAL 恢复 basebackup 之后的已提交写入，不能只启动一份旧快照。
                write_objects(split,args.objects)
                source.store.execute("INSERT INTO settings(key,value) VALUES('recovery.after_basebackup','must-survive')")
                with offline(source,'disaster-protect'):
                    protected = protect_objects(source, backup)
                    expected = database_manifest(source.store)
                    with psycopg.connect(source_dsn, autocommit=True) as db:
                        target_lsn, segment = db.execute('SELECT pg_current_wal_lsn()::text,pg_walfile_name(pg_current_wal_lsn())').fetchone()
                        db.execute('SELECT pg_switch_wal()')
                    if not re.fullmatch(r'[0-9A-F]+/[0-9A-F]+', target_lsn):
                        raise ValueError('无效恢复 LSN')
                    wait(lambda: command(['docker','exec',source_name,'test','-f','/wal-archive/' + segment]))
                    report.update(protected_objects=len(protected), protected_bytes=sum(o['size'] for o in protected.values()),
                                  target_lsn=target_lsn, wal_segment=segment, library_id=source.library_id,
                                  protected_at=datetime.now(UTC).isoformat())
                    save()
                # 损坏 live 最新版本，再切断整个 live MinIO；恢复必须只使用保护卷。
                first = next(iter(protected))
                client.delete_object(Bucket=bucket, Key=source.media._object_key(first,None))
            failure_time = time.monotonic()
            report['failure_at'] = datetime.now(UTC).isoformat()
            command(['docker','kill','--signal','KILL',source_name])
            live_minio.kill()
            live_minio.wait()
            init = ("cp -a /protection/base/. /restore/\n"
                    "touch /restore/recovery.signal\n"
                    "cat >> /restore/postgresql.auto.conf <<'RECOVERY'\n"
                    "restore_command = 'cp /wal-archive/%f %p'\n"
                    f"recovery_target_lsn = '{target_lsn}'\n"
                    "recovery_target_action = 'promote'\nRECOVERY\n"
                    "chown -R postgres:postgres /restore\n")
            command(['docker','run','--rm','--entrypoint','sh','--mount',f'source={volumes[1]},target=/restore',
                     '--mount',f'type=bind,source={backup},target=/protection,readonly',verifier.PG_IMAGE,'-ceu',init])
            _target_name, target_dsn = start_pg('restored',volumes[1])
            restored_endpoint, _, _ = start_minio('restored')
            os.environ['MINDEX_RECOVERY_DSN'] = target_dsn
            restored_config = config.model_copy(deep=True, update={'data_dir':output / 'restored-node'})
            restored_config.storage.s3.endpoint_url = restored_endpoint
            with open_context(restored_config) as restored:
                restore_objects(restored,backup)
                if database_manifest(restored.store) != expected:
                    raise ValueError('WAL 恢复后的业务表逐项摘要不一致')
                if restored.store.one("SELECT value FROM settings WHERE key='recovery.after_basebackup'")['value'] != 'must-survive':
                    raise ValueError('basebackup 之后的已提交写入未通过 WAL 恢复')
                with psycopg.connect(target_dsn) as db:
                    if db.execute('SELECT pg_is_in_recovery()').fetchone()[0]:
                        raise ValueError('恢复实例未按目标 LSN 提升')
            report.update(recovery_seconds=time.monotonic()-failure_time, observed_committed_write_loss=0,
                          basebackup_followup_recovered=True, source_unchanged=verifier.source_identity()==report['source_sha256'],
                          status='small_dataset_recovered_not_rpo_rto_acceptance')
            result_code = 0 if report['source_unchanged'] else 1
    except BaseException as exc:
        report.update(status='interrupted' if isinstance(exc,KeyboardInterrupt) else 'failed',error=type(exc).__name__)
        raise
    finally:
        for name in containers:
            try:
                (output / f'{name}.log').write_text(command(['docker','logs',name]))
            except (OSError, subprocess.SubprocessError):
                pass
        for name in reversed(containers):
            subprocess.run(['docker','rm','-f',name],capture_output=True,check=False)
        for volume in reversed(volumes):
            subprocess.run(['docker','volume','rm',volume],capture_output=True,check=False)
        report['finished_at'] = datetime.now(UTC).isoformat()
        save()
    print(json.dumps(report,ensure_ascii=False,indent=2))
    return result_code


if __name__ == '__main__':
    raise SystemExit(main())
