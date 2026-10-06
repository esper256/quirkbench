"""Task-oriented CLI construction. Parsing never opens controller state."""
import argparse
import json
import sys
from pathlib import Path

FAMILIES={
 'setup':'Configure this controller', 'doctor':'Check requirements and explain problems',
 'status':'Show controller readiness and work needing attention', 'recovery':'Download recovery images and prepare a target USB',
 'target':'Pair and manage target computers',
 'investigation':'Manage a problem, its source workspace and findings',
 'experiment':'Prepare tests and follow their progress and results',
 'run':'Review and approve individual target runs', 'monitor':'Watch investigation progress',
 'admin':'Manage installation, connections, storage and backups', 'dev':'Build recovery images and maintain releases'}


class CommandParser(argparse.ArgumentParser):
    def error(self,message):
        if getattr(self,'machine',False):
            from .operations import operation_response
            print(json.dumps(operation_response(error={'code':'INVALID_INPUT','message':message,'retryable':False})))
            raise SystemExit(2)
        super().error(message)

    def parse_args(self,args=None,namespace=None):
        argv=list(sys.argv[1:] if args is None else args)
        for p in walk(self):p.machine='--json' in argv
        value=super().parse_args(argv,namespace)
        if getattr(value,'show_version',False):value.command='version';return value
        if not getattr(value,'command',None):self.error('choose a command; see quirkbench --help')
        if value.command in ('backup','restore'):
            positional,option=('destination','output') if value.command=='backup' else ('backup','input')
            if (getattr(value,positional) is None)==(getattr(value,option) is None):
                self.error('provide exactly one path, positional or --'+option)
            if getattr(value,option) is not None:setattr(value,positional,getattr(value,option))
        if value.command=='endpoint':
            requirements={'show':(), 'stage':('request_id','host','source_sha256'), 'renew':('request_id','host','source_sha256'),
                          'apply':('request_id','identity_sha256','fingerprint'), 'rollback':('request_id','switch_sha256')}
            if any(getattr(value,n,None) is None for n in requirements[value.action]):
                self.error(value.route+' requires '+', '.join('--'+n.replace('_','-') for n in requirements[value.action]))
            if value.public_certificate and value.json:self.error('--public-certificate cannot include --json')
        if value.command=='target':
            required={'retarget-code':('generation','new_name','new_uuid','request_id'),
                      'drain-approve':('file','request_id'),'drain-revoke':('grant',),
                      'poweroff':('request_id',),'poweroff-cancel':('request_id',)}.get(value.action,())
            for name in required:
                if getattr(value,name,None) is None:self.error(value.route+' requires --'+name.replace('_','-'))
        if value.route=='investigation source prepare' and value.source is not None:
            if not value.base_oid or not value.quiesced:self.error('local source preparation requires --base-oid and --quiesced')
            value.action='prepare-source'
        if value.command=='evidence':value.investigation=value.name
        return value


def walk(parser):
    yield parser
    for a in parser._actions:
        if isinstance(a,argparse._SubParsersAction):
            for child in a.choices.values():yield from walk(child)


def globals(parser):
    parser.add_argument('--state',type=Path,default=argparse.SUPPRESS,help='Use controller data at PATH',metavar='PATH')
    parser.add_argument('--json',action='store_true',default=argparse.SUPPRESS,help='Return structured output')


def action(root,path,description,*,command,internal_action=None):
    """Build one real canonical command path; internal service names are private."""
    node=root
    parts=path.split()
    for index,name in enumerate(parts):
        if not hasattr(node,'commands'):
            node.commands=node.add_subparsers(prog=node.prog,required=node is not root,metavar='COMMAND')
        if name not in node.commands.choices:
            desc=description if index==len(parts)-1 else FAMILIES.get(name,'Manage '+name.replace('-',' '))
            child=node.commands.add_parser(name,help=desc,description=desc,formatter_class=argparse.RawDescriptionHelpFormatter)
            globals(child)
            node=child
        else:node=node.commands.choices[name]
    node.set_defaults(command=command,action=internal_action,route=path)
    return node


