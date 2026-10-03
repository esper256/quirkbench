"""Controller administration and positively verified external-USB target services."""
from __future__ import annotations
import argparse
from dataclasses import asdict
import json
import sqlite3
from pathlib import Path
import sys
import time
from .contracts import CapabilityReport, Experiment, canonical, digest
from .controller import Controller
from .state_config import configure_state_root, discover_state_root


class CommandParser(argparse.ArgumentParser):
    def parse_args(self,args=None,namespace=None):
        value=super().parse_args(args,namespace)
        if value.command in ('backup','restore'):
            positional,option=('destination','output') if value.command=='backup' else ('backup','input')
            if (getattr(value,positional) is None)==(getattr(value,option) is None):
                self.error(value.command+' requires exactly one positional path or --'+option)
            if getattr(value,option) is not None:setattr(value,positional,getattr(value,option))
        if value.command=='endpoint':
            requirements={'show':(), 'stage':('request_id','host','source_sha256'), 'renew':('request_id','host','source_sha256'),
                'apply':('request_id','identity_sha256','fingerprint','unit'), 'rollback':('request_id','switch_sha256'), 'wizard':('unit',)}
            allowed=set(requirements[value.action])|({'request_id','public_certificate'} if value.action=='show' else
                {'repository_url'} if value.action=='apply' else set())
            for name in ('request_id','host','source_sha256','identity_sha256','fingerprint','repository_url','unit','switch_sha256','public_certificate'):
                if name not in allowed and getattr(value,name):self.error('--'+name.replace('_','-')+' is incompatible with endpoint '+value.action)
            if any(getattr(value,name) is None for name in requirements[value.action]):self.error('endpoint '+value.action+' requires '+', '.join('--'+name.replace('_','-') for name in requirements[value.action]))
            if value.action=='wizard' and value.json:self.error('endpoint wizard requires attended input; use explicit actions with --json')
            if value.public_certificate and value.json:self.error('--public-certificate cannot include --json')
        if value.command=='target':
            client=('url','ca','token_file','report')
            if value.action is None:
                if value.name is not None or any(getattr(value,key) is None for key in client):
                    self.error('target client requires --url, --ca, --token-file and --report')
                if value.json or value.request_id or value.ttl_seconds is not None or value.status_version is not None:
                    self.error('enrollment options require target administration')
            else:
                if value.name is None:self.error('target '+value.action+' requires NAME or TARGET')
                if any(getattr(value,key) is not None for key in client) or value.once or value.interval!=5:
                    self.error('target administration cannot include target client options')
                if value.action=='show' and (value.request_id or value.ttl_seconds is not None):
                    self.error('target show is read-only')
                if value.action!='show' and value.status_version is not None:self.error('--status-version requires target show')
                if value.action not in ('add','drain-approve','retarget-code') and value.ttl_seconds is not None:self.error('--ttl-seconds requires target add, drain-approve or retarget-code')
            if value.action not in ('revoke','retarget-code') and value.generation is not None:self.error('--generation requires target revoke or retarget-code')
            if value.action=='retarget-code':
                if any(getattr(value,key) is None for key in ('generation','new_name','new_uuid','request_id')):
                    self.error('target retarget-code requires --generation, --new-name, --new-uuid and --request-id')
            elif value.new_name is not None or value.new_uuid is not None:self.error('--new-name and --new-uuid require target retarget-code')
            if value.action=='drain-approve':
                if value.file is None:self.error('target drain-approve requires --file with an exact evidence plan')
            elif value.file is not None:self.error('--file requires target drain-approve')
            if value.action=='drain-revoke':
                if value.grant is None:self.error('target drain-revoke requires --grant')
            elif value.grant is not None:self.error('--grant requires target drain-revoke')
        return value


