"""Stage reusable entries privately; only the coordinator approves publication."""
import json
from pathlib import Path
from .build_cache import BuildStageCache
from .contracts import canonical
from .store import atomic_write


class DeferredCache(BuildStageCache):
    def __init__(self,root,proposals,*,limit=50*1024**3):
        super().__init__(Path(proposals)/'work-cache')
        self.hints=BuildStageCache(root)
        self.proposals=BuildStageCache(Path(proposals)/'entries')
        self.records=[]
        self.limit=limit
        atomic_write(Path(proposals)/'settings.json',canonical({'schema_version':1,'retention':{'cache_gib':limit//1024**3}}))

    def peek_latest(self,lineage,stage):
        return self.hints.peek_latest(lineage,stage)

    def load(self,lineage,stage,identity,destinations):
        entry=self.hints._slot(lineage,stage)/self.key(stage,identity)
        if not entry.exists(): return None
        return self.hints._restore(entry,stage,destinations,expected_identity=identity,exact_trees=True)['metadata']

    def load_latest(self,lineage,stage,destinations):
        from .build_cache import HASH
        slot=self.hints._slot(lineage,stage)
        if not slot.is_dir(): return None
        entries=[p for p in slot.iterdir() if p.is_dir() and not p.is_symlink() and HASH.fullmatch(p.name)]
        if not entries: return None
        if len(entries)!=1: raise ValueError('ambiguous approved cache generations')
        return self.hints._restore(entries[0],stage,destinations)

    def publish(self,lineage,stage,identity,trees,metadata):
        from .maintenance import tree_bytes
        incoming=sum(tree_bytes(path) for path in trees.values())+len(canonical(identity))+len(canonical(metadata))+4096
        if tree_bytes(self.hints.root)+tree_bytes(self.proposals.root)+incoming>self.limit: return None
        key=self.proposals.publish(lineage,stage,identity,trees,metadata)
        if key is not None:
            self.records.append({'lineage':lineage,'stage':stage,'key':key,'identity':identity})
            atomic_write(self.proposals.root/'proposals.json',canonical({'entries':self.records}))
        return key


def approve(root,stage,*,verify,inputs):
    from .job_worker import document
    proposals=Path(stage)/'cache-proposals/entries'
    if not (proposals/'proposals.json').exists(): return
    record=document(proposals,'proposals.json')
    from .build_pipeline import BuildPipeline,_experiment_kbuild_implementation
    expected={'source':inputs.kernel_source_sha256,'base_source':inputs.kernel_source_lineage_sha256 or inputs.kernel_source_sha256,
              'builder':inputs.base_image_digest,'toolchain':inputs.toolchain_lock_sha256,'build_rpms':inputs.build_rpm_lock_sha256,
              'epoch':inputs.source_date_epoch,'config':inputs.kernel_config_sha256,'implementation':_experiment_kbuild_implementation()}
    lineage_expected=BuildPipeline._incremental_lineage(None,inputs)
    if set(record)!={'entries'} or not isinstance(record['entries'],list) or len(record['entries'])>1:
        raise ValueError('invalid bounded cache proposals')
    cache=BuildStageCache(Path(root)/'intermediate-cache')
    staged=BuildStageCache(proposals)
    for entry in record['entries']:
        verify()
        lineage,phase,key=entry['lineage'],entry['stage'],entry['key']
        if lineage!=lineage_expected or phase!='experiment-kbuild' or entry['identity']!=expected or key!=cache.key(phase,entry['identity']): raise ValueError('cache proposal identity differs')
        path=staged._slot(lineage,phase)/key
        metadata=document(path,'manifest.json')
        if not isinstance(metadata.get('trees'),dict) or set(metadata['trees'])!={'source','objects'}:
            raise ValueError('cache proposal trees are not allowed')
        trees={name:path/name for name in metadata['trees']}
        staged._verify_entry(path,phase,key,entry['identity'],trees)
        with cache.lock(lineage):
            verify(); cache.publish(lineage,phase,entry['identity'],trees,metadata['metadata'])
