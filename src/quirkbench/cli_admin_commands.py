"""Admin command arguments; services own behavior and authorization."""
from pathlib import Path
from .cli_parser import action


def build(root):
    connection(root)
    for name in ('list','show','export','delete'):
        p=action(root,'admin diagnostics '+name,{'list':'List reported recovery diagnostics','show':'Inspect a recovery report; reported data is not recovery proof','export':'Export opaque recovery report attachments to a new directory','delete':'Delete one report and its unreferenced files with controller workers stopped'}[name],command='diagnostics',internal_action=name)
        if name!='list':p.add_argument('report_id',help='Exact received report SHA256')
        else:
            p.add_argument('--after',default='',help='Continue after the returned report cursor');p.add_argument('--limit',type=int,default=20,help='Maximum diagnostic reports to return')
        if name=='export':p.add_argument('--output',type=Path,required=True,help='New directory, never an existing destination')
    p = action(root, 'admin repository configure', 'Configure a repository using an existing signing key; controller must be stopped', command='publication', internal_action='setup')
    p.add_argument('--repository', required=True, help='fresh explicit repository alias beneath controller state')
    p.add_argument('--url', required=True, help='HTTPS controller certificate host with a separate repository port')
    p.add_argument('--signing-home', type=Path, required=True, help='existing operator-provisioned GnuPG home; no keys are created')
    p.add_argument('--fingerprint', required=True, help='full uppercase fingerprint of the explicit composition signing key')
    p.add_argument('--request-id', required=True, help='retain this identity and exact choices for retry')
    p.set_defaults(unit=None)
    p.add_argument('--reserve-gib', type=float, default=20, help='Free storage reserve in GiB')
    p = action(root, 'admin controller run', 'Run the configured controller in this foreground session', command='controller-run', internal_action=None)
    p.add_argument('--engine', choices=['podman', 'docker'], help='Container engine to use for workers')
    p.add_argument('--worker-image', help='exact local worker image configuration ID (sha256:...)')
    p.add_argument('--podman-cgroup-manager', choices=['systemd', 'cgroupfs'], help='deliberate Podman manager override; otherwise use its configuration')
    p = action(root, 'admin install', 'Install a signed controller using independently configured publisher trust', command='release-install', internal_action=None)
    p.add_argument('version', help='Exact published controller version')
    p.add_argument('--request-id', help='Retry identity; defaults to release-VERSION')
    p.add_argument('--trust-bundle', type=Path, help='independently provisioned production trust bundle')
    p = action(root, 'admin operation list', 'List internal work records for troubleshooting', command='operation', internal_action='list')
    p.add_argument('--after', type=int, default=0, help='Continue after the returned cursor')
    p.add_argument('--limit', type=int, default=100, help='Maximum records to return')
    p.add_argument('--reserve-gib', type=float, default=20, help='Free storage reserve in GiB')
    p = action(root, 'admin operation show', 'Inspect an internal work record for troubleshooting', command='operation', internal_action='status')
    p.add_argument('operation_id', help='Internal operation ID for troubleshooting')
    p.add_argument('--reserve-gib', type=float, default=20, help='Free storage reserve in GiB')
    p = action(root, 'admin operation events', 'Read events for one internal work record', command='operation', internal_action='events')
    p.add_argument('operation_id', help='Internal operation ID for troubleshooting')
    p.add_argument('--after', type=int, default=0, help='Continue after the returned cursor')
    p.add_argument('--limit', type=int, default=100, help='Maximum records to return')
    p.add_argument('--reserve-gib', type=float, default=20, help='Free storage reserve in GiB')
    p = action(root, 'admin operation output', 'Read an authorized range of retained operation output', command='operation', internal_action='output')
    p.add_argument('operation_id', help='Internal operation ID for troubleshooting')
    p.add_argument('digest', help='SHA256 of the authorized output listed by the related record')
    p.add_argument('--offset', type=int, required=True, help='Starting byte offset for this read')
    p.add_argument('--length', type=int, required=True, help='Maximum bytes to read (at most 16384)')
    p.add_argument('--reserve-gib', type=float, default=20, help='Free storage reserve in GiB')
    p = action(root, 'admin operation resume', 'Request reconciliation and continuation of interrupted internal work', command='operation', internal_action='resume')
    p.add_argument('operation_id', help='Internal operation ID for troubleshooting')
    p.add_argument('--request-id', required=True, help='Durable request identity for safe retries')
    p.add_argument('--reserve-gib', type=float, default=20, help='Free storage reserve in GiB')
    p = action(root, 'admin storage show', 'Show retained storage and eligible cleanup', command='storage', internal_action=None)
    p.add_argument('--after', default='', help='Continue after the returned cursor')
    p.add_argument('--limit', type=int, default=20, help='Maximum records to return')
    for name,description in [
        ('prune','Remove eligible stopped staging and optional caches'),
        ('pin','Retain an exact storage owner with a note'),
        ('unpin','Remove an explicit retention pin'),
        ('abandon','Abandon interrupted work after its workers are reconciled'),
        ('abandon-upload','Abandon an incomplete evidence upload'),
        ('retain-run','Retain selected development outputs before cleanup')]:
        p=action(root,'admin storage '+name,description,command='maintenance',internal_action=name)
        p.set_defaults(run_id=None,note='operator pin',output=[],abandon=False,dry_run=False)
        if name!='prune':p.add_argument('run_id',help='Exact storage owner, upload or development-run ID for this action')
        if name=='pin':p.add_argument('--note',default='operator pin',help='Reason for retaining this storage owner')
        if name=='prune':p.add_argument('--dry-run',action='store_true',help='Preview eligible removal without deleting files')
        if name=='retain-run':
            p.add_argument('--output',action='append',default=[],help='Relative development output to retain; repeat for each file')
            p.add_argument('--abandon',action='store_true',help='Abandon the development run after retaining its selected output')
    p = action(root, 'admin storage cache list', 'Inspect intermediate build snapshots', command='build-cache', internal_action='list')
    p = action(root, 'admin storage cache prune', 'Remove an eligible intermediate build snapshot', command='build-cache', internal_action='prune')
    p.add_argument('cache_id', help='Exact snapshot ID returned by cache list')
    p = action(root, 'admin settings show', 'Show retention preferences', command='settings', internal_action='show')
    p.set_defaults(key=None,value=None)
    p = action(root, 'admin settings set', 'Set an explicit retention preference', command='settings', internal_action='set')
    p.add_argument('key', help='Retention setting name from settings show')
    p.add_argument('value', type=int, help='Nonnegative integer value for the selected setting')
    p = action(root, 'admin backup', 'Back up controller state and report source coverage', command='backup', internal_action=None)
    p.add_argument('destination', nargs='?', type=Path, help='New directory in which to write the backup')
    p.add_argument('--output', type=Path, help='new backup directory; includes coverage and reconciliation guidance')
    p.add_argument('--reserve-gib', type=float, default=20, help='Free storage reserve in GiB')
    p.add_argument('--repositories', type=Path, help='Explicit repository mapping file')
    p = action(root, 'admin restore', 'Restore into separate new state and leave investigations paused', command='restore', internal_action=None)
    p.add_argument('backup', nargs='?', type=Path, help='Existing backup directory to verify and restore')
    p.add_argument('--input', type=Path, help='backup directory; identity and editable Git require separate restoration')
    p.add_argument('--reserve-gib', type=float, default=20, help='Free storage reserve in GiB')
    p.add_argument('--repositories', type=Path, help='Explicit repository mapping file')
    p = action(root, 'admin recovery maintenance begin', 'Fence target scheduling for recovery library maintenance', command='library-maintenance', internal_action='begin')
    p.add_argument('device_id', help='Target ID')
    p.add_argument('--selection', help='SHA256 of the exact retained library selection')
    p = action(root, 'admin recovery maintenance finish', 'Finish explicit recovery library maintenance', command='library-maintenance', internal_action='finish')
    p.add_argument('device_id', help='Target ID')
    p.add_argument('--selection', help='SHA256 of the exact retained library selection')