def parser():
    result = CommandParser(prog='quirkbench', description=__doc__)
    result.add_argument('--state', type=Path, help='explicit controller state root; overrides configured selection')
    result.add_argument('--reserve-gib', type=float, default=20)
    result.add_argument('--repositories', type=Path, help='JSON mapping of configured OSTree repository aliases to absolute directories')
    commands = result.add_subparsers(dest='command', required=True)
    investigation = commands.add_parser('investigation', help='attended external investigation, source preparation and lifecycle')
    investigation_actions = investigation.add_subparsers(dest='action', required=True)
    dispatch=investigation_actions.add_parser('dispatch-proposal',help='bind one admitted proposal to explicit candidate/repository choices; existing service advances it')
    dispatch.add_argument('name');dispatch.add_argument('--proposal',required=True);dispatch.add_argument('--candidate')
    dispatch.add_argument('--repository');dispatch.add_argument('--request-id');dispatch.add_argument('--json',action='store_true')
    for name in ('start','brief','baseline','prepare-distribution'):
        command = investigation_actions.add_parser(name)
        command.add_argument('name',help='stable investigation identity')
        command.add_argument('--json',action='store_true')
        if name in ('start','prepare-distribution'):command.add_argument('--request-id',help='original durable retry identity; required for JSON')
        if name=='start':
            command.add_argument('--target',required=True,help='existing enrolled and registered target')
            command.add_argument('--problem',type=Path,help='bounded UTF-8 problem description')
            command.add_argument('--workspace',help='reserved private source workspace identity')
            command.add_argument('--baseline',help='explicit supported baseline ID when catalog selection is ambiguous')
            command.add_argument('--session-seconds',type=int,default=28800)
            command.add_argument('--token-budget',type=int,default=1000000)
    for name in ('status', 'pause', 'resume', 'source', 'prepare-source', 'capture-source', 'release-source'):
        command = investigation_actions.add_parser(name)
        command.add_argument('name', help='existing campaign identity')
        command.add_argument('--json', action='store_true')
        if name in ('source', 'prepare-source', 'capture-source', 'release-source'):
            command.add_argument('--workspace', help='exact workspace; inferred only when unique')
        if name in ('prepare-source', 'capture-source'):
            command.add_argument('--request-id', help='durable retry identity; required for each capture and JSON preparation')
            command.add_argument('--quiesced', action='store_true', help='explicitly acknowledge all source writers have stopped')
        if name == 'prepare-source':
            command.add_argument('--source', type=Path, required=True, help='canonical existing user Git root; original is preserved')
            command.add_argument('--base-oid', required=True, help='full actual Git base OID, also the original HEAD')
            command.add_argument('--allow-untracked', action='append', default=[], metavar='PATH')
    for name in ('prepare-candidate','build','compose'):
        command=investigation_actions.add_parser(name,help='derive immutable retained inputs for the existing worker; grants no attempt approval')
        command.add_argument('name',help='existing investigation identity')
        command.add_argument('--request-id',required=True,help='stable identity for exact replay; use a new identity after failed work')
        command.add_argument('--json',action='store_true')
        if name=='build':
            command.add_argument('--capture',required=True,help='completed source capture operation ID')
            command.add_argument('--candidate',required=True,help='completed candidate preparation operation ID')
        if name=='compose':
            command.add_argument('--build',required=True,help='completed joined build operation ID')
            command.add_argument('--repository',required=True,help='configured controller repository alias and signing policy')
    for name in ('context','history','recipes','proposal-schema','observations','observation','respond'):
        command=investigation_actions.add_parser(name,help='bounded existing-record view' if name!='respond' else 'answer a typed human request')
        command.add_argument('name',help='existing investigation identity')
        command.add_argument('--json',action='store_true')
        if name in ('history','observations'):
            command.add_argument('--after',type=int,default=0,help='cursor for this investigation and query kind')
            command.add_argument('--limit',type=int,default=20)
        if name=='history':command.add_argument('--kind',choices=('attempts','events','evidence'),default='attempts')
        if name in ('observation','respond'):command.add_argument('--request',required=name=='observation')
        if name=='respond':
            command.add_argument('--file',type=Path,help='existing typed observation response; required for machine input')
            command.add_argument('--request-id',required=True,help='durable answer retry identity')
            command.add_argument('--operator',help='operator identity for attended input')
    propose=investigation_actions.add_parser('propose',help='durably admit an external v2 proposal; grants no attempt approval')
    propose.add_argument('name');propose.add_argument('--file',type=Path,required=True)
    propose.add_argument('--request-id',required=True);propose.add_argument('--json',action='store_true')
    proposals=investigation_actions.add_parser('proposals',help='page retained proposals and dispatch intents; never invokes an agent')
    proposals.add_argument('name');proposals.add_argument('--after',type=int,default=0)
    proposals.add_argument('--limit',type=int,default=20);proposals.add_argument('--json',action='store_true')
    baseline_submit=investigation_actions.add_parser('submit-baseline',help='admit one published unmodified baseline to existing jobs; separate exact attempt approval required')
    baseline_submit.add_argument('name');baseline_submit.add_argument('--compose',required=True,help='completed joined composition operation ID')
    baseline_submit.add_argument('--request-id',help='stable retry identity; required with --json')
    baseline_submit.add_argument('--json',action='store_true')
    experiment=commands.add_parser('experiment',help='read immutable experiment identities; queues nothing')
    experiment_actions=experiment.add_subparsers(dest='action',required=True)
    listing=experiment_actions.add_parser('list');listing.add_argument('--investigation',required=True)
    listing.add_argument('--after',type=int,default=0);listing.add_argument('--limit',type=int,default=20);listing.add_argument('--json',action='store_true')
    review=experiment_actions.add_parser('review');review.add_argument('experiment_id');review.add_argument('--json',action='store_true')
    evidence=commands.add_parser('evidence',help='read public investigation evidence without private CAS access')
    evidence_actions=evidence.add_subparsers(dest='action',required=True)
    read=evidence_actions.add_parser('read')
    read.add_argument('digest');read.add_argument('--investigation',required=True,help='authorized existing investigation identity')
    read.add_argument('--offset',type=int,default=0);read.add_argument('--length',type=int,default=16384)
    read.add_argument('--after',type=int,default=0);read.add_argument('--limit',type=int,default=20)
    read.add_argument('--json',action='store_true')
    endpoint = commands.add_parser('endpoint', help='inspect or maintain controller addresses using the existing stopped service')
    endpoint.add_argument('action',choices=['show','stage','renew','apply','rollback','wizard'])
    endpoint.add_argument('--request-id',help='exact retained endpoint request; omitted show selects configured identity')
    endpoint.add_argument('--host',help='literal successor controller bind IP and certificate SAN')
    endpoint.add_argument('--source-sha256',help='exact original identity SHA256 from endpoint show')
    endpoint.add_argument('--identity-sha256',help='exact staged successor identity SHA256')
    endpoint.add_argument('--fingerprint',help='explicit full staged controller certificate SHA256')
    endpoint.add_argument('--repository-url',help='explicit successor repository service HTTPS URL')
    endpoint.add_argument('--unit',type=Path,help='existing native controller user-unit file, verified before apply')
    endpoint.add_argument('--switch-sha256',help='exact retained switch SHA256 for rollback')
    endpoint.add_argument('--public-certificate',action='store_true',help='show only the selected public PEM certificate')
    endpoint.add_argument('--json',action='store_true')
    commands.add_parser('setup-state', help='select private controller state for manual service setup')
    setup = commands.add_parser('setup', help='resume initial controller setup and optional native service startup')
    setup.add_argument('--request-id', help='durable retry identity; required with --json')
    setup.add_argument('--runtime', type=Path, help='verified immutable installed runtime root')
    setup.add_argument('--cache-gib', type=int)
    setup.add_argument('--reserve-gib', type=float, dest='setup_reserve_gib')
    setup.add_argument('--host', help='record the literal controller bind IP')
    setup.add_argument('--port', type=int)
    setup.add_argument('--allow-lan', action='store_true', default=None)
    setup.add_argument('--logout-policy', choices=['session', 'existing_linger'])
    setup.add_argument('--start-service', action='store_true', help='create private local TLS and enable/start the existing controller user unit')
    setup.add_argument('--builder-archive', type=Path, help='admit signed OCI capture/import to the existing worker')
    setup.add_argument('--builder-request-id', help='builder retry identity; defaults to the setup request ID plus -builder')
    setup.add_argument('--json', action='store_true')
    status = commands.add_parser('status', help='read independent controller readiness; never initializes state')
    status.add_argument('--json', action='store_true')
    preferences = commands.add_parser('settings', help='show or configure local retention preferences')
    preferences.add_argument('action', choices=['show','set'])
    preferences.add_argument('key', nargs='?'); preferences.add_argument('value', type=int, nargs='?')
    commands.add_parser('setup-check', help='inspect user service availability without changing host settings')
    install = commands.add_parser('controller-install', help='verify an immutable development archive; optionally activate the idle controller')
    install.add_argument('archive', type=Path, nargs='?')
    install.add_argument('--activate', action='store_true')
    install.add_argument('--rollback', action='store_true')
    install.add_argument('--json', action='store_true')
    install.add_argument('--release-statement', type=Path)
    install.add_argument('--release-signature', type=Path)
    install.add_argument('--release-key', type=Path, help='independently trusted publisher public key')
    install.add_argument('--release-fingerprint', help='full independently trusted publisher fingerprint')
    install.add_argument('--release-recovery', type=Path)
    install.add_argument('--release-builder', type=Path)
    install.add_argument('--release-catalog', type=Path)
    install.add_argument('--release-recovery-manifest', type=Path)
    install.add_argument('--release-recovery-candidate', type=Path)
    release = commands.add_parser('release-install', help='acquire and verify a signed controller; unavailable without independent publisher trust')
    release.add_argument('version')
    release.add_argument('--request-id', help='required with --json; human retries use release-VERSION')
    release.add_argument('--trust-bundle', type=Path, help='independently provisioned production trust bundle')
    release.add_argument('--json', action='store_true')
    recovery_download=commands.add_parser('recovery',help='acquire the factory image matching the signed installed controller')
    recovery_download.add_argument('action',choices=['download'])
    recovery_download.add_argument('--request-id');recovery_download.add_argument('--trust-bundle',type=Path)
    recovery_download.add_argument('--json',action='store_true')
    monitor = commands.add_parser('monitor', help='manually opened, read-only progress dashboard; never opens windows')
    monitor.add_argument('--run', dest='run_id'); monitor.add_argument('--once', action='store_true')
    monitor.add_argument('--json', action='store_true')
    housekeeping = commands.add_parser('maintenance', help='prune proven-stopped staging and optional caches')
    housekeeping.add_argument('action', choices=['prune','retain-run','status','pin','unpin','abandon','abandon-upload']); housekeeping.add_argument('run_id',nargs='?')
    housekeeping.add_argument('--note', default='operator pin')
    housekeeping.add_argument('--output', action='append', default=[]); housekeeping.add_argument('--abandon', action='store_true')
    housekeeping.add_argument('--dry-run', action='store_true')
    housekeeping.add_argument('--json', action='store_true')
    commands.add_parser('demo', help='run the fake build/target/agent durability demonstration')
    inventory = commands.add_parser('target-inventory', help='read reported recovery hardware and candidate planning blockers; queues nothing')
    inventory.add_argument('device_id'); inventory.add_argument('--json', action='store_true')
    device = commands.add_parser('register'); device.add_argument('report', type=Path)
    campaign = commands.add_parser('campaign')
    actions = campaign.add_subparsers(dest='action', required=True)
    create = actions.add_parser('create'); create.add_argument('id'); create.add_argument('--device', required=True, metavar='TARGET_ID', help='registered target ID (existing --device option)')
    for name in ('pause','resume','status'):
        command = actions.add_parser(name); command.add_argument('id')
    submit = actions.add_parser('submit'); submit.add_argument('id'); submit.add_argument('experiment', type=Path)
    budget = actions.add_parser('budget'); budget.add_argument('id'); budget.add_argument('--seconds', type=float, default=28800); budget.add_argument('--tokens', type=int, default=1000000)
    artifact = commands.add_parser('artifact'); artifact.add_argument('action', choices=['put']); artifact.add_argument('file', type=Path)
    operation = commands.add_parser('operation', help='read durable operation records')
    operation_actions = operation.add_subparsers(dest='action', required=True)
    operation_resume = operation_actions.add_parser('resume', help='request current owner to resume an interrupted build/compose job')
    operation_resume.add_argument('operation_id'); operation_resume.add_argument('--request-id', required=True)
    operation_list = operation_actions.add_parser('list', help='page existing operation summaries')
    operation_list.add_argument('--after', type=int, default=0); operation_list.add_argument('--limit', type=int, default=100)
    operation_list.add_argument('--json', action='store_true')
    operation_status = operation_actions.add_parser('status', help='read one operation without invoking recovery or scheduling')
    operation_status.add_argument('operation_id'); operation_status.add_argument('--json', action='store_true')
    operation_watch = operation_actions.add_parser('watch', help='watch persisted operation facts without an agent or scheduler')
    operation_watch.add_argument('operation_id'); operation_watch.add_argument('--json', action='store_true')
    operation_watch.add_argument('--once', action='store_true')
    operation_watch.add_argument('--interval', type=float, default=2)
    operation_events = operation_actions.add_parser('events', help='page durable operation events')
    operation_events.add_argument('operation_id'); operation_events.add_argument('--after', type=int, default=0)
    operation_events.add_argument('--limit', type=int, default=100); operation_events.add_argument('--json', action='store_true')
    operation_output = operation_actions.add_parser('output', help='read a bounded public output range')
    operation_output.add_argument('operation_id'); operation_output.add_argument('digest')
    operation_output.add_argument('--offset', type=int, required=True)
    operation_output.add_argument('--length', type=int, required=True)
    operation_output.add_argument('--json', action='store_true')
    build_cache = commands.add_parser('build-cache', help='inspect or prune private intermediate build snapshots')
    build_cache_actions = build_cache.add_subparsers(dest='action', required=True)
    build_cache_list = build_cache_actions.add_parser('list')
    build_cache_list.add_argument('--json', action='store_true')
    build_cache_prune = build_cache_actions.add_parser('prune')
    build_cache_prune.add_argument('cache_id')
    build_cache_prune.add_argument('--json', action='store_true')
    session = commands.add_parser('session', help='durable investigation observation records')
    session_actions = session.add_subparsers(dest='action', required=True)
    question = session_actions.add_parser('request', help='record an existing typed human-observation request')
    question.add_argument('session_id');question.add_argument('--campaign',required=True);question.add_argument('--file',type=Path,required=True);question.add_argument('--json',action='store_true')
    observations = session_actions.add_parser('observations', help='list typed human requests and answers')
    observations.add_argument('session_id'); observations.add_argument('--json', action='store_true')
    observations.add_argument('--after', type=int, default=0); observations.add_argument('--limit', type=int, default=20)
    observation = session_actions.add_parser('observation', help='read one complete human request and answer')
    observation.add_argument('session_id'); observation.add_argument('--request', required=True); observation.add_argument('--json', action='store_true')
    respond = session_actions.add_parser('respond', help='durably answer a human request')
    respond.add_argument('session_id'); respond.add_argument('--request', required=True)
    respond.add_argument('--file', type=Path, required=True); respond.add_argument('--request-id', required=True)
    recovery = commands.add_parser('recovery-inputs', help='exact stock package acquisition plan and v2 retained inputs')
    recovery_actions = recovery.add_subparsers(dest='action', required=True)
    acquire = recovery_actions.add_parser('acquire-plan', help='prepare recorded acquisition and print exact command; does not download')
    acquire.add_argument('directory',type=Path)
    acquire.add_argument('--spec',type=Path,help='immutable acquisition specification with pinned repository bytes and RPM trust')
    lock = recovery_actions.add_parser('lock', help='verify and retain downloaded binary RPM closure')
    lock.add_argument('directory',type=Path); lock.add_argument('--public-key',type=Path,required=True)
    lock.add_argument('--spec',type=Path,help='same immutable specification used for acquisition; otherwise retains legacy selection')
    lock.add_argument('--builder-image-digest',required=True); lock.add_argument('--diagnostics',type=Path,required=True)
    recipe = recovery_actions.add_parser('recipe', help='generate default stock RecoveryRecipe v2')
    recipe.add_argument('--lock',required=True); recipe.add_argument('--builder-image-digest',required=True)
    recipe.add_argument('--id',help='recipe identity; defaults to the selected lock digest'); recipe.add_argument('--epoch',type=int,required=True)
    recipe.add_argument('--root-mib',type=int,default=2048); recipe.add_argument('--factory-size-mib',type=int,default=4096)
    recipe.add_argument('--experiment-mib',type=int,default=32768); recipe.add_argument('--library-mib',type=int,default=32768)
    recipe.add_argument('--log-budget-mib',type=int,default=4096)
    recovery_image=commands.add_parser('recovery-image',help='admit a complete stock image for the fixed recovery coordinator')
    recovery_image.add_argument('--recipe',required=True); recovery_image.add_argument('--builder-archive',required=True)
    recovery_image.add_argument('--request-id',required=True)
    recovery_images=commands.add_parser('recovery-images',help='list published recovery images and exact retained file paths; read-only')
    recovery_images.add_argument('--json',action='store_true')
    recovery_images.add_argument('--limit',type=int,default=20)
    recovery_images.add_argument('--before',type=int,default=0,help='older image-operation cursor from the preceding page')
    attempt = commands.add_parser('attempt', help='local operator authorization for an exact physical attempt')
    attempt_actions = attempt.add_subparsers(dest='action', required=True)
    for name in ('status','show'):
        inspect = attempt_actions.add_parser(name);inspect.add_argument('attempt_id');inspect.add_argument('--json',action='store_true')
    for name in ('approve', 'reject'):
        decision = attempt_actions.add_parser(name)
        decision.add_argument('attempt_id')
        decision.add_argument('--request-id',help='durable unique decision ID; human omission derives it from the complete exact binding; required with --json')
        decision.add_argument('--operator',help='local operator attribution; default current UID')
        decision.add_argument('--json',action='store_true')
    backup = commands.add_parser('backup',help='backup the controller cut; capture quiesced sources first or report them incomplete')
    backup.add_argument('destination',type=Path,nargs='?');backup.add_argument('--output',type=Path,help='new backup directory; includes coverage and reconciliation guidance')
    restore = commands.add_parser('restore',help='verify backup into a new --state directory and leave scheduling paused')
    restore.add_argument('backup',type=Path,nargs='?');restore.add_argument('--input',type=Path,help='backup directory; private identity and editable Git require separate restoration')
    storage=commands.add_parser('storage',help='bounded read-only retention summary and eligible-cleanup guidance')
    storage.add_argument('--json',action='store_true');storage.add_argument('--after',default='');storage.add_argument('--limit',type=int,default=20)
    resolve = commands.add_parser('resolve'); resolve.add_argument('attempt_id'); resolve.add_argument('disposition', choices=['retry','abandon']); resolve.add_argument('--note', required=True)
    snapshot = commands.add_parser('snapshot'); snapshot.add_argument('campaign_id'); snapshot.add_argument('--source', type=Path, required=True); snapshot.add_argument('files', nargs='+')
    agent = commands.add_parser('agent-step'); agent.add_argument('campaign_id'); agent.add_argument('argv', nargs=argparse.REMAINDER)
    serve = commands.add_parser('serve'); serve.add_argument('--host', default='127.0.0.1', help='controller service bind address'); serve.add_argument('--port', type=int, default=8443); serve.add_argument('--allow-lan', action='store_true'); serve.add_argument('--cert', required=True); serve.add_argument('--key', required=True)
    authentication = serve.add_mutually_exclusive_group(required=True)
    authentication.add_argument('--tokens-file', type=Path)
    authentication.add_argument('--credential-registry', action='store_true', help='explicit registry mode; guided exchange also requires configured native repository publication')
    target = commands.add_parser('target',help='create an invitation, read target facts, or run the legacy HTTPS target client')
    target.add_argument('action',choices=['add','show','revoke','revoke-code','drain-approve','drain-revoke','retarget-code'],nargs='?');target.add_argument('name',nargs='?')
    target.add_argument('--request-id');target.add_argument('--ttl-seconds',type=int);target.add_argument('--json',action='store_true')
    target.add_argument('--status-version',type=int,choices=[1,2],help='versioned target facts; JSON defaults to v1, human output to v2')
    target.add_argument('--generation',help='exact credential generation expected by target revoke')
    target.add_argument('--new-name',help='new enrollment name for an explicitly scoped retarget invitation')
    target.add_argument('--new-uuid',help='exact new SMBIOS system UUID for an explicitly scoped retarget invitation')
    target.add_argument('--file',type=Path,help='exact original evidence manifest for explicit drain approval')
    target.add_argument('--grant',help='exact old-evidence drain grant to revoke')
    target.add_argument('--url'); target.add_argument('--ca'); target.add_argument('--token-file', type=Path); target.add_argument('--report', type=Path); target.add_argument('--once', action='store_true'); target.add_argument('--interval', type=float, default=5)
    watch = commands.add_parser('watch'); watch.add_argument('campaign_id'); watch.add_argument('--interval', type=float, default=2); watch.add_argument('--once', action='store_true'); watch.add_argument('--json', action='store_true'); watch.add_argument('--session', help='include human requests for this session')
    build = commands.add_parser('build',help='build pinned source manifests inside the dedicated Fedora container'); build.add_argument('manifest',type=Path); build.add_argument('--workspace',type=Path); build.add_argument('--campaign')
    candidate = commands.add_parser('candidate-rootfs',help='queue pinned candidate sysroot preparation; grants no execution approval')
    candidate.add_argument('input',type=Path,help='strict candidate-rootfs-input v1 JSON')
    candidate.add_argument('--builder-image-digest',help='pinned Fedora base marker identity')
    candidate.add_argument('--json',action='store_true',help='return the operation response envelope (the default)')
    image = commands.add_parser('image',help='assemble a new regular-file USB image'); image.add_argument('manifest',type=Path)
    qualify = commands.add_parser('qualify-image',help='run ten real UEFI recovery, candidate, load-failure, panic and fallback trials'); qualify.add_argument('image',type=Path); qualify.add_argument('--manifest',type=Path,required=True); qualify.add_argument('--ovmf-code',type=Path,required=True); qualify.add_argument('--ovmf-vars',type=Path,required=True); qualify.add_argument('--work',type=Path,required=True); qualify.add_argument('--timeout',type=int,default=180)
    compose = commands.add_parser('compose',help='compose and sign a complete experimental Fedora OSTree revision'); compose.add_argument('manifest',type=Path); compose.add_argument('--workspace',type=Path); compose.add_argument('--publish-repo',type=Path,required=True); compose.add_argument('--campaign')
    repo = commands.add_parser('serve-repository',help='serve read-only OSTree content with mutual TLS'); repo.add_argument('--host',default='127.0.0.1',help='controller repository service bind address'); repo.add_argument('--port',type=int,default=8444); repo.add_argument('--allow-lan',action='store_true'); repo.add_argument('--cert',required=True); repo.add_argument('--key',required=True); repo.add_argument('--client-ca',required=True)
    repo.add_argument('--credential-registry', action='store_true', help='require live registered leaf certificate in addition to mutual TLS')
    serve.add_argument('--job-worker',type=Path,help='installed fixed build/compose worker')
    serve.add_argument('--service-runtime',type=Path,help=argparse.SUPPRESS)
    for command in (build,compose,candidate):
        command.add_argument('--request-id'); command.add_argument('--wait',action='store_true')
        command.add_argument('--builder-archive',help='retained OCI archive SHA256; defaults to private service configuration')
        command.add_argument('--builder-config-digest',help='actual local builder image config ID; defaults to private service configuration')
    compose.add_argument('--builder-image-digest',help='Fedora base marker identity; defaults to private service configuration')
    serve.add_argument('--recovery-worker',type=Path,help='installed fixed worker; enables recovery-image operations only')
    serve.add_argument('--recovery-signing-home',type=Path)
    serve.add_argument('--recovery-public-key',type=Path)
    serve.add_argument('--recovery-fingerprint')
    maintenance = commands.add_parser('library-maintenance', help='fence scheduling for explicit recovery library maintenance'); maintenance.add_argument('action', choices=['begin','finish']); maintenance.add_argument('device_id'); maintenance.add_argument('--selection')
    commands.add_parser('target-service', help='run the verified USB target supervisor; never run on the controller')
    commands.add_parser('doctor', help='report optional build and VM prerequisites')
    return result