def page(p):
    p.add_argument('--after',type=int,default=0,help='Continue after this returned cursor')
    p.add_argument('--limit',type=int,default=20,help='Maximum records in this page (1–100)')


def additions(root):
    p=action(root,'doctor','Check requirements for a selected workflow',command='product',internal_action='doctor')
    p.add_argument('--workflow',choices=('controller','build','vm'),default='controller',help='Workflow whose tools to inspect')
    for path,desc in [('investigation list','List investigations'),('investigation show','Describe one investigation'),
                      ('investigation evidence list','List evidence recorded for an investigation'),
                      ('target list','List registered target computers'),('run list','List target runs for an investigation'),
                      ('recovery show','Inspect one exact recovery image')]:
        p=action(root,path,desc,command='product',internal_action=path)
        if path not in ('investigation list','target list'):
            p.add_argument('name',help='Recovery image ID' if path=='recovery show' else 'Investigation name')
        if path.endswith(' list'):page(p)
    for name,desc in [('submit','Prepare a test from exact source; target execution requires later approval'),
                      ('status','Show preparation stage, blockers and the next useful action'),
                      ('logs','Read preparation events and bounded build logs'),
                      ('resume','Continue interrupted preparation after reconciliation'),
                      ('list','List submissions and their resulting experiments')]:
        p=action(root,'experiment '+name,desc,command='submission',internal_action=name)
        p.add_argument('name',help='Investigation name')
        if name!='list':p.add_argument('--request-id',required=True,help='Exact durable submission request ID')
        if name=='submit':p.add_argument('--file',required=True,type=Path,help='Experiment submission JSON; use workspace quiesced acknowledgement or baseline source')
        if name=='resume':p.add_argument('--resume-request-id',required=True,help='Durable identity for this continuation request')
        if name=='logs':page(p)
        if name=='list':
            p.add_argument('--after',default='',help='Continue after the opaque cursor returned by this list')
            p.add_argument('--limit',type=int,default=20,help='Maximum records in this page (1–100)')
        if name=='logs':
            p.add_argument('--stage',choices=('source','candidate','proposal','build','system'),help='Preparation stage; default active or blocked stage')
            p.add_argument('--selector',help='Exact log name from the returned list')
            p.add_argument('--offset',type=int,default=0,help='Starting byte in the selected log')
            p.add_argument('--length',type=int,default=16384,help='Maximum log bytes to read')
        if name in ('submit','resume'):p.add_argument('--reserve-gib',type=float,default=20,help='Free storage reserve in GiB')


def example(p):
    """Generate a syntactically complete example from this action's arguments."""
    words=[p.prog]
    if p.get_default('command') in ('backup','restore'):
        return p.prog+(' --output ./backup' if p.get_default('command')=='backup' else ' --input ./backup --state ./restored')
    for a in p._actions:
        if a.dest in ('help','state','json') or isinstance(a,argparse._SubParsersAction):continue
        if a.option_strings and not a.required:continue
        if not a.option_strings and a.nargs in ('?','*'):continue
        if a.option_strings:words.append(a.option_strings[0])
        if a.nargs==0:continue
        examples={'name':'first-fix','target':'target-01','experiment_id':'EXPERIMENT_ID','attempt_id':'RUN_ID',
                  'request_id':'request-001','resume_request_id':'resume-001','file':'input.json','output':'./output',
                  'problem':'problem.md','repository':'lab','operation_id':'OPERATION_ID','archive':'controller.tar.gz'}
        words.append(str(a.choices[0] if a.choices else examples.get(a.dest, '1' if a.type in (int,float) else a.dest.upper())))
    return ' '.join(words)