def connection(root):
    descriptions={'show':'Show controller addresses and public certificate identity',
        'stage':'Stage a successor controller address and certificate',
        'renew':'Stage a renewed controller certificate',
        'apply':'Apply an exact staged identity with the controller stopped',
        'rollback':'Restore configuration from an exact recorded switch'}
    fields={'request_id':'Exact retained request identity', 'host':'Literal controller bind IP and certificate SAN',
        'source_sha256':'Original identity SHA256 from connection show', 'identity_sha256':'Staged successor identity SHA256',
        'fingerprint':'Full staged certificate SHA256', 'repository_url':'Successor repository HTTPS URL',
        'switch_sha256':'Exact retained configuration switch SHA256'}
    required={'show':(), 'stage':('request_id','host','source_sha256'), 'renew':('request_id','host','source_sha256'),
              'apply':('request_id','identity_sha256','fingerprint'), 'rollback':('request_id','switch_sha256')}
    for name,description in descriptions.items():
        p=action(root,'admin connection '+name,description,command='endpoint',internal_action=name)
        p.set_defaults(**{key:None for key in fields},unit=None,public_certificate=False)
        selected=required[name]+(('request_id',) if name=='show' else ('repository_url',) if name=='apply' else ())
        for key in selected:p.add_argument('--'+key.replace('_','-'),required=key in required[name],help=fields[key])
        if name=='show':p.add_argument('--public-certificate',action='store_true',help='Print the selected public PEM certificate')
