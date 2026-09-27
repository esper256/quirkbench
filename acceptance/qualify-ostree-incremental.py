#!/usr/bin/env python3
"""Qualify a signed changed-object update on a preserved regular-file sysroot."""
from dataclasses import asdict
import argparse
import json
from pathlib import Path
import threading
import time
from urllib.parse import urlsplit

from quirkbench.contracts import canonical
from quirkbench.deployment import DeploymentManifest
from quirkbench.image import _copy_tree
from quirkbench.ostree import OstreeBackend, Remote
from quirkbench.repository_http import make_repository_server
from quirkbench.store import atomic_write


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('prior-work','work','manifest','repository','public-key'):
        parser.add_argument('--'+name,type=Path,required=True)
    args=parser.parse_args()
    prior=args.prior_work.resolve();work=args.work.resolve()
    work.mkdir(parents=True,exist_ok=False)
    manifest=DeploymentManifest.from_dict(json.loads(args.manifest.read_bytes()))
    sysroot=work/'data'
    _copy_tree(prior/'data',sysroot)
    marker=sysroot/'.regular-file-fixture'
    if not marker.is_file():
        raise ValueError('source must be the regular-file deployment qualification fixture')
    trust=json.loads((sysroot/'quirkbench/remotes'/(manifest.repository+'.json')).read_bytes())['identity']
    url=urlsplit(trust['url'])
    if url.scheme!='https' or url.hostname!='127.0.0.1' or not url.port or url.path!='/'+manifest.repository:
        raise ValueError('fixture remote must be the recorded loopback mTLS publisher')
    cert,key=prior/'tls-cert.pem',prior/'tls-key.pem'
    server=make_repository_server(('127.0.0.1',url.port),{manifest.repository:args.repository.resolve()},cert,key,cert)
    fetched=set();fetch_lock=threading.Lock();handler=server.RequestHandlerClass
    class ObservedHandler(handler):
        def do_GET(self):
            if '/objects/' in self.path:
                with fetch_lock:fetched.add(self.path)
            return super().do_GET()
    server.RequestHandlerClass=ObservedHandler
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    def guard(path):
        if path.resolve()!=sysroot or path.is_symlink() or not marker.is_file():
            raise ValueError('regular-file fixture identity changed')
    backend=OstreeBackend(sysroot,{manifest.repository:Remote(trust['url'],cert,args.public_key.resolve(),cert,key)},verify_storage=guard,
                          progress=lambda phase,message:print(f'{phase}: {message}',flush=True),reserve_bytes=0)
    try:
        prior_attempt=backend.inspect('qualification-two')
        assert prior_attempt.revision!=manifest.revision,'qualification requires a different authorized revision'
        before=sum(1 for path in (sysroot/'ostree/repo/objects').glob('*/*') if path.is_file())
        start=time.monotonic()
        prepared=backend.prepare(manifest,'qualification-upgrade')
        elapsed=time.monotonic()-start
        after=sum(1 for path in (sysroot/'ostree/repo/objects').glob('*/*') if path.is_file())
        assert prepared.revision==manifest.revision
        assert backend.prepare(manifest,'qualification-upgrade')==prepared
        assert backend.inspect('qualification-two').revision==prior_attempt.revision
        state=sysroot/'ostree/deploy'/backend._stateroot('qualification-upgrade')
        assert not list(state.rglob('quirkbench-contamination'))
        record=asdict(prepared);record['boot_entry']=str(prepared.boot_entry.relative_to(sysroot))
        atomic_write(sysroot/'quirkbench/prepared.json',canonical(record))
        atomic_write(work/'deployment.json',canonical(manifest.to_dict()))
        report={'schema_version':1,'qualification':'signed-incremental-deployment','prior_revision':prior_attempt.revision,'revision':manifest.revision,
                'https_mutual_auth':True,'recorded_remote_trust_preserved':True,'signed_commit_verified':True,'prior_attempt_preserved':True,
                'fresh_etc_and_var':True,'repeat_prepare_idempotent':True,'objects_before':before,'objects_after':after,'objects_added':after-before,
                'distinct_object_requests':len(fetched),'prepare_elapsed_s':round(elapsed,3),'prepared_data_tree':str(sysroot),
                'limitations':['Regular-file bare-user fixture; physical ext4/USB remain unqualified.','Original outage/lost-ack regression evidence remains in prior-work/qualification.json.']}
        assert 0<report['objects_added']<before,'expected a changed-object update, not a full repository transfer'
        atomic_write(work/'qualification.json',canonical(report));print(json.dumps(report,sort_keys=True),flush=True)
    finally:
        server.shutdown();server.server_close();thread.join(timeout=5)


if __name__=='__main__':main()
