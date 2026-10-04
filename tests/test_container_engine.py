"""Shared containment proof rejects uncertain or substituted engine state."""
import json
import pytest
from quirkbench.build import BuildError
from quirkbench.container_engine import ContainerEngine

ID='a'*64
IMAGE='sha256:'+'b'*64


def container():
    return {'Id':ID,'Image':IMAGE,'Config':{'Labels':{'owner':'run'}},
            'HostConfig':{'RestartPolicy':{'Name':'no'},'Memory':4096,'MemorySwap':4096,
                          'NanoCpus':2*10**9,'PidsLimit':4096,'PidMode':'private','Privileged':False},
            'State':{'Running':True,'Pid':42,'Restarting':False}}


@pytest.mark.parametrize('failure',['identity','label','image','pid','restarting','restart-policy'])
def test_stop_rechecks_exact_identity_and_whole_container(failure):
    value=container();calls=[]
    def run(argv,**kwargs):
        calls.append(argv)
        if argv[1]=='inspect':return json.dumps([value])
        assert argv==['docker','stop','--time','10',ID]
        value['State'].update(Running=False,Pid=0)
        if failure=='identity':value['Id']='c'*64
        if failure=='label':value['Config']['Labels']['owner']='other'
        if failure=='image':value['Image']='sha256:'+'c'*64
        if failure=='pid':value['State']['Pid']=43
        if failure=='restarting':value['State']['Restarting']=True
        if failure=='restart-policy':value['HostConfig']['RestartPolicy']['Name']='always'
        return ''
    with pytest.raises(BuildError,match='identity|shutdown'):
        ContainerEngine('docker',runner=run).stop('name',IMAGE,'owner','run',ID)
    assert len(calls)==3 and calls[-1]==['docker','inspect',ID]


@pytest.mark.parametrize('field,value',[('Memory',0),('MemorySwap',-1),('NanoCpus',0),
    ('PidsLimit',-1),('PidMode','host'),('Privileged',True),('RestartPolicy',{'Name':'always'})])
def test_requested_limits_must_be_applied(field,value):
    backend=ContainerEngine('docker');observed=container()
    backend.validate_limits(observed,2,4096)
    observed['HostConfig'][field]=value
    with pytest.raises(BuildError,match='containment'):backend.validate_limits(observed,2,4096)


@pytest.mark.parametrize('reply',['[]','{}','[{},{}]','[null]','not json'])
def test_inspection_never_accepts_ambiguous_responses(reply):
    backend=ContainerEngine('docker',runner=lambda *a,**k:reply)
    with pytest.raises(BuildError):backend.inspect(ID)
