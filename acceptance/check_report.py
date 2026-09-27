"""Validate qualification evidence manifests; does not certify observed causality."""
import argparse
import hashlib
import json
from pathlib import Path

def verify(path, kind):
    path=Path(path).resolve()
    report=json.loads(path.read_text())
    if report.get('schema_version') != 1 or report.get('kind') != kind:
        raise ValueError('wrong report schema or kind')
    if not isinstance(report.get('operator'),str) or not report['operator'].strip():
        raise ValueError('operator observation attribution required')
    if not report.get('evidence'):
        raise ValueError('referenced evidence required')
    entries=list(report['evidence'])
    if kind=='hardware-endurance':
        required={'controller_restart','network_interruption','agent_session_replacement','pause_resume_1','pause_resume_2','storage_pressure','human_recovery_classification'}
        if report.get('status')!='qualified' or report.get('duration_seconds',0)<=30*3600:
            raise ValueError('qualification requires more than 30 real hours')
        if not required <= set(report.get('observed_events',[])) or report.get('unresolved_attempts'):
            raise ValueError('fault events or attempt resolutions missing')
        capabilities=report.get('capabilities',{})
        expected={'usb_boot','internal_storage_exclusion','firmware_preservation','watchdog','kdump','netconsole','suspend_resume'}
        if not expected <= set(capabilities) or any(capabilities[k] not in ('supported','unsupported') for k in expected):
            raise ValueError('all required recovery capabilities must be evaluated')
        if any(capabilities[k]!='supported' for k in ('usb_boot','internal_storage_exclusion','firmware_preservation')):
            raise ValueError('boot and protection requirements must be supported')
    else:
        if report.get('status')!='resolved' or not report.get('patches') or not report.get('source_and_build_identities'):
            raise ValueError('resolved issue requires patches and source/build identities')
        if any(not report.get('observations',{}).get(key) for key in ('baseline','patched','revert','regression')):
            raise ValueError('matched before/after/revert/regression observations required')
        if any(type(report.get('exposure_counts',{}).get(key)) is not int or report['exposure_counts'][key]<=0 for key in ('baseline','patched','revert')) or not report.get('uncertainty'):
            raise ValueError('exposure counts and uncertainty required')
        entries+=report['patches']
    for entry in entries:
        file=(path.parent/entry['path']).resolve()
        if path.parent not in file.parents or not file.is_file():
            raise ValueError('evidence must be a regular file within report directory')
        with file.open('rb') as handle:
            if hashlib.file_digest(handle,'sha256').hexdigest()!=entry['sha256']:
                raise ValueError('evidence hash mismatch')
    return report

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('kind',choices=['hardware-endurance','patch-bundle'])
    parser.add_argument('report',type=Path)
    args=parser.parse_args()
    verify(args.report,args.kind)
    print('Report structure and referenced hashes verified; observations still require review.')
