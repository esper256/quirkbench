"""Local recovery console. The human terminal is deliberately unrestricted."""
import asyncio
import curses
from pathlib import Path
import shutil
import ssl
import subprocess
import time

import aiohttp

from .files import atomic_write, read_json
from .linux import Linux


class Console:
    def __init__(self, data=Path('/run/quirkbench-data'), linux=None, terminal=('/bin/bash',)):
        self.terminal = terminal
        self.data = data
        self.home = data / 'quirkbench'
        self.linux = linux or Linux()
        self.notice = ''

    def lines(self):
        status = read_json(self.home / 'screen.json', {})
        settings = read_json(self.home / 'settings' / 'connection.json', {})
        mode = status.get('mode', 'recovery')
        return [f'Quirkbench · {mode.capitalize()} system', '',
                f'Target       {status.get("name", settings.get("name", "Not paired"))}',
                f'Controller   {settings.get("url", "No saved connection")}',
                f'Connection   {"Connected" if status.get("connected") else status.get("message", "Waiting for network")}',
                'Recovery OS  Read-only' if mode == 'recovery' else 'Next boot    Recovery', '',
                'Ready for your agent.' if status.get('connected') else 'Connect to a network to get started.',
                self.notice, '', 'N Network   C Connection   L Logs   R Save report',
                'U Upload report   T Terminal   P Power off']

    def save_network(self):
        if read_json(self.home / 'screen.json', {}).get('mode') == 'candidate':
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
            self.notice = 'Connected settings are temporary. Shared USB storage is unavailable.'

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

    def run(self, screen):
        curses.curs_set(0)
        screen.timeout(1000)
        while True:
            screen.erase()
            height, width = screen.getmaxyx()
            for row, line in enumerate(self.lines()[:height-1]):
                screen.addnstr(row, 0, line, max(0, width-1))
            screen.refresh()
            key = screen.getch()
            if key < 0:
                continue
            key = chr(key).lower() if key < 256 else ''
            command = {'n': ['nmtui'], 't': self.terminal, 'l': ['journalctl', '-b'],
                       'p': ['systemctl', 'poweroff']}.get(key)
            if key == 'c':
                self.notice = 'The saved controller address appears above. Use Terminal to inspect or edit settings.'
            elif command or key in ('r', 'u'):
                curses.def_prog_mode()
                curses.endwin()
                try:
                    if command:
                        subprocess.run(command, check=False)
                        if key == 'n':
                            self.save_network()
                    else:
                        asyncio.run(self.report(upload=key == 'u'))
                except (OSError, ValueError, aiohttp.ClientError) as e:
                    self.notice = str(e)
                finally:
                    curses.reset_prog_mode()
                    screen.clear()


def main():
    curses.wrapper(Console().run)


if __name__ == '__main__':
    main()
