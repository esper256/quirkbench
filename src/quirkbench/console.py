"""Local recovery console. The human terminal is deliberately unrestricted."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import curses
import fcntl
import json
import os
from pathlib import Path
import shutil
import ssl
import subprocess
import termios
import textwrap
import time

import aiohttp

from .files import atomic_write, read_json
from .linux import Linux
from .recovery_runtime import EmergencyPower, network_facts
from .diagnostics import emergency_report, recovery_report

MENU = [('n', 'Network'), ('c', 'Controller'), ('a', 'Activity'),
        ('l', 'Logs & reports'), ('i', 'Target details'), ('t', 'Open terminal'), ('p', 'Power')]


def text(value):
    # Terminal output must not interpret control characters from SSIDs or logs.
    return ''.join(c if c.isprintable() else ' ' for c in str(value))


def snapshot(path):
    try:
        return read_json(path, {})
    except (OSError, ValueError):
        return {}


def local_facts(data):
    """Read host facts off the UI thread; no writes, mounts or network probes."""
    result = {'root': 'Unknown', 'space': 'Unavailable'}
    try:
        entries = [line.split() for line in Path('/proc/self/mountinfo').read_text().splitlines()]
        root = next(e for e in entries if e[4] == '/')
        result['root'] = 'Read-only' if 'ro' in root[5].split(',') else 'Writable'
        if any(e[4].replace(r'\040', ' ').replace(r'\011', '\t').replace(r'\012', '\n').replace(r'\134', '\\') == str(data) for e in entries):
            result['space'] = f'{shutil.disk_usage(data).free / 1024**3:.1f} GiB'
        model = Path('/sys/class/dmi/id/product_name')
        result['model'] = model.read_text().strip() if model.exists() else 'Unknown'
    except (OSError, StopIteration):
        pass
    return result


class Console:
    def __init__(self, data=Path('/run/quirkbench-data'), linux=None, terminal=('/bin/bash', '-i'), facts=None):
        self.terminal = terminal
        self.data = data
        self.home = data / 'quirkbench'
        # A sibling of the data mount lives in /run on the target, not on USB.
        self.screen = data.parent / 'quirkbench-runtime' / 'screen.json'
        self.linux = linux or Linux()
        self.notice = ''
        self.selected = 0
        self.facts = facts or {'network': 'Checking...', 'ip': 'Checking...', 'root': 'Checking...', 'space': 'Checking...'}
        self.page = 'home'
        self.scroll = 0
        self.follow = True
        self.choice = 0
        self.report_task = None
        self.local_progress = None
        self.power_task = None
        self.power_action = None
        self.power_error = ''
        self.emergency = EmergencyPower()
        self.network_save = None
        self.emergency_report_task = None

    def state(self):
        status = snapshot(self.screen)
        # The target publishes public identity/URL into /run. Never touch the
        # removable drive while drawing or handling keys.
        settings = {key: status.get(key) for key in ('name', 'target_id', 'url')}
        stale = time.time() - status.get('updated', 0) > 5
        connected = status.get('connected', False) and not stale
        last = status.get('last_contact')
        current = status.get('current_work', 'Waiting for controller')
        if stale and status:
            current = 'Target service not responding'
        name = status.get('name') or settings.get('name') or 'Not labelled'
        tid = status.get('target_id') or settings.get('target_id')
        fields = [('Target', name + (f' ({tid[-6:]})' if tid else '')),
                  ('Network', self.facts['network']), ('Target IP', self.facts['ip']),
                  ('Controller', 'Connected' if connected else 'Disconnected'),
                  ('Last contact', f'{max(0, int(time.time()-last))}s ago' if last else 'Never'),
                  ('Controller URL', settings.get('url') or 'Not configured'),
                  ('Recovery OS' if status.get('mode', 'recovery') == 'recovery' else 'Candidate OS', self.facts['root']),
                  ('Data free', self.facts['space']), ('Current work', current)]
        return fields, status, settings

    def progress_line(self, status):
        stale = time.time()-status.get('updated', 0)>5
        progress = self.local_progress or (status.get('progress') if not stale else None)
        if not progress:
            return '[------------] '+('Waiting for current status' if stale else 'No operation running')
        # Moving blocks mean activity, not an invented completion percentage.
        position = int(time.monotonic()*4) % 18
        position = min(position, 18-position)
        bar = ('-'*position+'###').ljust(12, '-')
        elapsed = max(0, int(time.time()-progress['started']))
        return f'[{bar}] {progress["label"]} / {elapsed}s'

    def lines(self, width=80, height=24):
        fields, status, _ = self.state()
        width = max(20, width)
        inner = width - 2
        def row(value=''):
            return '│' + (' '+text(value))[:inner].ljust(inner) + '│'
        def border(left='├', right='┤'):
            return left+'─'*inner+right
        mode = status.get('mode', 'recovery').upper()
        lines = [border('┌', '┐'), row('QUIRKBENCH / '+mode)]
        if width >= 76 and height >= 24:
            left = 24
            right = inner-left-1
            def pair(a, b):
                return '│'+(' '+text(a))[:left].ljust(left)+'│'+(' '+text(b))[:right].ljust(right)+'│'
            lines += ['├'+'─'*left+'┬'+'─'*right+'┤', pair('MENU', 'CURRENT STATUS')]
            for i, (label, value) in enumerate(fields):
                item = ('> ' if i == self.selected else '  ') + MENU[i][0].upper()+'  '+MENU[i][1] if i<len(MENU) else ''
                lines.append(pair(item, f'{label:<16}{text(value)}'))
            lines += ['├'+'─'*left+'┴'+'─'*right+'┤']
        else:
            lines += [border()]
            for i,(key,label) in enumerate(MENU):
                lines.append(row(('> ' if i==self.selected else '  ')+key.upper()+' '+label))
            lines += [row('Target: '+fields[0][1]), row('Controller: '+fields[3][1]), row('Work: '+fields[-1][1]), border()]
        lines.append(row('RECOVERY & MCP ACTIVITY'))
        available = max(0, height-len(lines)-5)
        events = status.get('events', [])[-available:] if available else []
        for event in events:
            stamp = time.strftime('%H:%M:%S', time.localtime(event['time']))
            lines.append(row(f'{stamp}  {event["source"]:<8}  {event["message"]}'))
        while len(lines)<height-5:
            lines.append(row())
        lines += [border(), row(self.progress_line(status)), border(),
                  row(self.notice or '↑↓ Select  Enter Open  A Activity  T Terminal  Esc Back'), border('└','┘')]
        return lines[:height]

    def save_network(self):
        if snapshot(self.screen).get('mode') == 'candidate':
            self.notice = 'Network changes apply to this run only.'
            return
        source = Path('/run/NetworkManager/system-connections')
        destination = self.home / 'settings' / 'network'
        try:
            destination.mkdir(parents=True, exist_ok=True)
            for profile in source.glob('*'):
                if profile.is_file():
                    atomic_write(destination / profile.name, profile.read_bytes())
            self.notice = 'Network settings saved for future boots.'
        except OSError:
            self.notice = 'Network settings are temporary. Data storage is unavailable.'

    async def report(self, upload=False, basic=False):
        folder = self.screen.parent / 'reports' if basic else self.home / 'reports'
        folder.mkdir(parents=True, exist_ok=True)
        name = 'recovery-' + str(time.time_ns())
        path = folder / (name + '.txt')
        text = emergency_report() if basic else await recovery_report(self.linux, self.data, snapshot(self.screen))
        settings = read_json(self.screen.parent / 'report-connection.json', {})
        # Fixed diagnostics never request NetworkManager secrets or private key files.
        for secret in (settings.get('token'), settings.get('invitation')):
            if secret:
                text = text.replace(secret, '[removed]')
        atomic_write(path, text)
        if upload:
            if not settings.get('target_id'):
                raise ValueError(f'Pair with the controller before uploading. Local report: {path}')
            tls = ssl.create_default_context(cafile=self.screen.parent / 'controller.crt')
            async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=tls), timeout=aiohttp.ClientTimeout(total=30)) as client:
                with path.open('rb') as f:
                    # Explicit type avoids reading the OS MIME database after drive failure.
                    payload = aiohttp.payload.BufferedReaderPayload(f, content_type='text/plain; charset=utf-8')
                    async with client.put(settings['url'] + '/api/v1/reports/' + name, data=payload,
                                          headers={'Authorization': 'Bearer ' + settings['token']}) as response:
                        response.raise_for_status()
                        if not (await response.json()).get('stored'):
                            raise ValueError('Controller did not confirm report storage.')
            self.notice = f'Report uploaded. Local copy: {path}'
        else:
            self.notice = f'Report saved{" in RAM until power off" if basic else ""}: {path}'
        return path

    def open_page(self, page):
        self.page, self.choice, self.scroll = page, 0, 0
        self.follow = True
        self.notice = ''

    def options(self):
        reboot = 'Reboot into recovery' if self.state()[1].get('mode') == 'candidate' else 'Reboot'
        return {'logs': ['View boot logs', 'View Quirkbench service logs', 'Preview report', 'Save report', 'Send report to controller', 'Send emergency report'],
                'power': ['Back', reboot, 'Shut down', 'Force reboot', 'Force power off'],
                'emergency-confirm': ['Cancel', 'Force reboot now' if getattr(self, 'pending', '')=='restart' else 'Force power off now'],
                'confirm': ['Cancel', 'Reboot now' if getattr(self, 'pending', '')=='restart' else 'Shut down now']}.get(self.page, [])

    def page_lines(self, width=80):
        fields, status, settings = self.state()
        if self.page == 'emergency-confirm':
            return ['EMERGENCY POWER', '', 'Unsaved data may be lost. This skips shutdown and storage cleanup.',
                    'Force reboot cannot change the next boot selection.',
                    'If the kernel cannot respond, use the physical power button.', self.emergency.error, '',
                    *[('> ' if i==self.choice else '  ')+label for i,label in enumerate(self.options())]]
        if self.page == 'power-progress':
            return ['REBOOTING' if self.power_action=='restart' else 'SHUTTING DOWN', '',
                    self.local_progress['label'], '',
                    'Wait until the computer has powered off before disconnecting the drive.'
                    if self.power_action=='shutdown' else 'Keep the drive connected while the computer reboots.', '',
                    'F Force power action   T Open terminal']
        if self.page == 'power-error':
            return ['POWER ACTION FAILED', '', *textwrap.wrap(self.power_error, max(20,width-2)), '',
                    'F Force power action   Esc Back   T Open terminal']
        if self.page == 'activity':
            rows = ['ACTIVITY / '+('Following' if self.follow else 'Scroll paused'), '']
            for event in status.get('events', []):
                stamp = time.strftime('%H:%M:%S', time.localtime(event['time']))
                rows += [f'{stamp}  {event["source"]}  {event["message"]}']
                if event.get('detail'):
                    rows += ['  '+line for line in event['detail'].splitlines()]
            return rows
        if self.page == 'details':
            return ['TARGET DETAILS', '', *[f'{k:<16}{v}' for k,v in fields],
                    'Model           '+self.facts.get('model', 'Unknown'),
                    'Full target ID  '+str(status.get('target_id') or settings.get('target_id') or 'Not paired'),
                    'Boot ID         '+str(status.get('boot_id', 'Unknown'))]
        if self.page == 'controller':
            return ['CONTROLLER', '', *[f'{k:<16}{v}' for k,v in fields[3:6]], '',
                    'Reconnection is automatic.',
                    'To change saved connection settings, use Open terminal.',
                    'Settings: '+str(self.home / 'settings'), '', status.get('message', '')]
        title = {'logs':'LOGS & REPORTS', 'power':'POWER', 'confirm':'CONFIRM'}.get(self.page, self.page.upper())
        extra = []
        if self.page == 'confirm':
            extra = ['Current work: '+fields[-1][1], 'This interrupts active work. Unconfirmed evidence stays on the drive.', '']
        return [title, '', *extra, *[('> ' if i==self.choice else '  ')+label for i,label in enumerate(self.options())], '', self.notice]

    def child(self, screen, command):
        curses.def_prog_mode()
        curses.endwin()
        try:
            if command == self.terminal:
                print('Local root terminal. You can change connected disks. Type exit to return.', flush=True)
            # Interactive programs need their errors/prompts on this terminal,
            # even though the console service itself logs errors to the journal.
            try:
                result = subprocess.run(command, stderr=subprocess.STDOUT, check=False)
            except OSError as error:
                print(f'Could not start {command[0]}: {error}\nPress Enter to return.', flush=True)
                input()
                raise
            if result.returncode:
                print(f'{command[0]} exited with status {result.returncode}. Press Enter to return.', flush=True)
                input()
        finally:
            curses.reset_prog_mode()
            screen.clear()

    def activate(self, key, screen):
        if key == 'n':
            self.child(screen, ['nmtui'])
            if not self.network_save or self.network_save.done():
                self.network_save = self.workers.submit(self.save_network)
                self.notice = 'Network changes apply now; saving for future boots.'
        elif key == 't':
            self.child(screen, self.terminal)
        else:
            self.open_page({'a':'activity', 'l':'logs', 'i':'details', 'c':'controller', 'p':'power'}[key])

    def power_command(self, command, check=True):
        result = subprocess.run(command, capture_output=True, text=True, timeout=120)
        if check and result.returncode:
            raise RuntimeError((result.stderr or result.stdout).strip() or f'{command[0]} failed ({result.returncode}).')
        return result

    def power(self, action):
        def stage(label):
            self.local_progress = {'label': label, 'started': time.time()}
        env = self.data/'boot/grubenv'
        if action == 'restart' and not env.is_file():
            raise ValueError('Boot settings are unavailable. Open terminal to inspect the drive, or choose Shut down.')
        was_running = self.power_command(['systemctl','is-active','--quiet','quirkbench-target.service'], check=False).returncode == 0
        try:
            # Stop deployment before it can select a candidate after our reset.
            stage('Stopping target work')
            self.power_command(['systemctl','stop','quirkbench-target.service'])
            if action == 'restart':
                stage('Selecting recovery for the next boot')
                self.power_command(['grub2-editenv', str(env), 'unset','next_entry'])
            stage('Requesting reboot' if action=='restart' else 'Requesting shutdown')
            self.power_command(['systemctl','--no-block','reboot' if action=='restart' else 'poweroff'])
            stage('Reboot requested; waiting for the system' if action=='restart' else 'Shutdown requested; waiting for the system')
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            if was_running:
                stage('Restoring the controller connection after failure')
                try:
                    self.power_command(['systemctl','start','quirkbench-target.service'])
                except (OSError, RuntimeError, subprocess.SubprocessError) as restore_error:
                    raise RuntimeError(f'{error} Controller connection could not restart: {restore_error}') from error
            raise

    def start_power(self):
        if self.power_action:
            self.page = 'power-progress'
            return
        self.power_action = self.pending
        self.local_progress = {'label': 'Preparing power action', 'started': time.time()}
        self.open_page('power-progress')
        # A separate lane: reports or storage checks cannot queue power behind them.
        self.power_task = self.power_workers.submit(self.power, self.power_action)

    def update_power(self):
        if self.power_task and self.power_task.done():
            try:
                self.power_task.result()
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                self.power_error = text(error)
                self.pending = self.power_action
                self.power_action = self.local_progress = None
                if self.page != 'emergency-confirm':
                    self.open_page('power-error')
            finally:
                self.power_task = None

    def key(self, key, screen):
        if self.page == 'emergency-confirm':
            if key == 27:
                self.open_page('power-progress' if self.power_action else 'power')
            elif key in (curses.KEY_UP, curses.KEY_DOWN):
                self.choice = 1 - self.choice
            elif key in (10, 13, curses.KEY_ENTER):
                if self.choice == 0:
                    self.open_page('power-progress' if self.power_action else 'power')
                else:
                    try:
                        self.emergency.trigger(self.pending)
                        self.notice = 'Emergency power requested. If the computer does not respond, use its physical power button.'
                    except OSError as error:
                        self.notice = str(error)
            return
        if self.power_action or self.page=='power-error':
            if key in (ord('f'), ord('F')):
                self.pending = self.power_action or self.pending
                self.open_page('emergency-confirm')
            elif key in (ord('t'), ord('T')):
                self.child(screen, self.terminal)
            elif key==27 and not self.power_action:
                self.open_page('home')
            return
        if key == 27:
            self.open_page('home')
        elif self.page == 'home':
            if key in (curses.KEY_DOWN, curses.KEY_UP):
                self.selected = (self.selected + (1 if key==curses.KEY_DOWN else -1)) % len(MENU)
            elif key in (10, 13, curses.KEY_ENTER):
                self.activate(MENU[self.selected][0], screen)
            elif 0<=key<256 and chr(key).lower() in dict(MENU):
                letter = chr(key).lower()
                self.selected = next(i for i,(k,_) in enumerate(MENU) if k==letter)
                self.activate(letter, screen)
        elif key in (curses.KEY_DOWN, curses.KEY_UP, curses.KEY_NPAGE, curses.KEY_PPAGE):
            step = 1 if key in (curses.KEY_DOWN,curses.KEY_NPAGE) else -1
            if self.options():
                self.choice = (self.choice+step) % len(self.options())
            else:
                self.follow = False
                self.scroll = max(0, self.scroll+step*(8 if key in (curses.KEY_NPAGE,curses.KEY_PPAGE) else 1))
        elif self.page == 'activity' and key in (ord('f'), ord('F')):
            self.follow = True
        elif key in (10,13,curses.KEY_ENTER):
            if self.page == 'logs':
                if self.choice < 2:
                    command = ['journalctl','-b']
                    if self.choice == 1:
                        command += ['-u','quirkbench-target','-u','quirkbench-prepare','-u','quirkbench-console']
                    self.child(screen, command)
                elif self.choice == 5:
                    if not self.emergency_report_task or self.emergency_report_task.done():
                        self.emergency_report_task = self.emergency_workers.submit(lambda: asyncio.run(self.report(upload=True, basic=True)))
                        self.notice = 'Sending emergency report; saved in RAM until power off.'
                else:
                    if self.report_task and not self.report_task.done():
                        self.notice = 'A report is already in progress.'
                        return
                    self.report_preview = self.choice==2
                    upload = self.choice in (4, 5)
                    self.local_progress = {'label': 'Collecting and uploading report' if upload else 'Collecting report', 'started': time.time()}
                    self.report_task = self.workers.submit(lambda: asyncio.run(self.report(upload=upload)))
            elif self.page == 'power':
                if self.choice == 0:
                    self.open_page('home')
                elif self.choice in (3, 4):
                    self.pending = 'restart' if self.choice == 3 else 'shutdown'
                    self.open_page('emergency-confirm')
                else:
                    self.pending = 'restart' if self.choice==1 else 'shutdown'
                    status = self.state()[1]
                    if status.get('progress') or status.get('mode')=='candidate' or self.report_task or time.time()-status.get('updated',0)>5:
                        self.open_page('confirm')
                    else:
                        self.start_power()
            elif self.page == 'confirm':
                if self.choice == 0:
                    self.open_page('home')
                else:
                    self.start_power()

    def draw(self, screen):
        screen.erase()
        height, width = screen.getmaxyx()
        if self.page == 'home':
            rows = self.lines(width, max(height, 18))
            if height < 18:
                selected = next((i for i,l in enumerate(rows) if '> ' in l[:25]), 0)
                start = max(0, selected-max(0,height-3))
                rows = rows[start:start+max(1,height-1)]+['↑↓ Select  Enter Open  Esc Back']
        else:
            body = self.page_lines(width)
            available = max(1,height-4)
            if self.options():
                selected = next(i for i,line in enumerate(body) if line.startswith('> '))
                self.scroll = min(self.scroll, selected)
                self.scroll = max(self.scroll, selected-available+1)
            if self.page=='activity' and self.follow:
                self.scroll=max(0,len(body)-available)
            self.scroll=min(self.scroll,max(0,len(body)-available))
            rows=body[self.scroll:self.scroll+available]
            rows+=['']*max(0,height-4-len(rows))
            help_line = 'F Force power action  T Open terminal' if self.power_action else '↑↓ Scroll/select  Enter Open  F Follow  Esc Back'
            if self.page == 'power-error':
                help_line = 'F Force power action  Esc Back  T Open terminal'
            rows += ['─'*width, self.progress_line(self.state()[1]), help_line,self.notice]
        for y,line in enumerate(rows[:height]):
            color = 1
            if line.startswith(('┌','├','└')):
                color = 2
            elif 'QUIRKBENCH /' in line or 'CURRENT STATUS' in line or 'RECOVERY & MCP' in line:
                color = 3
            try:
                screen.addnstr(y,0,text(line),max(0,width),curses.color_pair(color))
                if self.page=='home' and width>=76 and height>=24 and 4<=y<13:
                    screen.addnstr(y,27,text(line[27:43]),16,curses.color_pair(5))
                # Highlight the menu alone, never the adjacent status value.
                if self.page=='home' and '> ' in line[:25]:
                    start=line.index('> ')
                    screen.addnstr(y,start,text(line[start:24]),max(0,min(24-start,width-start-1)),curses.color_pair(4))
                elif line.startswith('> '):
                    screen.addnstr(y,0,text(line),max(0,width-1),curses.color_pair(4))
            except curses.error:
                pass  # Resizes can arrive between getmaxyx() and a draw.
        screen.refresh()

    def run(self, screen):
        with ThreadPoolExecutor(max_workers=3) as workers, ThreadPoolExecutor(max_workers=1) as power_workers, ThreadPoolExecutor(max_workers=1) as emergency_workers:
            self.workers = workers
            self.power_workers = power_workers
            self.emergency_workers = emergency_workers
            try:
                curses.curs_set(0)
            except curses.error:
                pass
            screen.keypad(True)
            screen.timeout(200)
            if curses.has_colors():
                curses.start_color()
                if curses.can_change_color():
                    for number,rgb in [(0,(78,110,98)),(7,(850,870,830)),(2,(714,788,604)),(6,(298,408,353)),(4,(565,639,580))]:
                        curses.init_color(number,*rgb)
                for n,fg,bg in [(1,7,0),(2,6,0),(3,2,0),(4,0,2),(5,4,0)]:
                    curses.init_pair(n,fg,bg)
                screen.bkgd(' ',curses.color_pair(1))
            facts = workers.submit(local_facts,self.data)
            next_check = time.monotonic()+3
            self.emergency.prepare()
            self.facts.update(network_facts())
            next_network = 0
            switched = False
            while True:
                self.update_power()
                for name in ('network_save', 'emergency_report_task'):
                    task = getattr(self, name)
                    if task and task.done():
                        try:
                            task.result()
                        except (OSError, ValueError, aiohttp.ClientError, TimeoutError) as error:
                            self.notice = text(error)
                        setattr(self, name, None)
                if self.report_task and self.report_task.done():
                    try:
                        path = self.report_task.result()
                        if self.report_preview and not self.power_action:
                            self.child(screen, ['less', str(path)])
                    except (OSError, ValueError, aiohttp.ClientError, subprocess.SubprocessError) as e:
                        self.notice = text(e)
                    finally:
                        self.report_task = None
                        if not self.power_action:
                            self.local_progress = None
                if facts.done():
                    self.facts.update(facts.result())
                    if time.monotonic()>=next_check:
                        facts=workers.submit(local_facts,self.data)
                        next_check=time.monotonic()+3
                if time.monotonic() >= next_network:
                    self.facts.update(network_facts())
                    next_network = time.monotonic() + 3
                if not facts.done() and time.monotonic() > next_check + 5:
                    self.facts['space'] = 'Unavailable (check stalled)'
                self.draw(screen)
                if not switched:
                    # Only the packaged tty2 console claims the display, once drawn.
                    if os.isatty(0) and os.ttyname(0)=='/dev/tty2':
                        # Linux TIOCL_SETKMSGREDIRECT: printk otherwise follows
                        # the active VT, even with console=tty1 on the command line.
                        fcntl.ioctl(0,termios.TIOCLINUX,bytes([11,1]))
                        subprocess.run(['chvt','2'],check=False,timeout=3)
                    switched=True
                try:
                    self.key(screen.getch(), screen)
                except (OSError, ValueError, aiohttp.ClientError, subprocess.SubprocessError) as e:
                    self.notice = text(e)


def main():
    curses.wrapper(Console().run)


if __name__ == '__main__':
    main()