def parser():
    root=CommandParser(prog='quirkbench',usage='quirkbench [OPTIONS] COMMAND ...',
                       description='Quirkbench — investigate Linux hardware problems and test patches.',
                       formatter_class=argparse.RawDescriptionHelpFormatter)
    globals(root)
    root.add_argument('--version',dest='show_version',action='store_true',help='Show the running version and source')
    root.set_defaults(state=None,json=False,reserve_gib=20,repositories=None,show_version=False)
    # Real family builders call existing services through typed parsed namespaces.
    from . import cli_setup_commands,cli_status_commands,cli_monitor_commands,cli_investigation_commands
    from . import cli_experiment_commands,cli_run_commands,cli_target_commands,cli_recovery_commands,cli_admin_commands,cli_dev_commands
    for module in (cli_setup_commands,cli_status_commands,cli_monitor_commands,cli_investigation_commands,
                   cli_experiment_commands,cli_run_commands,cli_target_commands,cli_recovery_commands,cli_admin_commands,cli_dev_commands):
        module.build(root)
    additions(root)
    local=root.commands.choices['investigation'].commands.choices['source'].commands.choices['prepare']
    local.add_argument('--source',type=Path,help='Optional existing Git source for deliberate manual preparation')
    local.add_argument('--base-oid',help='Exact base Git object ID for --source')
    local.add_argument('--quiesced',action='store_true',help='Acknowledge all local source writers stopped')
    local.add_argument('--workspace',help='Explicit workspace identity for local source')
    local.add_argument('--allow-untracked',action='append',default=[],help='Explicit untracked path to capture from local source')
    for node in walk(root):
        if node is root:continue
        command=node.get_default('command'); selected=node.get_default('action')
        required=({'retarget-code':('generation','new_name','new_uuid','request_id'),'drain-approve':('file','request_id'),'drain-revoke':('grant',),'poweroff':('request_id',),'poweroff-cancel':('request_id',)}.get(selected,()) if command=='target' else
                  {'stage':('request_id','host','source_sha256'),'renew':('request_id','host','source_sha256'),'apply':('request_id','identity_sha256','fingerprint'),'rollback':('request_id','switch_sha256')}.get(selected,()) if command=='endpoint' else ())
        for option in node._actions:
            if option.dest in required:option.required=True
        if command in ('backup','restore'):
            node.epilog='Example\n  '+node.prog+(' --output ./backup' if command=='backup' else ' --input ./backup --state ./restored')
        elif command=='controller-reset':node.epilog='Example\n  '+node.prog+' --request-id fresh-start-1 --confirm-reset'
        else:node.epilog='Example\n  '+example(node)
        node.epilog+='\n\nCommands never prompt; provide required choices as arguments or input files.'
        if getattr(node,'commands',None):node.usage=node.prog+' [OPTIONS] COMMAND ...'
    # Routine actions precede diagnostics in family help.
    for family, order in {'investigation': ('start','list','show','status','brief','context','history','pause','resume'),
                          'experiment': ('submit','list','status','logs','show','resume'),
                          'run': ('list','show','approve','reject','resolve'),
                          'target': ('pair','list','show','inventory')}.items():
        group=root.commands.choices[family].commands
        rank={name:i for i,name in enumerate(order)}
        group._choices_actions.sort(key=lambda item:rank.get(item.dest,len(order)))
    # Hide argparse's inline enumeration and use the reviewed grouped overview.
    root.commands.help=argparse.SUPPRESS
    root.commands._choices_actions=[]
    groups=[('Getting started',('setup','doctor','status','recovery','target')),
            ('Investigating a problem',('investigation','experiment','run','monitor')),
            ('Specialist tools',('admin','dev'))]
    root.epilog='\n\n'.join(title+'\n'+'\n'.join('  '+n.ljust(16)+FAMILIES[n] for n in names) for title,names in groups)
    root.epilog+='\n\nExamples\n  quirkbench setup --help\n  quirkbench investigation start first-fix --target target-01 --problem problem.md\n  quirkbench investigation brief first-fix\n  quirkbench monitor first-fix\n\nUse quirkbench COMMAND --help for actions and examples.\nCommands never prompt; provide required choices as arguments or input files.'
    return root
