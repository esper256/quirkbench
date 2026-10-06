"""Small curses presentation over recovery facts and existing attended services."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
import io
import queue
import threading
import time
import textwrap

from .recovery_status import Facts, actions, recommendation, text

MENUS = {
    'home': ('network','controller','diagnostics','terminal','power'),
    'controller': ('pair','save_network','replay_network','endpoint','retarget','drain'),
    'diagnostics': ('logs','retry_checks','retry_network','reset_network','collect','export','upload'),
    'power': ('shutdown','restart','local_power'),
}


class AttendedIO(io.TextIOBase):
    """Bounded action text and one prompt; leaving its screen is not cancellation."""
    def __init__(self):
        self.lock=threading.Lock();self.answers=queue.Queue(maxsize=1)
        self.history='';self.prompting=False

    def write(self,value):
        with self.lock:
            self.history=(self.history+''.join(c for c in value if c.isprintable() or c in '\n\t'))[-65536:]
        return len(value)

    def flush(self): pass

    def readline(self,size=-1):
        with self.lock: self.prompting=True
        answer=self.answers.get()
        with self.lock: self.prompting=False
        return answer[:size] if size>=0 else answer

    def feed(self,value):
        try: self.answers.put_nowait(value)
        except queue.Full: pass

    def snapshot(self):
        with self.lock: return self.history,self.prompting


@dataclass
class Task:
    action: str
    label: str
    stream: AttendedIO = field(default_factory=AttendedIO)
    started: float = field(default_factory=time.monotonic)
    done: bool = False
    result: object = None
    error: str = ''
    retryable: bool = False
    finished: float = 0

    def run(self,dispatch):
        try: self.result=dispatch(self.action,self.stream)
        except Exception as exc:
            # Never expose private/native exception payloads to report collection.
            self.error=f'{self.label} failed ({type(exc).__name__}). Review logs and retry the retained operation.'
            from .transport import TransportError
            import errno, socket, ssl
            chain=[];cause=exc
            while cause is not None and len(chain)<8:
                chain.append(cause);cause=cause.__cause__
            self.retryable=(isinstance(exc,TransportError) and not any(isinstance(e,ssl.SSLError) for e in chain)
                and any(isinstance(e,(TimeoutError,ConnectionError,socket.gaierror))
                        or isinstance(e,OSError) and e.errno in (errno.ENETUNREACH,errno.EHOSTUNREACH,errno.ETIMEDOUT) for e in chain))
        finally: self.finished=time.monotonic();self.done=True


@dataclass
class Screen:
    facts: Facts = field(default_factory=Facts)
    view: str = 'home'
    focus: dict = field(default_factory=lambda:{name:rows[0] for name,rows in MENUS.items()})
    task: Task | None = None
    input: str = ''
    help: bool = False
    loading: bool = True
    observed: float = 0
    notice: str = ''
    scroll: int = 0

    def current(self):
        if self.observed and time.monotonic()-self.observed>5 and self.facts.controller=='connected':
            return replace(self.facts,controller='disconnected')
        return self.facts


def rows(state):
    available={a.id:a for a in actions(state.current())}
    from .recovery_status import Action
    available['logs']=Action('logs','View boot and recovery logs')
    return tuple(available[name] for name in MENUS.get(state.view, MENUS['home']))


def draw(window,state):
    """Rendering alone performs no service or filesystem operation."""
    import curses
    height,width=window.getmaxyx();window.erase()
    def line(y,value,attribute=0):
        if 0<=y<height:
            try: window.addnstr(y,2,text(value,max(0,width-4)),max(0,width-4),attribute)
            except curses.error: pass
    line(0,'QUIRKBENCH                                      Recovery',curses.A_BOLD)
    facts=state.current()
    if state.help:
        help_lines=[
            'Move with Up/Down. Enter opens the selected action. Esc returns.',
            'L opens logs. T opens the independent root terminal; exit returns.',
            'Network setup is temporary until explicitly remembered.',
            'Pairing never grants permission to run an experiment.',
            'During a slow action, Esc leaves progress without cancelling.',
            'Ctrl+T opens the terminal while entering a prompted answer.',
            'Reports held in RAM survive UI restart, but are lost on reboot.',
            'Manual root commands can modify internal disks.',
        ]
        for y,value in enumerate(help_lines,3): line(y,value)
        line(height-2,'? or Esc Close help    T Terminal')
    elif state.view == 'info':
        for y,value in enumerate(textwrap.wrap(state.notice,max(1,width-4)),3): line(y,value)
        line(height-2,'Esc Back   L Logs   T Terminal')
    elif state.view=='progress' and state.task is not None:
        task=state.task;history,prompting=task.stream.snapshot()
        title=task.error or (task.label+(' — finished' if task.done else f' — {time.monotonic()-task.started:.0f}s elapsed'))
        line(2,title,curses.A_BOLD)
        visible=[]
        for value in history.splitlines(): visible.extend(textwrap.wrap(value,max(1,width-4)) or [''])
        capacity=max(1,height-10)
        stop=max(0,len(visible)-state.scroll)
        for y,value in enumerate(visible[max(0,stop-capacity):stop],4): line(y,value)
        if prompting:
            prompt=history.rsplit('\n',1)[-1]
            hidden=any(word in prompt.lower() for word in ('password','one-use code','single-use code','secret','token'))
            line(height-5,'Answer: '+('*'*len(state.input) if hidden else state.input))
            line(height-3,'Enter Submit   Esc Back (action continues)   Ctrl+T Terminal')
        else: line(height-3,'Esc Back   L Logs   T Terminal   PgUp/PgDn Scroll')
    else:
        title,detail,recommended=recommendation(facts)
        if state.loading: title,detail='Checking recovery status','Networking, diagnostics and the terminal stay accessible.'
        line(2,title,curses.A_BOLD)
        for y,value in enumerate(textwrap.wrap(detail,max(1,width-4))[:2],4): line(y,value)
        labels=[('USB',facts.usb),('Network',facts.network),('Controller',facts.controller),('Computer',facts.target or facts.binding)]
        for y,(name,value) in enumerate(labels,7):line(y,f'{name:<14}{value}')
        if state.view!='home':line(12,{'controller':'Controller connection','diagnostics':'Troubleshooting','power':'Power'}[state.view],curses.A_BOLD)
        for y,action in enumerate(rows(state),14):
            selected=state.focus[state.view]==action.id
            label=('> ' if selected else '  ')+action.label+(' (unavailable)' if not action.enabled else '')
            line(y,label,curses.A_REVERSE if selected else 0)
        if state.task and not state.task.done:
            line(height-4,state.task.label+' is running. Enter its progress screen to answer prompts.')
        elif state.notice:line(height-4,state.notice)
        line(height-2,'Up/Down Move   Enter Open   ? Help   L Logs   T Terminal   Esc Back')
    window.noutrefresh();curses.doupdate()


def run(window, *, read_status, dispatch, network, terminal, present, initial=None):
    import curses
    state=initial or Screen();updates=queue.Queue(maxsize=1);stop=threading.Event()
    def sample():
        while not stop.is_set():
            try: value=read_status()
            except Exception:value=Facts(boot='failed',boot_detail='Status observations failed. Inspect logs or open the terminal.')
            try: updates.put_nowait(value)
            except queue.Full:
                try: updates.get_nowait()
                except queue.Empty: pass
                try: updates.put_nowait(value)
                except queue.Full: pass
            stop.wait(1)
    window.keypad(True);window.timeout(100)
    curses.set_escdelay(100)
    try: curses.curs_set(0)
    except curses.error: pass
    # Paint before either checks or initial pairing, including failed startup.
    draw(window,state);present()
    threading.Thread(target=sample,name='recovery-status',daemon=True).start()
    def start(name,label,show=True):
        if state.task is not None and not state.task.done:
            state.view='progress';state.notice='One action is already running.';return
        state.task=Task(name,label)
        if show:state.view='progress'
        state.input='';state.scroll=0
        threading.Thread(target=state.task.run,args=(dispatch,),name='recovery-action',daemon=True).start()
    auto_attempts=0
    try:
        while True:
            try:
                state.facts=updates.get_nowait();state.loading=False;state.observed=time.monotonic()
            except queue.Empty: pass
            eligible={a.id:a for a in actions(state.current())}
            retry=(state.task is not None and state.task.done and state.task.action=='pair'
                   and state.task.retryable and auto_attempts<3
                   and time.monotonic()-state.task.finished >= (2 if auto_attempts==1 else 5))
            if (not state.loading and eligible['pair'].enabled and
                    ((auto_attempts==0 and state.task is None) or retry)):
                auto_attempts+=1
                start('pair','Connecting to the prepared controller',show=auto_attempts==1 or state.view=='progress')
            draw(window,state)
            key=window.getch()
            if key<0:continue
            if key in (4,):return 0  # EOF requests only UI exit; target work has another owner.
            if key in (20,):terminal();continue
            if key==27:
                if state.help:state.help=False
                else:state.view='home'
                continue
            if state.view=='progress' and state.task:
                _,prompting=state.task.stream.snapshot()
                if prompting:
                    if key in (10,13,curses.KEY_ENTER):
                        state.task.stream.feed(state.input+'\n');state.input=''
                    elif key in (curses.KEY_BACKSPACE,127,8):state.input=state.input[:-1]
                    elif 32<=key<127 and len(state.input)<1024:state.input+=chr(key)
                    continue
                if key==curses.KEY_PPAGE:state.scroll+=max(1,window.getmaxyx()[0]-10)
                elif key==curses.KEY_NPAGE:state.scroll=max(0,state.scroll-max(1,window.getmaxyx()[0]-10))
            if key in (ord('t'),ord('T')):terminal();continue
            if key==ord('?'):state.help=not state.help;continue
            if key in (ord('l'),ord('L')):start('logs','Boot and recovery logs');continue
            if state.view not in MENUS:continue
            menu=rows(state);index=next(i for i,a in enumerate(menu) if a.id==state.focus[state.view])
            if key in (curses.KEY_DOWN,curses.KEY_UP):
                state.focus[state.view]=menu[(index+(1 if key==curses.KEY_DOWN else -1))%len(menu)].id
            elif key in (10,13,curses.KEY_ENTER):
                action=menu[index]
                if action.id in ('controller','diagnostics','power'):state.view=action.id;continue
                if action.id=='terminal':terminal();continue
                if not action.enabled:
                    state.notice=action.reason+' '+action.remedy;state.view='info';continue
                if action.id=='network':
                    if state.task and not state.task.done:
                        state.notice='An action is running. Leave its progress screen without duplicating it.';continue
                    curses.def_prog_mode();curses.endwin()
                    try:network()
                    except (OSError,ValueError,RuntimeError):state.notice='Network setup failed. Inspect NetworkManager logs and retry.'
                    finally:curses.reset_prog_mode();window.clearok(True)
                else:start(action.id,action.label)
    finally:
        stop.set()
        if state.task and not state.task.done:state.task.stream.feed('')
