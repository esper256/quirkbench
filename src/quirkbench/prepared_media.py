"""Versioned controller-prepared geometry; never authorizes device writes.

The factory identity and GPT types remain those of the existing six-role
assembler. Unlike historical commissioning records, this record says nothing
about target RAM and must be compared with observed media before use.
"""
from __future__ import annotations

from .commission import CommissionError, CommissionIdentity
from .contracts import canonical, digest, sha256

ALIGNMENT = 2048  # sectors, one MiB at the supported 512-byte sector size
LIBRARY_OVERHEAD_MIB = 16
MIN_MUTABLE_MIB = 64
FIELDS = {'schema_version', 'record_type', 'artifact_sha256', 'disk_guid',
          'partition_uuids', 'device_bytes', 'geometry', 'library_payload_bytes',
          'library_overhead_bytes', 'factory_data_end', 'complete'}
FIELDS_V2 = FIELDS - {'complete'} | {'completion'}


def plan_layout(factory: CommissionIdentity, device_bytes: int, *, factory_data_end: int, library_payload_bytes=0):
    """Keep fixed partitions and split actual remaining aligned bytes equally.

    The empty library retains only filesystem overhead because six roles are
    consumed by existing boot policy. No target RAM or speculative pack budget.
    A populated factory experiment filesystem cannot be silently shrunk.
    """
    if (type(device_bytes) is not int or device_bytes <= 0 or device_bytes % 512
            or type(library_payload_bytes) is not int or library_payload_bytes < 0):
        raise CommissionError('prepared media requires actual sector-aligned capacity')
    end = ((device_bytes // 512 - 33) // ALIGNMENT) * ALIGNMENT
    start = factory.partition_starts[3]
    library = ((library_payload_bytes + 1024**2 - 1) // 1024**2
               + LIBRARY_OVERHEAD_MIB) * ALIGNMENT
    remaining = end - start - library
    experiment = (remaining // (2 * ALIGNMENT)) * ALIGNMENT
    evidence = remaining - experiment
    if min(experiment, evidence) < MIN_MUTABLE_MIB * ALIGNMENT:
        raise CommissionError('USB cannot fit recovery and minimum experiment/evidence filesystems')
    if (type(factory_data_end) is not int or factory_data_end < start
            or factory_data_end >= start + experiment):
        raise CommissionError('factory experiment filesystem cannot be shrunk')
    fixed = list(zip(factory.partition_starts[:3], factory.fixed_ends))
    return [list(pair) for pair in fixed] + [
        [start, start + experiment - 1],
        [start + experiment, start + experiment + library - 1],
        [start + experiment + library, end - 1]]


def record(factory, artifact_sha256, device_bytes, *, factory_data_end: int, library_payload_bytes=0, complete=False, version=1):
    value = {'schema_version': 1, 'record_type': 'prepared-media',
             'artifact_sha256': sha256(artifact_sha256), 'disk_guid': factory.disk_guid,
             'partition_uuids': list(factory.partition_uuids), 'device_bytes': device_bytes,
             'geometry': plan_layout(factory, device_bytes, factory_data_end=factory_data_end,
                                     library_payload_bytes=library_payload_bytes),
             'factory_data_end':factory_data_end,
             'library_payload_bytes': library_payload_bytes,
             'library_overhead_bytes': LIBRARY_OVERHEAD_MIB * 1024**2, 'complete': complete}
    if type(version) is not int or version not in (1,2):
        raise CommissionError('unsupported prepared-media version')
    if type(complete) is not bool:
        raise CommissionError('invalid prepared-media completion')
    if version == 2:
        del value['complete']
        value.update(schema_version=2, completion='COMPLETED' if complete else 'PREPARING')
    return validate(value, factory=factory, expected_artifact_sha256=artifact_sha256,
                    factory_data_end=factory_data_end)


def validate(value, *, factory: CommissionIdentity, expected_artifact_sha256: str,
             factory_data_end: int):
    """Validate exact fields against authenticated factory identity, not a label."""
    validate_geometry(value, factory=factory, factory_data_end=factory_data_end)
    if sha256(value['artifact_sha256']) != sha256(expected_artifact_sha256):
        raise CommissionError('prepared media differs from selected artifact')
    return value


def validate_geometry(value, *, factory, factory_data_end):
    """Target structural check against its fixed root identity and observed GPT.

    This does not authenticate the reported artifact digest. Signed artifact
    acquisition and exact byte verification belong to controller preparation.
    """
    if (not isinstance(value, dict) or type(value.get('schema_version')) is not int
            or value['schema_version'] not in (1,2)
            or set(value) != (FIELDS if value['schema_version']==1 else FIELDS_V2)
            or value['record_type'] != 'prepared-media'
            or (value['schema_version']==1 and type(value['complete']) is not bool)
            or (value['schema_version']==2 and value['completion'] not in ('PREPARING','COMPLETED'))):
        raise CommissionError('invalid prepared-media record')
    sha256(value['artifact_sha256'])
    if (value['factory_data_end'] != factory_data_end
            or type(value['factory_data_end']) is not int):
        raise CommissionError('prepared media differs from selected artifact')
    if (value['disk_guid'] != factory.disk_guid
            or value['partition_uuids'] != list(factory.partition_uuids)
            or type(value['library_overhead_bytes']) is not int
            or value['library_overhead_bytes'] != LIBRARY_OVERHEAD_MIB * 1024**2):
        raise CommissionError('prepared media differs from factory identity')
    geometry = value['geometry']
    if (not isinstance(geometry, list) or len(geometry) != 6
            or any(not isinstance(pair, list) or len(pair) != 2
                   or any(type(n) is not int for n in pair) for pair in geometry)
            or geometry != plan_layout(factory, value['device_bytes'],
                                       factory_data_end=factory_data_end, library_payload_bytes=value['library_payload_bytes'])):
        raise CommissionError('prepared geometry differs from capacity policy')
    return value


def confirmation(value):
    """Exact immutable plan reference; not sufficient alone to authorize writing."""
    return digest(canonical(value))


def is_complete(value):
    """Call only after strict version validation; historical bool stays frozen."""
    return value['complete'] if value['schema_version']==1 else value['completion']=='COMPLETED'


def completed(value):
    if value.get('schema_version') != 2 or value.get('completion') != 'PREPARING':
        raise CommissionError('completion publication requires preparing v2 media')
    return {**value, 'completion':'COMPLETED'}