def _main(argv=None):
    args = parser().parse_args(argv)
    if args.command=='storage':
        from .storage_view import status
        from .contracts import ContractError
        from .operations import operation_response
        from .state_reader import safe_text
        try:
            answer=status(discover_state_root(args.state),after=args.after,limit=args.limit)
            print(json.dumps(answer,sort_keys=True) if args.json else safe_text(json.dumps(answer['data'],indent=2,sort_keys=True)))
            return 0
        except (OSError,ValueError,sqlite3.Error) as exc:
            code,status_code=('INVALID_INPUT',2) if isinstance(exc,ContractError) else ('INFRASTRUCTURE',5)
            message=str(exc)[:512] if status_code==2 else 'storage state unavailable; no work queued or cleanup performed'
            if args.json:print(json.dumps(operation_response(error={'code':code,'message':message,'retryable':status_code==5}),sort_keys=True))
            else:print(code+': '+message,file=sys.stderr)
            return status_code
    if args.command in ('experiment','attempt'):
        from .attended_views import ApprovalReader,experiments,review,attempt,decide
        from .contracts import Conflict,ContractError
        from .operations import operation_response
        from .state_reader import safe_text
        try:
            root=discover_state_root(args.state)
            reader=ApprovalReader(root)
            if args.command=='experiment':
                answer=experiments(reader,args.investigation,after=args.after,limit=args.limit) if args.action=='list' else review(reader,args.experiment_id)
            elif args.action in ('show','status'):
                answer=attempt(reader,args.attempt_id,legacy=args.action=='status' and not args.json)
            else:answer=decide(root,args)
            if args.json:print(json.dumps(answer,sort_keys=True))
            elif args.command=='attempt' and args.action=='status':print(json.dumps(answer,indent=2,sort_keys=True))
            elif args.command=='attempt' and args.action in ('approve','reject') and args.request_id:
                print(json.dumps(answer['data']['decision'],indent=2,sort_keys=True))
            else:print(safe_text(json.dumps(answer['data'],indent=2,sort_keys=True)))
            return 0
        except (OSError,ValueError,sqlite3.Error) as exc:
            code,status=(('CONFLICT',3) if isinstance(exc,Conflict) else ('INVALID_INPUT',2) if isinstance(exc,ContractError) else ('INFRASTRUCTURE',5))
            message=str(exc)[:512] if status!=5 else 'attended state unavailable; inspect retained state and controller readiness'
            if args.json:print(json.dumps(operation_response(error={'code':code,'message':message,'retryable':status==5}),sort_keys=True))
            else:print(code+': '+message,file=sys.stderr)
            return status
    if args.command in ('investigation','evidence'):
        if args.command=='evidence':args.name=args.investigation;args.action='evidence'
        from .investigation_sources import execute
        from .contracts import Conflict, ContractError
        from .operations import operation_response
        from .store import StoragePressure
        try:
            answer = execute(discover_state_root(args.state), args)
            if args.json:
                print(json.dumps(answer, sort_keys=True))
            elif args.action=='brief':
                from .investigations import render_brief
                print(render_brief(answer['data']))
            elif answer.get('operation_id'):
                print('Investigation operation accepted: ' + answer['operation_id'])
                print('Inspect: ' + answer['data']['status_command'])
                print('Progress: ' + answer['data']['monitor_command'])
                if args.action=='propose':
                    print('Proposal retained; bind it with dispatch-proposal. Acceptance grants no attempt approval.')
                else:
                    print('Preparation/capture completion requires the existing controller service. Resume the investigation explicitly if paused.')
            else:
                from .state_reader import safe_text
                print(safe_text(json.dumps(answer['data'], indent=2, sort_keys=True)))
            return 0
        except (OSError, ValueError, sqlite3.Error, StoragePressure) as exc:
            from .investigation_pipeline import PipelineBlocked
            from .candidate_rootfs_operation import CandidateBlocked
            code = 'BLOCKED' if isinstance(exc,(PipelineBlocked,CandidateBlocked,StoragePressure)) else 'CONFLICT' if isinstance(exc, Conflict) else 'INVALID_INPUT' if isinstance(exc, ContractError) else 'INFRASTRUCTURE'
            message = str(exc)[:512] if code != 'INFRASTRUCTURE' else 'source service unavailable; inspect the retained operation and readiness'
            if args.json:
                print(json.dumps(operation_response(error={'code': code, 'message': message, 'retryable': code in ('BLOCKED','INFRASTRUCTURE')}), sort_keys=True))
            else:
                print(code + ': ' + message, file=sys.stderr)
            return {'BLOCKED':4, 'CONFLICT': 3, 'INVALID_INPUT': 2, 'INFRASTRUCTURE': 5}[code]
    if args.command=='recovery':
        from .recovery_download import submit
        from .release_trust import ReleaseUnavailable
        from .setup_contracts import SetupUnavailable
        from .operations import operation_response
        from .contracts import ContractError,Conflict
        try:
            if args.json and not args.request_id:raise ContractError('recovery download --json requires --request-id')
            answer=submit(discover_state_root(args.state),args.request_id,trust_bundle=args.trust_bundle)
            if args.json:print(json.dumps(answer,sort_keys=True))
            else:
                print('Recovery acquisition accepted: '+answer['operation_id'])
                print('Inspect: '+answer['data']['status_command'])
                print('Progress: '+answer['data']['monitor_command'])
                print('Image authentication and compatibility precede publication. Qualification and writing media require separate authorization.')
            return 0
        except (OSError,ValueError,sqlite3.Error) as exc:
            code,status=(('UNAVAILABLE',4) if isinstance(exc,(ReleaseUnavailable,SetupUnavailable)) else
                ('CONFLICT',3) if isinstance(exc,Conflict) else ('INVALID_INPUT',2) if isinstance(exc,ContractError) else ('INFRASTRUCTURE',5))
            if args.json:print(json.dumps(operation_response(error={'code':code,'message':str(exc),'retryable':False}),sort_keys=True))
            else:print('Recovery acquisition blocked: '+str(exc),file=sys.stderr)
            return status
    if args.command=='endpoint':
        from .endpoint_facade import execute
        from .operations import operation_response
        from .contracts import Conflict,ContractError
        from .setup_contracts import SetupUnavailable
        try:
            root=args.state or discover_state_root()
            if root is None:raise SetupUnavailable('run quirkbench setup before endpoint maintenance')
            if args.action=='wizard':
                from .endpoint_facade import wizard
                wizard(root,unit=args.unit)
                return 0
            answer=execute(root,args.action,request_id=args.request_id,host=args.host,source_sha256=args.source_sha256,
                identity_sha256=args.identity_sha256,fingerprint=args.fingerprint,repository_url=args.repository_url,
                unit=args.unit,switch_sha256=args.switch_sha256)
            if args.public_certificate:print(answer['certificate_pem'],end='')
            elif args.json:print(json.dumps(answer,sort_keys=True))
            else:
                print('Endpoint request: '+answer['request_id'])
                for key,label in [('identity_sha256','Identity SHA256'),('certificate_sha256','Certificate SHA256'),('controller_url','Controller'),('repository_url','Repository'),('switch_sha256','Switch SHA256')]:
                    if answer.get(key) is not None:print(label+': '+answer[key])
                if args.action=='show':print('Recorded public identity; current reachability remains separate.')
                elif args.action in ('stage','renew'):print('Identity staged with the existing CA. Apply its exact identity and fingerprint with the controller user service stopped.')
                else:print('Configuration restored.' if answer['rolled_back'] else 'Configuration applied.')
                if args.action!='show':print('Start the existing controller user service when ready, then use recovery endpoint maintenance for each target. Reachability and target migration remain separate.')
            return 0
        except (OSError,ValueError,RuntimeError,sqlite3.Error) as exc:
            code,status=(('UNAVAILABLE',4) if isinstance(exc,SetupUnavailable) else ('CONFLICT',3) if isinstance(exc,Conflict) else ('INVALID_INPUT',2) if isinstance(exc,ContractError) else ('INFRASTRUCTURE',5))
            message=str(exc)[:512] if code!='INFRASTRUCTURE' else 'endpoint maintenance unavailable; exact request retained'
            if args.json:print(json.dumps(operation_response(error={'code':code,'message':message,'retryable':code in ('UNAVAILABLE','CONFLICT')}),sort_keys=True))
            else:print('Endpoint maintenance blocked: '+message,file=sys.stderr)
            return status
    if args.command=='target' and args.action is not None:
        from .target_setup import add_target,show_target
        from .operations import operation_response
        from .contracts import Conflict,ContractError
        from .setup_contracts import SetupUnavailable
        request_id=args.request_id
        try:
            root=discover_state_root(args.state).expanduser().absolute()
            if args.action=='add':
                if args.json and not request_id:raise ContractError('target add --json requires --request-id')
                answer=add_target(root,args.name,request_id,ttl_seconds=args.ttl_seconds)
                request_id=answer['record']['request_id']
            elif args.action=='show':answer=show_target(root,args.name,version=args.status_version or (1 if args.json else 2))
            elif args.action=='retarget-code':
                from .retarget_invitation import issue
                answer=issue(root,args.name,args.generation,args.new_name,args.new_uuid,request_id,
                    ttl_seconds=args.ttl_seconds if args.ttl_seconds is not None else 300)
            elif args.action=='drain-approve':
                from .evidence_drain import approve,load,validate_plan
                from .state_reader import read_file
                if not request_id:raise ContractError('target drain-approve requires an explicit --request-id')
                path=args.file.expanduser().absolute()
                plan=validate_plan(load(read_file(path.parent,path.name,limit=65536)))
                answer=approve(root,args.name,plan,request_id,ttl_seconds=args.ttl_seconds if args.ttl_seconds is not None else 900)
            elif args.action=='drain-revoke':
                from .evidence_drain import revoke
                answer=revoke(root,args.name,args.grant)
            else:
                from .target_lifecycle import revoke_target
                if args.json and not request_id:raise ContractError('target revocation --json requires --request-id')
                answer=revoke_target(root,args.name,request_id,generation=args.generation,action=args.action)
                request_id=answer['request_id']
            if args.json:print(json.dumps(operation_response(data=answer),sort_keys=True))
            elif args.action in ('add','retarget-code'):
                record=answer['record']
                print('Enrollment request: '+record['request_id'])
                print('Controller: '+record['controller_url'])
                print('Compare this full certificate SHA-256 on the recovery console: '+record['certificate_sha256'])
                print('One-use code: '+answer['code'])
                print('Code ID: '+record['code_id'])
                print('Expires at Unix time: '+str(record['expires_at']))
                print('Pairing is pending. Exact candidate and attempt approval is still required.')
                if args.action=='retarget-code':
                    print('Retarget invitation only. Local one-shot clearance, original evidence preservation and stopped activation remain required.')
            elif args.action=='show':
                print('Target: '+(answer['device_id'] or answer['target']))
                print('Enrollment: '+answer['enrollment']['state'])
                print('Credentials live: '+str(answer['enrollment']['credentials_live']).lower())
                print('Recorded recovery mode: '+str(answer['recovery']['reported_mode'] or 'unavailable'))
                contact=answer['recovery']['contact_current']
                print('Recent authenticated contact: '+('unknown' if contact is None else 'within 30 seconds' if contact else 'not current'))
                print('Candidate input blockers: '+', '.join(answer['candidate_preparation']['blocking_reasons']))
                print('Exact candidate and attempt approval is still required.')
            elif args.action=='drain-approve':
                record=answer['record']
                print('Old-evidence drain grant: '+record['grant_id'])
                print('Original attempt: '+record['plan']['attempt_id'])
                print('Private credential file: '+answer['credential_file'])
                print('Expires at Unix time: '+str(record['expires_at']))
                print('Only the approved original evidence may be uploaded and acknowledged. Registration, execution and completion remain blocked.')
            elif args.action=='drain-revoke':print('Revoked old-evidence drain grant: '+answer['grant_id'])
            else:
                print('Revocation request: '+answer['request_id'])
                print('Revoked: '+(answer['generation'] or answer['code_id']))
                print('Campaigns paused at revocation: '+(', '.join(answer['paused_campaigns_at_revoke']) or 'none'))
                print('Unresolved attempts at revocation: '+(', '.join(answer['unresolved_attempts_at_revoke']) or 'none'))
                print('Workers pending at revocation: '+(', '.join(answer['workers_pending_at_revoke']) or 'none'))
                if answer['work_lists_truncated']:
                    print('Lists show the first 1000 identities. Counts at revocation: '
                          +str(answer['paused_campaign_count_at_revoke'])+' campaigns, '
                          +str(answer['unresolved_attempt_count_at_revoke'])+' unresolved attempts, '
                          +str(answer['worker_count_at_revoke'])+' workers.')
                print('Physical shutdown and one-shot clearance require local verification. Old evidence retains its original attribution; draining requires separate maintenance.')
            return 0
        except (OSError,ValueError,sqlite3.Error) as exc:
            code,status=(('UNAVAILABLE',4) if isinstance(exc,SetupUnavailable) else ('CONFLICT',3) if isinstance(exc,Conflict)
                else ('INVALID_INPUT',2) if isinstance(exc,ContractError) else ('INFRASTRUCTURE',5))
            message=str(exc)[:512] if code!='INFRASTRUCTURE' else 'target setup unavailable; retry the retained request'
            if args.json:print(json.dumps(operation_response(data={'request_id':request_id},error={'code':code,'message':message,'retryable':status==5}),sort_keys=True))
            else:print(code+': '+message,file=sys.stderr)
            return status
    if args.command == 'release-install':
        from .release_install import acquire_install
        from .release_trust import ReleaseUnavailable
        from .contracts import Conflict, ContractError
        from .operations import operation_response
        request_id = args.request_id or ('release-' + args.version)
        try:
            if args.json and not args.request_id:
                raise ContractError('release-install --json requires --request-id')
            answer = acquire_install(args.version, request_id, trust_bundle=args.trust_bundle)
            if args.json:
                print(json.dumps(operation_response(data=answer), sort_keys=True))
            else:
                print('Release installation request: ' + request_id)
                print('Installed runtime: ' + answer['runtime_root'])
                print('Setup and artifact qualification remain incomplete.')
            return 0
        except (OSError, ValueError) as exc:
            code, status = (('UNAVAILABLE', 4) if isinstance(exc, ReleaseUnavailable) else
                            ('CONFLICT', 3) if isinstance(exc, Conflict) else
                            ('INVALID_INPUT', 2) if isinstance(exc, ContractError) else ('INFRASTRUCTURE', 5))
            message = str(exc)[:512] if code != 'INFRASTRUCTURE' else 'release acquisition unavailable; retry recorded request'
            if args.json:
                print(json.dumps(operation_response(data={'request_id': request_id},
                    error={'code': code, 'message': message, 'retryable': code == 'INFRASTRUCTURE'}), sort_keys=True))
            else:
                print(request_id + ': ' + code + ': ' + message, file=sys.stderr)
            return status
    if args.command in ('setup', 'status'):
        from .controller_setup import setup_controller, controller_status, setup_progress
        from .setup_contracts import SetupUnavailable
        from .contracts import Conflict, ContractError
        from .operations import operation_response
        operation_id = None
        try:
            if args.command == 'setup':
                if args.json and not args.request_id:
                    raise ContractError('setup --json requires --request-id')
                answer = setup_controller(args.state, request_id=args.request_id, runtime_root=args.runtime,
                    cache_gib=args.cache_gib, reserve_gib=args.setup_reserve_gib, host=args.host,
                    port=args.port, allow_lan=args.allow_lan, logout_policy=args.logout_policy)
                if args.start_service:
                    from .setup_service import install_service
                    service_result = install_service()
                    answer = {**controller_status(Path(answer['state_root'])), 'service_setup_result': service_result}
                if args.builder_archive:
                    from .builder_setup import prepare
                    answer['builder_preparation'] = prepare(Path(answer['state_root']),
                        answer['setup_progress']['intent']['runtime_root'], args.builder_archive,
                        args.builder_request_id or answer['setup_progress']['request_id'] + '-builder')
                elif args.builder_request_id:
                    raise ContractError('--builder-request-id requires --builder-archive')
            else:
                answer = controller_status(args.state)
            progress = answer['setup_progress']
            operation_id = (answer['builder_preparation']['operation_id'] if answer.get('builder_preparation')
                            else progress['setup_id'] if progress else None)
            if args.json:
                print(json.dumps(operation_response(operation_id=operation_id, data=answer), sort_keys=True))
            else:
                if progress:
                    print('Setup request: ' + progress['request_id'])
                print('Controller state: ' + answer['state_root'])
                print('Setup complete: no; pending ' + ', '.join(answer['pending_integration']))
                print('Background work ready: ' + str(answer['background_work_ready']).lower())
                print('Targets: ' + (str(answer['readiness']['target_count']) if answer['readiness']['target_count'] is not None else 'unknown'))
                if answer.get('service_setup_result'):
                    print('Controller certificate SHA256: ' + answer['service_setup_result']['certificate_sha256'])
                if answer.get('builder_preparation'):
                    print('Builder preparation operation: ' + answer['builder_preparation']['operation_id'])
                for instruction in answer['instructions']:
                    print(instruction)
            return 0
        except (OSError, ValueError, sqlite3.Error) as exc:
            code, exit_code = (('UNAVAILABLE', 4) if isinstance(exc, SetupUnavailable) else
                               ('CONFLICT', 3) if isinstance(exc, Conflict) else
                               ('INVALID_INPUT', 2) if isinstance(exc, ContractError) else ('INFRASTRUCTURE', 5))
            try:
                progress = setup_progress()
                operation_id = progress['setup_id'] if progress else None
            except (OSError, ValueError):
                progress = None
            message = str(exc)[:512] if code != 'INFRASTRUCTURE' else 'setup/status unavailable; recorded intent retained'
            if args.json:
                print(json.dumps(operation_response(operation_id=operation_id,
                    data={'request_id': progress['request_id']} if progress else None,
                    error={'code': code, 'message': message, 'retryable': code == 'INFRASTRUCTURE'}), sort_keys=True))
            else:
                if progress:
                    print('Setup request: ' + progress['request_id'], file=sys.stderr)
                print(code + ': ' + message, file=sys.stderr)
            return exit_code
    explicit_state = args.state is not None
    if args.command == 'controller-install':
        from .controller_install import install, activate, rollback
        from .contracts import Conflict, ContractError
        from .operations import operation_response
        try:
            release_inputs = (args.release_statement, args.release_signature, args.release_key, args.release_fingerprint)
            asset_inputs = (args.release_recovery, args.release_builder, args.release_catalog)
            sidecars = (args.release_recovery_manifest, args.release_recovery_candidate)
            assets = None
            if any(value is not None for value in (*asset_inputs, *sidecars)):
                if not all(value is not None for value in (*release_inputs, *asset_inputs)):
                    raise ContractError('release assets require complete signed release inputs and all three asset paths')
                assets = dict(zip(('recovery_image', 'builder_archive', 'baseline_catalog'), asset_inputs))
                if any(value is not None for value in sidecars):
                    if not all(value is not None for value in sidecars):
                        raise ContractError('release recovery compatibility requires both manifest and candidate')
                    assets.update(zip(('recovery_manifest', 'recovery_candidate'), sidecars))
            authenticated = None
            if any(value is not None for value in release_inputs):
                if not all(value is not None for value in release_inputs) or args.rollback or args.archive is None:
                    raise ContractError('release verification requires archive, statement, signature, independent key and fingerprint')
                from .controller_release import bounded_file, verify_release
                parameters = {'assets': assets} if assets is not None else {}
                authenticated = verify_release(args.archive, bounded_file(args.release_statement, 16384),
                    bounded_file(args.release_signature, 65536), args.release_key, args.release_fingerprint, **parameters)
            if args.rollback:
                if args.archive or args.activate: raise ContractError('--rollback takes no archive or --activate')
                answer = rollback(discover_state_root(args.state))
            else:
                if args.archive is None: raise ContractError('controller-install requires ARCHIVE')
                if authenticated is None:
                    answer = install(args.archive)
                else:
                    statement = authenticated['statement']
                    answer = install(args.archive, expected_archive_sha256=statement['controller_archive_sha256'],
                                     expected_version=statement['controller_version'])
                if args.activate: answer = activate(answer, discover_state_root(args.state))
                if authenticated is not None:
                    answer = {**answer, 'distribution_verification': authenticated}
            print(json.dumps(operation_response(data=answer), sort_keys=True))
            return 0
        except (OSError, ValueError, sqlite3.Error) as exc:
            code = 'CONFLICT' if isinstance(exc, Conflict) else 'INVALID_INPUT' if isinstance(exc, ContractError) else 'INFRASTRUCTURE'
            print(json.dumps(operation_response(error={'code':code,'message':str(exc)[:512],'retryable':False}), sort_keys=True))
            return 3 if code == 'CONFLICT' else 2 if code == 'INVALID_INPUT' else 5
    if args.command == 'setup-state':
        try:
            selected = configure_state_root(args.state)
            Controller(Path(selected['state_root']), reserve_bytes=0)
            from .retention_settings import DEFAULTS,set_setting
            if not (Path(selected['state_root'])/'settings.json').exists():
                set_setting(Path(selected['state_root']),'completed_attempts',DEFAULTS['completed_attempts'])
            print(json.dumps(selected, sort_keys=True))
            return 0
        except (ValueError, OSError, sqlite3.Error) as exc:
            print(f'setup blocked: {exc}', file=sys.stderr)
            return 2
    if args.command in ('build','compose','candidate-rootfs') or (args.command=='operation' and args.action=='resume'):
        from .job_cli import run
        return run(args)
    if args.command=='recovery-images':
        from .recovery_listing import list_images,render_images
        from .state_reader import StateReader
        try:
            answer=list_images(StateReader(discover_state_root(args.state).expanduser().absolute()),before=args.before,limit=args.limit)
            print(json.dumps(answer,sort_keys=True) if args.json else render_images(answer))
            return 0
        except (OSError,ValueError,sqlite3.Error) as exc:
            if args.json:
                from .operations import operation_response
                print(json.dumps(operation_response(error={'code':'UNAVAILABLE','message':('Recovery image listing unavailable: '+str(exc))[:512],'retryable':False}),sort_keys=True))
            else: print('Recovery image listing unavailable: '+str(exc),file=sys.stderr)
            return 2
    if args.command == 'settings':
        try:
            root=discover_state_root(args.state).expanduser().absolute()
            from .retention_settings import settings,set_setting
            if args.action=='show':
                if args.key is not None or args.value is not None: raise ValueError('settings show takes no arguments')
                if not (root/'controller.sqlite').is_file(): raise ValueError('run setup-state first')
                answer=settings(root)
            else:
                if args.key is None or args.value is None: raise ValueError('settings set requires KEY VALUE')
                if not (root/'controller.sqlite').is_file(): raise ValueError('run setup-state first')
                answer=set_setting(root,args.key,args.value)
            print(json.dumps({'retention':answer},sort_keys=True)); return 0
        except (OSError,ValueError) as exc:
            print('settings unavailable: '+str(exc),file=sys.stderr); return 2
    if args.command in ('monitor', 'maintenance'):
        try:
            root = discover_state_root(args.state).expanduser().absolute()
            if args.command == 'monitor':
                from .tui import monitor
                return monitor(root, run_id=args.run_id, once=args.once, json_output=args.json)
            from .maintenance import prune
            if args.action in ('status','pin','unpin','abandon','abandon-upload'):
                from .retention import status,pin,abandon
                if args.action=='status': answer=status(root)
                elif args.action=='abandon': answer=abandon(root,args.run_id)
                elif args.action=='abandon-upload':
                    from .upload_retention import abandon as abandon_upload
                    answer=abandon_upload(root,args.run_id)
                else:
                    if not args.run_id: raise ValueError('pin/unpin requires retention OWNER')
                    pin(root,args.run_id,args.note if args.action=='pin' else None)
                    answer={'owner':args.run_id,'pinned':args.action=='pin'}
            elif args.action=='retain-run':
                if not args.run_id or args.dry_run:
                    raise ValueError('retain-run requires RUN_ID and does not accept --dry-run')
                from .development_run import retain
                from .maintenance import private_lock
                with private_lock(root/'coordinator.lock'), private_lock(root/'build.lock'):
                    answer=retain(root,args.run_id,outputs=args.output,abandon=args.abandon)
            else:
                if args.run_id or args.output or args.abandon:
                    raise ValueError('prune accepts only --dry-run and --json')
                answer = prune(root, dry_run=args.dry_run)
            print(json.dumps(answer, sort_keys=True))
            return 0
        except KeyboardInterrupt:
            return 130
        except (ValueError, OSError, sqlite3.Error) as exc:
            print(f'{args.command} unavailable: {exc}', file=sys.stderr)
            return 2
    if args.command == 'setup-check':
        from .controller_setup import inspect_user_manager
        report=inspect_user_manager()
        from .controller_service import require_ready
        try: report.update(require_ready(discover_state_root(args.state)))
        except (ValueError,OSError,sqlite3.Error) as exc: report['instructions'].append(str(exc))
        from .controller_install import installation_report
        report.update(installation_report(discover_state_root(args.state),service_ready=report.get('background_work_ready',False)))
        if report['installations']['mismatch']:
            report['instructions'].append('CLI, configured service or last advertised service revisions differ; use controller-install ARCHIVE --activate after reconciling work.')
        print(json.dumps(report, sort_keys=True))
        return 0
    if args.command == 'build-cache':
        from .build import BuildError
        from .build_cache import BuildStageCache
        try:
            root = discover_state_root(args.state).resolve() / 'intermediate-cache'
            if args.action == 'list' and not root.exists():
                items = []
            else:
                cache = BuildStageCache(root)
                if args.action == 'list':
                    items = cache.list()
                else:
                    removed = cache.prune(args.cache_id)
                    if not removed:
                        raise BuildError('build cache ID does not exist')
                    items = [{'cache_id': args.cache_id, 'pruned': True}]
            if args.json:
                print(json.dumps({'schema_version': 1, 'items': items}, sort_keys=True))
            else:
                for item in items:
                    if item.get('pruned'):
                        print(f"Pruned {item['cache_id']}")
                    else:
                        print(f"{item['cache_id']} {item['lineage']} {item['stage']}")
            return 0
        except (BuildError, OSError, ValueError) as exc:
            print(f'build cache unavailable: {exc}', file=sys.stderr)
            return 3
    if args.command == 'session':
        from .contracts import Conflict, ContractError
        from .operations import operation_response
        from .product_contracts import MAX_DOCUMENT_BYTES
        try:
            args.state = discover_state_root(args.state)
            if args.reserve_gib < 0:
                raise ContractError('reserve must be nonnegative')
            raw = None
            if args.action in ('request','respond'):
                with args.file.open('rb') as stream:
                    raw = stream.read(MAX_DOCUMENT_BYTES + 1)
            from .state_reader import StateReader
            controller = (Controller(args.state.resolve(), reserve_bytes=int(args.reserve_gib*1024**3))
                          if args.action in ('request','respond') else StateReader(args.state.expanduser().absolute()))
            if args.action == 'request':
                from .product_contracts import load_document
                request=load_document(raw,'observation-request')
                if request['session_id']!=args.session_id: raise ContractError('observation session mismatch')
                data={'request_id':controller.issue_observation(args.campaign,request)}
            elif args.action == 'observations':
                data = controller.list_observations(args.session_id, after=args.after, limit=args.limit)
            elif args.action == 'observation':
                data = controller.observation_detail(args.session_id, args.request)
            else:
                data = controller.respond_observation(args.session_id, args.request, args.request_id, raw)
            if args.action in ('request','respond') or args.json:
                print(json.dumps(operation_response(data=data), sort_keys=True))
            elif args.action == 'observations':
                for item in data['items']:
                    request = item['request']
                    print(f"{request['request_id']} {request['kind']} {item['state']}: {request['prompt']}")
                    if item.get('truncated'):
                        print(f"  Full record: quirkbench session observation {args.session_id} --request {request['request_id']}")
                if data['next_cursor'] is not None:
                    print(f"More observations: --after {data['next_cursor']}")
            else:
                print(json.dumps(data, indent=2, sort_keys=True))
            return 0
        except Exception as exc:
            code = 'CONFLICT' if isinstance(exc, Conflict) else 'INVALID_INPUT' if isinstance(exc, (ContractError, FileNotFoundError, IsADirectoryError)) else 'INFRASTRUCTURE'
            status = 3 if code == 'CONFLICT' else 2 if code == 'INVALID_INPUT' else 5
            message = str(exc)[:512] if code != 'INFRASTRUCTURE' else 'observation service unavailable'
            if args.action in ('request','respond') or args.json:
                print(json.dumps(operation_response(error={'code': code, 'message': message,
                                                           'retryable': code == 'INFRASTRUCTURE'}), sort_keys=True))
            else:
                print(f'{code}: {message}', file=sys.stderr)
            return status
    if args.command == 'operation':
        from .contracts import Conflict, ContractError
        from .operations import operation_response
        try:
            args.state = discover_state_root(args.state)
            if args.reserve_gib < 0:
                raise ContractError('reserve must be nonnegative')
            if args.action == 'watch':
                import math
                if not math.isfinite(args.interval) or not 0.5 <= args.interval <= 60:
                    raise ContractError('operation watch interval must be 0.5..60 seconds')
                if not (args.state / 'controller.sqlite').is_file():
                    raise ContractError('operation watch requires existing controller state')
            from .state_reader import StateReader
            controller = StateReader(args.state.expanduser().absolute())
            if args.action == 'watch':
                from .operation_watch import watch_operation
                try:
                    return watch_operation(controller, args.operation_id, once=args.once,
                                           json_output=args.json, interval=args.interval)
                except KeyboardInterrupt:
                    return 130
            if args.action == 'list':
                answer = controller.operation_list(after=args.after, limit=args.limit)
            elif args.action == 'status':
                answer = controller.operation_status(args.operation_id)
            elif args.action == 'events':
                answer = controller.operation_events(args.operation_id, after=args.after, limit=args.limit)
            else:
                answer = controller.operation_output(args.operation_id, args.digest,
                                                     offset=args.offset, length=args.length)
            if args.json:
                print(json.dumps(answer, sort_keys=True))
            elif args.action == 'events':
                from .monitor import render_operation_events
                print(render_operation_events(answer))
            elif args.action == 'output':
                print(f"Read {answer['data']['length']} bytes from public output {answer['data']['sha256']}; use --json for content")
            elif args.action == 'list':
                for row in answer['data']['items']:
                    print(f"{row['id']} {row['kind']} {row['state']} {row['stage'] or ''}")
                if answer['data']['next_cursor'] is not None:
                    print(f"More operations: --after {answer['data']['next_cursor']}")
            else:
                from .monitor import render_operation
                failure = None
                if answer['data']['state'] == 'FAILED':
                    try:
                        failure = controller.operation_failure(args.operation_id)
                    except (ContractError, OSError):
                        pass
                print(render_operation(answer, failure))
            return 0
        except Exception as exc:
            code = 'CONFLICT' if isinstance(exc, Conflict) else 'INVALID_INPUT' if isinstance(exc, ContractError) else 'INFRASTRUCTURE'
            status = 3 if code == 'CONFLICT' else 2 if code == 'INVALID_INPUT' else 5
            message = str(exc)[:512] if code != 'INFRASTRUCTURE' else 'operation status unavailable'
            if args.json:
                print(json.dumps(operation_response(error={'code': code, 'message': message,
                                                           'retryable': code == 'INFRASTRUCTURE'}), sort_keys=True))
            else:
                print(f'{code}: {message}', file=sys.stderr)
            return status
    try:
        args.state = discover_state_root(args.state)
        read_only = args.command in ('watch', 'target-inventory') or (args.command == 'campaign' and args.action == 'status')
        if not read_only and args.command not in ('doctor','target','endpoint','target-service','serve-repository'):
            from .state_config import outside_checkout
            outside_checkout(args.state)
            if not explicit_state and not args.state.exists():
                raise ValueError('run quirkbench setup-state before creating controller work')
        if read_only and not (args.state / 'controller.sqlite').is_file():
            raise ValueError('controller state unavailable; run quirkbench setup-state first')
        if args.command in ('build', 'compose', 'image'):
            from .state_config import outside_checkout
            from .retention import managed_path
            if args.command in ('build', 'compose'):
                import uuid
                args.workspace=args.workspace or args.state/'workspaces'/(args.command+'-'+uuid.uuid4().hex)
                managed_path(args.state,args.workspace)
                if args.command=='compose': managed_path(args.state,args.publish_repo)
            else:
                raw_image = json.loads(args.manifest.read_bytes())
                managed_path(args.state,Path(raw_image['output']).parent)
        if args.reserve_gib < 0:
            raise ValueError('reserve must be nonnegative')
        repository_paths = {}
        deployment_repository = None
        repository_config = args.repositories or (args.state / 'repositories.json')
        if repository_config.exists():
            if repository_config.is_symlink(): raise ValueError('repository configuration cannot be a symlink')
            repository_paths = json.loads(repository_config.read_bytes())
            if not isinstance(repository_paths,dict) or any(not isinstance(v,str) or not Path(v).is_absolute() for v in repository_paths.values()): raise ValueError('repository configuration requires absolute directory paths')
            needs_repository = (args.command in {'compose','serve-repository','backup','restore','agent-step','snapshot'}
                                or (args.command == 'campaign' and args.action == 'submit'))
            if needs_repository:
                from .ostree_repository import OstreeRepository
                deployment_repository = OstreeRepository({k:Path(v) for k,v in repository_paths.items()})
        elif args.repositories is not None:
            raise ValueError('repository configuration file does not exist')
        controller_options = {'reserve_bytes':int(args.reserve_gib*1024**3), 'deployment_repository':deployment_repository}
        if args.command == 'serve-repository':
            from .repository_http import make_repository_server
            if not repository_paths: raise ValueError('serve-repository requires configured repositories')
            if args.host not in ('localhost','127.0.0.1','::1') and not args.allow_lan: raise ValueError('LAN binding requires --allow-lan')
            from .credential_registry import CredentialRegistry
            registry = CredentialRegistry(args.state) if args.credential_registry else None
            server = make_repository_server((args.host,args.port),repository_paths,args.cert,args.key,args.client_ca, credential_registry=registry)
            print('OSTree repository service ready; mutual TLS required.',flush=True)
            try: server.serve_forever()
            finally: server.server_close()
            return 0
        elif args.command == 'demo':
            from .simulation import demo
            answer = demo(args.state)
        elif args.command == 'image':
            from .image import ImageInputs, create_image
            raw=json.loads(args.manifest.read_bytes())
            if not isinstance(raw,dict) or set(raw)-set(ImageInputs.__dataclass_fields__):raise ValueError('invalid image input fields')
            fields={key:Path(value) if key not in ('size_mib','root_mib','smoke','experiment_mib','library_mib','log_budget_mib') and value is not None else value for key,value in raw.items()}
            from .retention import work,published
            image_dir=Path(raw['output']).parent
            with work(args.state,'recovery',image_dir) as run_owner:
                result=create_image(ImageInputs(**fields))
                controller=Controller(args.state.resolve(),**controller_options)
                values=[controller.store.put_file(p).sha256 for p in image_dir.iterdir() if p.is_file() and p.name!='process-groups.json']
                published(controller.root,run_owner,values)
                answer={'manifest':str(result)}
        elif args.command == 'qualify-image':
            from .qemu import QemuInputs, qualify_boot_cycle
            controller=Controller(args.state.resolve(),**controller_options)
            from .retention import work,published
            with work(controller.root,'qualification',args.work.resolve()) as run_owner:
                report=qualify_boot_cycle(QemuInputs(args.image.resolve(),args.ovmf_code.resolve(),args.ovmf_vars.resolve(),args.work.resolve(),timeout_seconds=args.timeout),args.manifest.resolve(),event=lambda phase,message:print(f'{phase}: {message}',file=sys.stderr,flush=True))
                artifact=controller.store.put_file(report)
                published(controller.root,run_owner,[artifact.sha256])
                answer={'qualification':str(report)}
        elif args.command == 'doctor':
            import shutil
            names = ('podman','distrobox','qemu-system-x86_64','qemu-img','virt-fw-vars','grub2-mkimage','dracut','openssl','ostree','rpm-ostree','rpmbuild','createrepo_c')
            answer = {'executables': {name: shutil.which(name) for name in names}, 'physical_hardware_qualified': False}
        elif args.command == 'target-service':
            from .runtime import main as target_main
            return target_main([])
        elif args.command == 'target':
            from .target import TargetAgent
            from .transport import HTTPSDeviceClient, TransportError
            report = CapabilityReport.from_dict(json.loads(args.report.read_bytes()))
            token = args.token_file.read_text().strip()
            target = TargetAgent(HTTPSDeviceClient(args.url, report.device_id, token, args.ca), args.state, report)
            if args.interval <= 0:
                raise ValueError('interval must be positive')
            failures = 0
            while True:
                try:
                    answer = {'step': target.step()}
                    failures = 0
                except (OSError, TransportError) as exc:
                    failures += 1
                    if args.once or failures >= 10 or str(exc) in ('HTTP 401','HTTP 403'):
                        raise
                    print(f'Target reconnecting: consecutive failures={failures}; durable outbox retained', file=sys.stderr, flush=True)
                if args.once:
                    break
                time.sleep(min(60, args.interval * 2**min(failures,4)))
        elif args.command == 'restore':
            restored = Controller.restore(args.backup, args.state, **controller_options)
            answer = {'restored': str(restored.root), 'scheduling': 'paused'}
            if args.input:
                from .backup_coverage import load_summary
                answer['historical_backup_coverage']=load_summary(args.backup)
                answer['next_steps']=['Restore private identity and operator configuration separately.',
                    'Restore editable Git separately; reconcile original source ownership before capture.',
                    'Reconcile target execution, recovery return and pending evidence before explicit resume.']
        else:
            if read_only:
                from .state_reader import StateReader
                controller = StateReader(args.state.expanduser().absolute())
            else:
                controller = Controller(args.state, **controller_options)
            if args.command == 'library-maintenance':
                answer = controller.library_maintenance(args.device_id, args.selection, finish=args.action == 'finish')
            elif args.command == 'target-inventory':
                answer = controller.target_inventory(args.device_id)
            elif args.command == 'register':
                answer = controller.register(CapabilityReport.from_dict(json.loads(args.report.read_bytes())))
            elif args.command == 'campaign':
                if args.action == 'create':
                    controller.create_campaign(args.id, args.device)
                elif args.action == 'submit':
                    controller.submit_attended(args.id, Experiment.from_dict(json.loads(args.experiment.read_bytes())))
                elif args.action == 'budget':
                    controller.configure_budget(args.id, args.seconds, args.tokens)
                elif args.action in ('resume', 'pause'):
                    getattr(controller, args.action)(args.id)
                answer = controller.status(args.id)
            elif args.command == 'watch':
                from .monitor import render
                if args.interval <= 0:
                    raise ValueError('watch interval must be positive')
                while True:
                    snapshot = controller.monitor(args.campaign_id)
                    if args.session:
                        snapshot['observations'] = controller.list_observations(args.session)
                        with controller.transaction() as db:
                            row = db.execute('SELECT campaign FROM observation_requests WHERE session=? LIMIT 1', (args.session,)).fetchone()
                        if row is not None and row['campaign'] != args.campaign_id:
                            raise ValueError('observation session belongs to another campaign')
                    if args.json:
                        print(json.dumps(snapshot, sort_keys=True), flush=True)
                    else:
                        if sys.stdout.isatty() and not args.once:
                            print('\033[2J\033[H', end='')
                        print(render(snapshot), flush=True)
                    if args.once:
                        return 0
                    time.sleep(args.interval)
            elif args.command == 'artifact':
                artifact=controller.store.put_file(args.file)
                from .retention import register
                register(controller.root,'input',[artifact.sha256])
                answer=asdict(artifact)
            elif args.command == 'recovery-inputs':
                from .recovery_inputs import acquisition_command,retain_packages,generate_recipe
                if args.action=='acquire-plan':
                    from .retention import managed_path,register
                    from .recovery_acquisition import load_spec,stage_spec,freeze_legacy_spec,MAX_SPEC
                    from .state_reader import read_file
                    spec=load_spec(read_file(args.spec.resolve().parent,args.spec.name,limit=MAX_SPEC)) if args.spec else freeze_legacy_spec()
                    directory=managed_path(controller.root,args.directory.resolve())
                    if directory.exists(): raise ValueError('acquisition requires a fresh inputs directory')
                    directory.mkdir(parents=True,mode=0o700)
                    (directory/'rpms').mkdir(mode=0o700)
                    stage_spec(spec,directory)
                    from .store import atomic_write
                    from .controller import controller_boot_id
                    import os
                    atomic_write(directory/'process-groups.json',canonical({'boot':controller_boot_id(),'pid_namespace':os.readlink('/proc/self/ns/pid'),'groups':[]}))
                    spec_digest=controller.store.put(canonical(spec)).sha256
                    import uuid
                    owner=register(controller.root,'input',[spec_digest],owner='storage:acquisition-v1:'+spec_digest+':'+uuid.uuid4().hex,paths=(directory,),state='WAITING')
                    answer={'argv':['python3','-m','quirkbench.recovery_inputs','--state',str(controller.root),'--owner',owner],
                            'dnf_argv':acquisition_command(directory/'rpms',spec=spec),'spec_sha256':spec_digest,'selection':'explicit' if args.spec else 'historical-candidate-compatibility','directory':str(directory/'rpms'),'executed':False,'retention_owner':owner}
                elif args.action=='lock':
                    from .retention import verified_acquisition,work,published
                    verified_acquisition(controller.root,args.directory.resolve())
                    from .recovery_acquisition import load_spec,MAX_SPEC
                    from .state_reader import read_file
                    from .recovery_acquisition import completed_spec
                    spec=completed_spec(controller.root,args.directory.resolve())
                    if args.spec:
                        requested=load_spec(read_file(args.spec.resolve().parent,args.spec.name,limit=MAX_SPEC))
                        if spec is None or canonical(requested)!=canonical(spec): raise ValueError('lock specification differs from acquisition')
                    with work(controller.root,'input',args.diagnostics.resolve()) as diagnostic_owner:
                        from .ostree import CommandRunner
                        from .store import atomic_write
                        def failure_log(raw):
                            atomic_write(args.diagnostics.resolve()/'package-verification.log',raw)
                            return 'retained private package-verification.log'
                        def query(argv):
                            return CommandRunner(lambda *args:None,lambda:None,timeout_s=60,diagnostic=failure_log)(argv)
                        def verify(argv,timeout_s):
                            return CommandRunner(lambda *args:None,lambda:None,timeout_s=timeout_s,diagnostic=failure_log)(argv)
                        lock=retain_packages(args.directory.resolve(),args.public_key.resolve(),controller.store,
                            args.diagnostics.resolve()/'diagnostics',builder_image_digest=args.builder_image_digest,
                            query=query,signature_runner=verify,spec=spec)
                        published(controller.root,diagnostic_owner,[digest(canonical(lock))],disposable_work=True)
                    from .retention import register,release_acquisition,release_group
                    value=controller.store.put(canonical(lock)).sha256
                    register(controller.root,'input',[value],owner='input:'+value)
                    release_group(controller.root,diagnostic_owner)
                    release_acquisition(controller.root,args.directory.resolve(),value)
                    answer={'lock':lock,'sha256':value}
                else:
                    layout={name:getattr(args,name) for name in ('root_mib','factory_size_mib','experiment_mib','library_mib','log_budget_mib')}
                    recipe=generate_recipe(args.lock,controller.store,recipe_id=args.id,
                        builder_image_digest=args.builder_image_digest,source_date_epoch=args.epoch,layout=layout)
                    from .retention import register
                    value=controller.store.put(canonical(recipe)).sha256
                    register(controller.root,'recipe',[value],owner='recipe:'+value)
                    answer={'recipe':recipe,'sha256':value}
            elif args.command == 'recovery-image':
                answer=controller.admit_recovery_image(args.request_id,args.recipe,args.builder_archive)
            elif args.command == 'attempt':
                if args.action=='status': answer=controller.operator_attempt_status(args.attempt_id)
                else:
                    answer = controller.decide_attempt(args.attempt_id,
                        'approved' if args.action == 'approve' else 'rejected', request_id=args.request_id)
            elif args.command == 'backup':
                answer = {'backup': controller.backup(args.destination,coverage=bool(args.output))}
                if args.output:
                    from .backup_coverage import load_summary
                    answer['coverage']=load_summary(args.destination)
                    answer['next_steps']=['Keep private identity and operator configuration in a separate protected backup.',
                        'For incomplete sources: stop writers, investigation capture-source NAME --workspace ID --quiesced --request-id ID; inspect operation status, then back up to a new destination.',
                        'Reconcile offline targets and pending evidence; target-only backlog is unknown.']
            elif args.command == 'resolve':
                answer = controller.resolve(args.attempt_id, args.disposition, args.note)
            elif args.command == 'snapshot':
                from .agent import snapshot_sources
                answer = snapshot_sources(controller, args.campaign_id, args.source, args.files)
            elif args.command == 'agent-step':
                from .agent import CommandAgent, run_decision
                answer = run_decision(controller, args.campaign_id, CommandAgent(args.argv))
            elif args.command == 'serve':
                from .transport import make_server
                from .credential_registry import CredentialRegistry
                registry = CredentialRegistry(controller.root) if args.credential_registry else None
                tokens = None if registry is not None else json.loads(args.tokens_file.read_bytes())
                with controller.lifecycle() as owner:
                    coordinator=None
                    if args.recovery_worker is not None:
                        if any(value is None for value in (args.recovery_signing_home,args.recovery_public_key,args.recovery_fingerprint)):
                            raise ValueError('recovery coordinator requires explicit signing trust and worker configuration')
                        from .worker_service import SystemdUserWorkerServices
                        from .recovery_coordinator import RecoveryImageCoordinator
                        services=SystemdUserWorkerServices(worker_program=args.recovery_worker.resolve())
                        owner.reconcile_units(services)
                        coordinator=RecoveryImageCoordinator(owner,services,signing_home=args.recovery_signing_home,
                            trusted_public_key=args.recovery_public_key,fingerprint=args.recovery_fingerprint)
                    jobs=None
                    if args.job_worker is not None:
                        from .worker_service import SystemdUserWorkerServices
                        from .job_coordinator import JobCoordinator
                        services=SystemdUserWorkerServices(worker_program=args.job_worker.resolve(),development=True)
                        owner.reconcile_units(services)
                        jobs=JobCoordinator(owner,services)
                    from .enrollment_runtime import publication_runtime
                    with publication_runtime(controller,registry=registry,service_runtime=args.service_runtime,
                            host=args.host,port=args.port,certfile=args.cert,keyfile=args.key,allow_lan=args.allow_lan) as publication:
                        server = make_server(controller, host=args.host, port=args.port, certfile=args.cert, keyfile=args.key, device_tokens=tokens, credential_registry=registry, allow_lan=args.allow_lan, enrollment_service=publication.application,tls_context=publication.tls_context)
                        try:
                            if coordinator is None and jobs is None:
                                server.service_actions=owner.housekeep_requested
                                server.serve_forever()
                            else:
                                import threading
                                thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
                                try:
                                    from contextlib import nullcontext
                                    from .controller_service import readiness_heartbeat
                                    heartbeat=(readiness_heartbeat(owner,args.service_runtime,capabilities=publication.capabilities)
                                               if jobs is not None and args.service_runtime is not None else nullcontext([]))
                                    with heartbeat as failures:
                                        while True:
                                            if failures: raise failures[0]
                                            result=coordinator.tick() if coordinator is not None else None
                                            if result is not None: print('RECOVERY_IMAGE '+json.dumps(result,sort_keys=True),flush=True)
                                            result=jobs.tick() if jobs is not None else None
                                            if result is not None: print('JOB '+json.dumps(result,sort_keys=True),flush=True)
                                            owner.housekeep_requested()
                                            time.sleep(2)
                                finally:
                                    server.shutdown(); thread.join(5)
                        finally:
                            server.server_close()
                answer = {'stopped': True}
            else:
                raise ValueError('unknown command')
        if args.command == 'target-inventory' and args.json:
            from .operations import operation_response
            answer = operation_response(data=answer)
        print(json.dumps(answer, indent=2, sort_keys=True))
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        if args.command == 'target-inventory':
            from .operations import operation_response
            from .contracts import ContractError, Conflict
            code, status = ('CONFLICT', 3) if isinstance(exc, Conflict) else (('INVALID_INPUT', 2) if isinstance(exc, (ValueError, ContractError)) else ('INFRASTRUCTURE', 5))
            if args.json:
                print(json.dumps(operation_response(error={'code': code, 'message': 'Inventory query failed; no work queued.', 'retryable': status == 5})))
            else:
                print('Inventory query failed; no work queued.', file=sys.stderr)
            return status
        # Avoid accidentally echoing provider credentials or subprocess output.
        print(f'{type(exc).__name__}: {exc}' if isinstance(exc, (ValueError, FileNotFoundError)) or type(exc).__name__ in ('BuildError','ImageError','QemuError','BootError','CommissionError') else f'{type(exc).__name__}: operation failed; progress retained', file=sys.stderr)
        return 1


