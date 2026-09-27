#!/usr/bin/env python3
"""Real signed HTTPS pull and isolated deployment fixture; never accesses block devices."""
import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import subprocess
import threading

from quirkbench.contracts import ContractError, canonical
from quirkbench.deployment import DeploymentManifest
from quirkbench.ostree import OstreeBackend, Remote
from quirkbench.repository_http import make_repository_server
from quirkbench.store import atomic_write


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work', required=True, type=Path)
    parser.add_argument('--manifest', required=True, type=Path)
    parser.add_argument('--repository', required=True, type=Path)
    parser.add_argument('--public-key', required=True, type=Path)
    parser.add_argument('--repository-mode', choices=('bare','bare-user'), default='bare',
                        help='Offline fixture only: bare-user preserves logical xattrs on SELinux hosts.')
    args = parser.parse_args()
    work = args.work.resolve()
    work.mkdir(parents=True, exist_ok=False)
    manifest = DeploymentManifest.from_dict(json.loads(args.manifest.read_bytes()))
    cert, key = work / 'tls-cert.pem', work / 'tls-key.pem'
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '2',
                    '-keyout', str(key), '-out', str(cert), '-subj', '/CN=localhost',
                    '-addext', 'subjectAltName=DNS:localhost,IP:127.0.0.1'],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    key.chmod(0o600)
    server = make_repository_server(('127.0.0.1', 0), {manifest.repository: args.repository.resolve()}, cert, key, cert)
    # Inject a bounded network outage after some genuine objects have arrived.
    # This subclass exists only in the qualification fixture, never the service.
    transfer = {'requests': 0, 'outage': True, 'denied': 0}
    transfer_lock = threading.Lock()
    original_handler = server.RequestHandlerClass
    class InterruptedHandler(original_handler):
        def do_GET(self):
            with transfer_lock:
                transfer['requests'] += int('/objects/' in self.path)
                deny = transfer['outage'] and transfer['requests'] > 8
                transfer['denied'] += int(deny)
            if deny:
                self.close_connection = True
                self.send_error(503, 'Qualification network interruption')
            else:
                super().do_GET()
    server.RequestHandlerClass = InterruptedHandler
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    sysroot = work / 'data'
    sysroot.mkdir()
    (sysroot / '.regular-file-fixture').write_text('No physical device is commissioned here.\n')
    if args.repository_mode == 'bare-user':
        subprocess.run(['ostree','admin','init-fs',str(sysroot)],check=True)
        subprocess.run(['ostree','--repo='+str(sysroot/'ostree/repo'),'config','set','core.mode','bare-user'],check=True)
    remote = Remote(f'https://127.0.0.1:{server.server_address[1]}/{manifest.repository}', cert,
                    args.public_key.resolve(), cert, key)
    def guard(path):
        if path.resolve() != sysroot or path.is_symlink() or not (path / '.regular-file-fixture').is_file():
            raise ValueError('fixture path identity changed')
    backend = OstreeBackend(sysroot, {manifest.repository: remote}, verify_storage=guard,
                            progress=lambda phase,message: print(f'{phase}: {message}', flush=True), reserve_bytes=0)
    real_runner = backend.runner
    deploy_calls = []
    lose_deploy_ack = True
    def fault_runner(argv):
        nonlocal lose_deploy_ack
        if 'pull' in argv:
            # Do not spend time automatically retrying the deliberate outage.
            argv = [*argv[:argv.index('pull') + 1], '--network-retries=0', *argv[argv.index('pull') + 1:]]
        result = real_runner(argv)
        if 'deploy' in argv:
            deploy_calls.append(tuple(argv))
            if lose_deploy_ack:
                lose_deploy_ack = False
                raise ConnectionError('Qualification lost response after durable deploy')
        return result
    backend.runner = fault_runner
    try:
        try:
            backend.prepare(manifest, 'qualification-one')
        except ContractError:
            assert transfer['denied'] > 0
            assert not deploy_calls
            assert not list((sysroot / 'quirkbench/attempts').glob('*/prepared.json'))
        else:
            raise AssertionError('interrupted download was incorrectly ready')
        # OSTree may discard transaction objects on abort while retaining a
        # partial-commit marker; both are valid retry states.
        assert (any((sysroot / 'ostree/repo/objects').glob('*/*'))
                or any((sysroot / 'ostree/repo/state').glob('*.commitpartial')))
        transfer['outage'] = False
        try:
            backend.prepare(manifest, 'qualification-one')
        except ConnectionError:
            assert len(deploy_calls) == 1
        else:
            raise AssertionError('deployment interruption was not injected')
        first = backend.prepare(manifest, 'qualification-one')
        assert len(deploy_calls) == 1, 'retry duplicated an already deployed attempt'
        first_state = sysroot / 'ostree/deploy' / backend._stateroot(first.attempt_id)
        (first_state / 'var/quirkbench-contamination').write_text('previous run')
        (first_state / 'deploy' / (manifest.revision + '.0') / 'etc/quirkbench-contamination').write_text('previous setting')
        second = backend.prepare(manifest, 'qualification-two')
        second_state = sysroot / 'ostree/deploy' / backend._stateroot(second.attempt_id)
        assert not list(second_state.rglob('quirkbench-contamination'))
        again = backend.prepare(manifest, 'qualification-two')
        assert again.deployment_id == second.deployment_id
        assert backend.inspect('qualification-one').revision == first.revision
        # The image builder consumes only this public prepared tree, never TLS keys.
        record = asdict(again)
        record['boot_entry'] = str(again.boot_entry.relative_to(sysroot))
        atomic_write(sysroot / 'quirkbench/prepared.json', canonical(record))
        public = asdict(manifest)
        atomic_write(work / 'deployment.json', canonical(public))
        report = {'schema_version':1, 'revision':manifest.revision, 'https_mutual_auth':True,
                  'signed_commit_verified':True, 'repeat_prepare_idempotent':True,
                  'interrupted_download_remained_unarmed':True,
                  'interrupted_deployment_reconciled_without_duplicate':True,
                  'isolated_etc_and_var':True, 'previous_attempt_still_resolvable':True,
                  'prepared_data_tree':str(sysroot), 'repository_mode':args.repository_mode,
                  'limitations':['Regular-file fixture; no physical boot claim.',
                    'bare-user fixture mode, when selected, avoids inherited host SELinux xattrs; physical ext4 remains to be qualified.']}
        atomic_write(work / 'qualification.json', canonical(report))
        print(json.dumps(report, sort_keys=True), flush=True)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


if __name__ == '__main__':
    main()
