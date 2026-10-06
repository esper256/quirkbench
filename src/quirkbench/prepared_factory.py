"""Explicit factory-layout v3; historical commissioning v2 keeps its meaning."""
from dataclasses import dataclass
from pathlib import Path

from .commission import BootIdentity, CommissionError, validate_factory_geometry
from .enrollment_records import _document
from .filesystem import read_file

FIELDS = {'schema_version', 'record_type', 'disk_guid', 'partition_uuids',
          'partition_starts', 'fixed_ends', 'factory_data_end', 'library_payload_bytes'}


@dataclass(frozen=True)
class PreparedFactoryIdentity(BootIdentity):
    partition_starts: tuple[int, int, int, int]
    fixed_ends: tuple[int, int, int]
    factory_data_end: int
    library_payload_bytes: int = 0

    def __post_init__(self):
        super().__post_init__()
        if not isinstance(self.partition_starts, (list, tuple)) or not isinstance(self.fixed_ends, (list, tuple)):
            raise CommissionError('invalid prepared factory geometry')
        validate_factory_geometry(self.partition_starts, self.fixed_ends)
        object.__setattr__(self, 'partition_starts', tuple(self.partition_starts))
        object.__setattr__(self, 'fixed_ends', tuple(self.fixed_ends))
        if (type(self.factory_data_end) is not int or self.factory_data_end < self.partition_starts[3]
                or type(self.library_payload_bytes) is not int or self.library_payload_bytes != 0):
            raise CommissionError('invalid prepared factory extent or unsupported library payload')


def validate(value):
    if (not isinstance(value, dict) or set(value) != FIELDS
            or type(value['schema_version']) is not int or value['schema_version'] != 3
            or value['record_type'] != 'factory-layout'):
        raise CommissionError('invalid controller-prepared factory identity')
    return PreparedFactoryIdentity(*(value[field] for field in
        ('disk_guid', 'partition_uuids', 'partition_starts', 'fixed_ends',
         'factory_data_end', 'library_payload_bytes')))


def record(disk_guid, partition_uuids, partitions):
    value = {'schema_version':3, 'record_type':'factory-layout', 'disk_guid':disk_guid,
             'partition_uuids':list(partition_uuids),
             'partition_starts':[part['start'] for part in partitions],
             'fixed_ends':[part['end'] for part in partitions[:3]],
             'factory_data_end':partitions[3]['end'], 'library_payload_bytes':0}
    validate(value)
    return value


def load(path: Path):
    return validate(_document(read_file(path.parent, path.name, limit=65536)))
