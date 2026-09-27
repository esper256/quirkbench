#!/usr/bin/env python3
"""Strict libostree verification of cached trusted, unsigned and untrusted commits.

Run in the isolated builder with OSTree, GnuPG and python3-gobject-base installed.
All keys are disposable fixtures generated under a new work directory.
"""
import argparse
import json
from pathlib import Path
import subprocess

from quirkbench.contracts import canonical
from quirkbench.ostree import STRICT_SIGNATURE_SCRIPT
from quirkbench.store import atomic_write


def run(*argv):
    return subprocess.check_output(list(map(str, argv)), text=True, stderr=subprocess.PIPE).strip()


def qualify(work):
    work = work.absolute()
    work.mkdir(parents=True, exist_ok=False)
    repo = work / 'repo'
    run('ostree', '--repo=' + str(repo), 'init', '--mode=archive')
    tree = work / 'tree'
    tree.mkdir()
    source = tree / 'fixture.txt'
    keys = {}
    for name in ('trusted', 'untrusted'):
        home = work / name
        home.mkdir(mode=0o700)
        run('gpg', '--batch', '--pinentry-mode', 'loopback', '--passphrase', '', '--homedir', home,
            '--quick-generate-key', 'Quirkbench disposable ' + name, 'rsa2048', 'sign', '1d')
        listing = run('gpg', '--batch', '--homedir', home, '--with-colons', '--list-keys')
        fingerprint = next(line.split(':')[9] for line in listing.splitlines() if line.startswith('fpr:'))
        keys[name] = (home, fingerprint)
    public_key = work / 'trusted.asc'
    public_key.write_text(run('gpg', '--batch', '--homedir', keys['trusted'][0], '--armor', '--export', keys['trusted'][1]))
    run('ostree', '--repo=' + str(repo), 'remote', 'add', '--gpg-import=' + str(public_key),
        '--set=gpg-verify=true', 'lab', 'https://localhost/lab/')
    outcomes = {}
    revisions = {}
    for case in ('unsigned', 'trusted', 'untrusted'):
        source.write_text(case + '\n')
        revision = run('ostree', '--repo=' + str(repo), 'commit', '--branch=fixture/' + case,
                       '--tree=dir=' + str(tree), '--subject=' + case)
        if case != 'unsigned':
            home, fingerprint = keys[case]
            run('ostree', '--repo=' + str(repo), 'gpg-sign', '--gpg-homedir=' + str(home), revision, fingerprint)
        result = subprocess.run(['python3', '-c', STRICT_SIGNATURE_SCRIPT, str(repo), revision, 'lab'],
                                text=True, capture_output=True)
        accepted = result.returncode == 0 and result.stdout.strip() == 'signature-valid'
        assert accepted == (case == 'trusted'), (case, result.returncode, result.stdout, result.stderr)
        outcomes[case] = 'accepted' if accepted else 'rejected'
        revisions[case] = revision
    # Demonstrate why the CLI display command is not an authorization boundary.
    display = subprocess.run(['ostree', '--repo=' + str(repo), 'show', '--gpg-verify-remote=lab', revisions['unsigned']],
                             text=True, capture_output=True)
    report = {'schema_version': 1, 'verification': outcomes,
              'unsigned_cli_display_exit': display.returncode, 'strict_signature_gate_passed': True}
    atomic_write(work / 'qualification.json', canonical(report))
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work', type=Path, required=True)
    print(json.dumps(qualify(parser.parse_args().work), indent=2))