def main(argv=None):
    """A publication barrier, not a scheduler; read-only commands do no housekeeping."""
    args=parser().parse_args(argv)
    readonly=(args.command in ('storage','experiment','build','compose','candidate-rootfs','monitor','watch','target-inventory','operation','doctor','setup-check','status','recovery-images',
                               'target','endpoint','target-service','serve-repository') or
              (args.command=='campaign' and args.action=='status') or
              (args.command=='attempt' and args.action in ('status','show')) or
              (args.command=='investigation' and args.action in ('status','source','brief','baseline','context','history','recipes','proposal-schema','proposals','observations','observation')) or args.command=='evidence' or
              (args.command=='settings' and args.action=='show') or
              (args.command=='maintenance' and args.action in ('status','prune')) or
              (args.command=='session' and args.action in ('observations','observation')) or
              (args.command=='build-cache' and args.action=='list'))
    if readonly or args.command in ('setup-state','setup','serve','controller-install','release-install'): return _main(argv)
    try:
        root=discover_state_root(args.state).expanduser().absolute()
        if not (root/'controller.sqlite').is_file(): return _main(argv)
        from .maintenance import private_lock,prune
        from .contracts import Conflict
        # Abandonment must exclude the acquisition claim-to-launch interval.
        exclusive = args.command == 'maintenance' and args.action in ('abandon','abandon-upload')
        with private_lock(root/'command.lock',shared=not exclusive):
            result=_main(argv)
        try: prune(root)
        except (OSError,ValueError,sqlite3.Error) as exc:
            print('Housekeeping deferred: '+type(exc).__name__+'. Use maintenance status/prune.',file=sys.stderr)
        return result
    except (OSError,ValueError) as exc:
        print('command unavailable: '+str(exc),file=sys.stderr); return 2
