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
import time

import aiohttp

from .files import atomic_write, read_json
from .linux import Linux

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
    result = {'network': 'Not connected', 'ip': 'Not assigned', 'root': 'Unknown', 'space': 'Unavailable'}
    try:
        devices = subprocess.run(['nmcli', '-t', '-e', 'no', '-f', 'DEVICE,STATE', 'device'],
                                 capture_output=True, text=True, timeout=2, check=True)
        for line in devices.stdout.splitlines():
            device, state = line.split(':', 1)
            if state != 'connected' or device == 'lo':
                continue
            profile = subprocess.run(['nmcli', '-g', 'GENERAL.CONNECTION', 'device', 'show', device],
                                     capture_output=True, text=True, timeout=2, check=True)
            addresses = subprocess.run(['ip', '-j', 'address', 'show', 'dev', device],
                                       capture_output=True, text=True, timeout=2, check=True)
            ips = [a['local'] for d in json.loads(addresses.stdout) for a in d['addr_info']
                   if a.get('scope') == 'global']
            result.update(network=profile.stdout.strip() or device, ip=', '.join(ips) or 'Not assigned')
            break
    except (OSError, ValueError, subprocess.SubprocessError):
        result['network'] = 'Unavailable'
    try:
        entries = [line.split() for line in Path('/proc/self/mountinfo').read_text().splitlines()]
        root = next(e for e in entries if e[4] == '/')
        result['root'] = 'Read-only' if 'ro' in root[5].split(',') else 'Writable'
        if any(e[4] == str(data) for e in entries):
            result['space'] = f'{shutil.disk_usage(data).free / 1024**3:.1f} GiB'
        model = Path('/sys/class/dmi/id/product_name')
        result['model'] = model.read_text().strip() if model.exists() else 'Unknown'
    except (OSError, StopIteration):
        pass
    return result


class Console:
    def __init__(self, data=Path('/run/quirkbench-data'), linux=None, terminal=('/bin/bash',), facts=None):
        self.terminal = terminal
        self.data = data
        self.home = data / 'quirkbench'
        self.linux = linux or Linux()
        self.notice = ''
        self.selected = 0
        self.facts = facts or {'network': 'Checking...', 'ip': 'Checking...', 'root': 'Checking...', 'space': 'Checking...'}
        self.page = 'home'
        self.scroll = 0
        self.follow = True
        self.choice = 0

    def state(self):
        status = snapshot(self.home / 'screen.json')
        settings = snapshot(self.home / 'settings' / 'connection.json')
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
                  ('Controller URL', settings.get('url', 'Not configured')),
                  ('Recovery OS' if status.get('mode', 'recovery') == 'recovery' else 'Candidate OS', self.facts['root']),
                  ('USB free', self.facts['space']), ('Current work', current)]
        return fields, status, settings

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
        available = max(0, height-len(lines)-3)
        events = status.get('events', [])[-available:] if available else []
        for event in events:
            stamp = time.strftime('%H:%M:%S', time.localtime(event['time']))
            lines.append(row(f'{stamp}  {event["source"]:<8}  {event["message"]}'))
        while len(lines)<height-3:
            lines.append(row())
        lines += [border(), row(self.notice or '↑↓ Select  Enter Open  A Activity  T Terminal  Esc Back'), border('└','┘')]
        return lines[:height]

    def save_network(self):
        if snapshot(self.home / 'screen.json').get('mode') == 'candidate':
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
            self.notice = 'Network settings are temporary. Shared USB storage is unavailable.'

    async def report(self, upload=False):
        folder = self.home / 'reports' if self.home.exists() else Path('/run/quirkbench-reports')
        folder.mkdir(parents=True, exist_ok=True)
        name = 'recovery-' + str(time.time_ns())
        chunks = []
        for view in ('hardware', 'storage', 'network', 'boot-log'):
            result = await self.linux.inspect(view, 200)
            chunks.append(view + '\n' + result['output'])
        path = folder / (name + '.txt')
        text = '\n\n'.join(chunks)
        settings = read_json(self.home / 'settings' / 'connection.json', {})
        # Fixed diagnostics never request NetworkManager secrets or private key files.
        for secret in (settings.get('token'), settings.get('invitation')):
            if secret:
                text = text.replace(secret, '[removed]')
        atomic_write(path, text)
        if upload:
            if not settings.get('target_id'):
                raise ValueError(f'Pair with the controller before uploading. Local report: {path}')
            tls = ssl.create_default_context(cafile=self.home / 'settings' / 'controller.crt')
            async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=tls)) as client:
                with path.open('rb') as f:
                    async with client.put(settings['url'] + '/api/v1/reports/' + name, data=f,
                                          headers={'Authorization': 'Bearer ' + settings['token']}) as response:
                        response.raise_for_status()
            self.notice = f'Report uploaded. Local copy: {path}'
        else:
            self.notice = f'Report saved: {path}'
        return path

    def open_page(self, page):
        self.page, self.choice, self.scroll = page, 0, 0
        self.follow = True
        self.notice = ''

    def options(self):
        return {'logs': ['View boot logs', 'View Quirkbench service logs', 'Preview report', 'Save report', 'Send report to controller'],
                'power': ['Back', 'Restart into recovery', 'Shut down'],
                'confirm': ['Keep running', 'Confirm '+getattr(self, 'pending', '')]}.get(self.page, [])

    def page_lines(self):
        fields, status, settings = self.state()
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
            extra = ['Current work: '+fields[-1][1], 'This interrupts active work. Unconfirmed evidence stays on USB.', '']
        return [title, '', *extra, *[('> ' if i==self.choice else '  ')+label for i,label in enumerate(self.options())], '', self.notice]

    def child(self, screen, command):
        curses.def_prog_mode()
        curses.endwin()
        try:
            if command == self.terminal:
                print('Local root terminal. You can change connected disks. Type exit to return.', flush=True)
            subprocess.run(command, check=True)
        finally:
            curses.reset_prog_mode()
            screen.clear()

    def activate(self, key, screen):
        if key == 'n':
            self.child(screen, ['nmtui'])
            self.save_network()
        elif key == 't':
            self.child(screen, self.terminal)
        else:
            self.open_page({'a':'activity', 'l':'logs', 'i':'details', 'c':'controller', 'p':'power'}[key])

    def power(self, screen):
        env = self.data/'boot/grubenv'
        if self.pending == 'restart' and not env.is_file():
            raise ValueError('Boot settings are unavailable. Open terminal to inspect the USB, or choose Shut down.')
        was_running = subprocess.run(['systemctl','is-active','--quiet','quirkbench-target.service'],
                                     capture_output=True, check=False).returncode == 0
        try:
            # Stop deployment before it can select a candidate after our reset.
            self.child(screen, ['systemctl','stop','quirkbench-target.service'])
            if self.pending == 'restart':
                self.child(screen, ['grub2-editenv', str(env), 'unset','next_entry'])
            self.child(screen, ['systemctl','reboot' if self.pending=='restart' else 'poweroff'])
        except (OSError, subprocess.SubprocessError):
            if was_running:
                self.child(screen, ['systemctl','start','quirkbench-target.service'])
            raise

    def key(self, key, screen):
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
                else:
                    path = asyncio.run(self.report(upload=self.choice==4))
                    if self.choice==2:
                        self.child(screen, ['less', str(path)])
            elif self.page == 'power':
                if self.choice == 0:
                    self.open_page('home')
                else:
                    self.pending = 'restart' if self.choice==1 else 'shutdown'
                    self.open_page('confirm')
            elif self.page == 'confirm':
                if self.choice == 0:
                    self.open_page('home')
                else:
                    self.power(screen)

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
            body = self.page_lines()
            available = max(1,height-2)
            if self.options():
                selected = next(i for i,line in enumerate(body) if line.startswith('> '))
                self.scroll = min(self.scroll, selected)
                self.scroll = max(self.scroll, selected-available+1)
            if self.page=='activity' and self.follow:
                self.scroll=max(0,len(body)-available)
            self.scroll=min(self.scroll,max(0,len(body)-available))
            rows=body[self.scroll:self.scroll+available]
            rows+=['']*max(0,height-2-len(rows))
            rows += ['↑↓ Scroll/select  Enter Open  F Follow  Esc Back',self.notice]
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
        with ThreadPoolExecutor(max_workers=1) as workers:
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
            switched = False
            while True:
                if facts.done():
                    self.facts = facts.result()
                    if time.monotonic()>=next_check:
                        facts=workers.submit(local_facts,self.data)
                        next_check=time.monotonic()+3
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
