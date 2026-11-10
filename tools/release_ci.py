#!/usr/bin/env python3
"""Manual-release CI helper. Standard library; GitHub operations use authenticated gh."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

import package_payload as packager

ROOT = Path(__file__).resolve().parents[1]


def version(value):
    if not isinstance(value, str) or len(value) > 32 or not re.fullmatch(r'v?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)', value):
        raise ValueError('Use a stable version such as 1.3.0 or v1.3.0 (no prerelease/build suffix)')
    return value.removeprefix('v')


def newer(a, b):
    return tuple(map(int, version(a).split('.'))) > tuple(map(int, version(b).split('.')))


def gh(*args):
    return subprocess.run(['gh', *args], check=True, capture_output=True, text=True).stdout


def api(path, missing=False):
    try:
        return json.loads(gh('api', '--method', 'GET', path))
    except subprocess.CalledProcessError as error:
        if missing and 'HTTP 404' in error.stderr:
            return None
        raise RuntimeError('GitHub API request failed: '+path+'\n'+error.stderr[-500:]) from error


def release_by_tag(repository, tag):
    # REST /releases/tags only finds published releases. The CLI also resolves a
    # draft's pending tag and supplies its database ID for the REST readback.
    try:
        return json.loads(gh('release', 'view', tag, '--repo', repository, '--json', 'databaseId,isDraft,tagName'))
    except subprocess.CalledProcessError as error:
        if error.stderr.strip().lower() == 'release not found':
            return None
        raise RuntimeError('Release lookup failed: '+error.stderr[-500:]) from error


def unused(repository, tag):
    if api(f'repos/{repository}/git/ref/tags/{tag}', missing=True) is not None or release_by_tag(repository, tag) is not None:
        raise ValueError(f'{tag} already exists; releases and tags are never overwritten')


def prepare(value, repository, target, kind, directory):
    if value is None:
        if kind != 'moonlight':
            raise ValueError('--version is required for CivHomebrew releases')
        value = read_json(ROOT/'payload.json', 65536)['release']
    value = version(value)
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}', repository) or not re.fullmatch(r'[0-9a-f]{40}', target):
        raise ValueError('Invalid repository or exact target commit')
    api('repos/'+repository)  # A missing latest release must not hide authentication/repository errors.
    previous = api(f'repos/{repository}/releases/latest', missing=True)
    if previous and not newer(value, previous['tag_name']):
        raise ValueError('Version must be newer than the latest stable release')
    unused(repository, 'v'+value)
    directory.mkdir(parents=True, exist_ok=True)
    if (directory/'plan.json').exists():
        raise ValueError('Release plan already exists; use a fresh build directory')
    has_baseline = False
    if previous and kind == 'civhomebrew':
        names = ['civhomebrew-release.json', 'core.civupdate', 'launcher.civupdate']
        if any(a['name'] == names[0] for a in previous['assets']):
            for name in names:
                assets = [a for a in previous['assets'] if a['name'] == name and a.get('state') == 'uploaded']
                if len(assets) != 1:
                    raise ValueError('Previous release must have exactly one '+name)
            baseline = directory/'previous'
            baseline.mkdir()
            for name in names:
                gh('release', 'download', previous['tag_name'], '--repo', repository, '--dir', str(baseline), '--pattern', name)
            has_baseline = True
        else:
            print('Previous release predates component snapshots; both components receive the new version.')
    plan = {'version': value, 'tag': 'v'+value, 'repository': repository, 'target': target, 'kind': kind,
            'previous': {'id': previous['id'], 'tag': previous['tag_name']} if previous else None, 'baseline': has_baseline}
    (directory/'plan.json').write_text(json.dumps(plan, indent=2)+'\n')
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
            output.write('version='+value+'\ntag=v'+value+'\n')
    print('Prepared '+kind+' '+plan['tag']+' at '+target)
    return plan


def component_versions(sources, baseline, value):
    """An unchanged module keeps its version; changed modules receive the new version."""
    result = {}
    for name, source in sources.items():
        old = baseline.get(name)
        if old and source == old['source']:
            result[name] = version(old['release'])
        else:
            if old and not newer(value, old['release']):
                raise ValueError('New version must exceed the changed '+name+' version')
            result[name] = value
    return result


def read_json(path, limit=packager.MAX_FRAME):
    if path.stat().st_size > limit:
        raise ValueError('Oversized release file: '+str(path))
    return json.loads(path.read_text(encoding='utf-8'))


def build_homebrew(plan, directory):
    import payload_menu as menu
    baseline = {}
    if plan.get('baseline'):
        baseline = {name: read_json(directory/'previous'/(name+'.civupdate')) for name in ('core', 'launcher')}
        for name, image in baseline.items():
            if image != menu.system_package(name, image['source'], image['release']):
                raise ValueError('Invalid previous '+name+' component')
        manifest = read_json(directory/'previous/civhomebrew-release.json', 65536)
        if manifest != menu.release_manifest(baseline, manifest['release']) or version(manifest['release']) != version(plan['previous']['tag']):
            raise ValueError('Previous release manifest/components disagree')
    releases = component_versions({name: menu.component_bundle(name) for name in ('core', 'launcher')}, baseline, plan['version'])
    menu.main(['build', '--release', plan['version'], '--core-release', releases['core'], '--launcher-release', releases['launcher']])
    packager.main(['--directory', str(ROOT/'examples/hello'), '--non-interactive', '--no-save-metadata',
                   '--version', plan['version'], '--releases-url', 'https://github.com/'+plan['repository']+'/releases/latest',
                   '--update-asset', 'hello.civapp', '-o', str(ROOT/'dist/hello.civapp')])
    return [ROOT/'dist/civhomebrew-release.json', ROOT/'dist/core.civupdate', ROOT/'dist/launcher.civupdate',
            ROOT/'dist/payload-menu.js', ROOT/'dist/hello.civapp', ROOT/'tools/package_payload.py']


def build_moonlight(plan):
    packager.main(['--directory', str(ROOT), '--non-interactive', '--no-save-metadata', '--version', plan['version'],
                   '--releases-url', 'https://github.com/'+plan['repository']+'/releases/latest',
                   '--update-asset', 'gba.civapp', '-o', str(ROOT/'dist/gba.civapp')])
    return [ROOT/'dist/gba.civapp', ROOT/'tools/package_payload.py']


def build(directory):
    plan = read_json(directory/'plan.json')
    files = build_homebrew(plan, directory) if plan['kind'] == 'civhomebrew' else build_moonlight(plan)
    checksums = ''.join(hashlib.sha256(p.read_bytes()).hexdigest()+'  '+p.name+'\n' for p in files)
    checksum_file = ROOT/'dist/SHA256SUMS'
    checksum_file.write_text(checksums)
    files.append(checksum_file)
    artifacts = {p.name: {'path': str(p.relative_to(ROOT)), 'bytes': p.stat().st_size,
                        'sha256': hashlib.sha256(p.read_bytes()).hexdigest()} for p in files}
    (directory/'artifacts.json').write_text(json.dumps(artifacts, indent=2)+'\n')
    title = 'CivHomebrew' if plan['kind'] == 'civhomebrew' else 'Moonlight'
    notes = title+' '+plan['tag']+'\n\nCommit: `'+plan['target']+'`\n\n'
    if plan['kind'] == 'civhomebrew':
        manifest = read_json(ROOT/'dist/civhomebrew-release.json')
        notes += '\n'.join('- '+name+': v'+version(target['release']) for name, target in manifest['components'].items())+'\n\n'
        notes += 'Includes the complete component set for updating directly from older releases.\n'
    else:
        notes += 'Install gba.civapp through CivHomebrew, or use X Menu → Check app updates.\n'
    notes += '\nSee SHA256SUMS for artifact checksums.\n'
    (directory/'notes.md').write_text(notes)
    print('Validated release artifacts: '+', '.join(artifacts))


def publish(directory):
    plan = read_json(directory/'plan.json')
    artifacts = read_json(directory/'artifacts.json')
    for name, info in artifacts.items():
        path = packager.inside(ROOT, info['path'])
        if path.name != name or path.stat().st_size != info['bytes'] or hashlib.sha256(path.read_bytes()).hexdigest() != info['sha256']:
            raise ValueError('Release artifact changed after validation: '+name)
    repository, tag = plan['repository'], plan['tag']
    current = api(f'repos/{repository}/releases/latest', missing=True)
    if (current['id'] if current else None) != (plan['previous']['id'] if plan['previous'] else None):
        raise ValueError('Latest release changed during the build; run a fresh workflow')
    unused(repository, tag)
    paths = [str(ROOT/info['path']) for info in artifacts.values()]
    gh('release', 'create', tag, *paths, '--repo', repository, '--draft', '--target', plan['target'],
       '--title', ('CivHomebrew ' if plan['kind'] == 'civhomebrew' else 'Moonlight ')+tag,
       '--notes-file', str(directory/'notes.md'))
    draft = release_by_tag(repository, tag)
    if not draft or draft.get('isDraft') is not True or draft.get('tagName') != tag or type(draft.get('databaseId')) is not int or draft['databaseId'] < 1:
        raise ValueError('Created draft release could not be verified; left unpublished')
    remote = api(f'repos/{repository}/releases/{draft["databaseId"]}')
    uploaded = remote['assets']
    if len(uploaded) != len(artifacts):
        raise ValueError('Draft release asset set differs; left unpublished')
    for name, info in artifacts.items():
        found = [a for a in uploaded if a['name'] == name and a.get('state') == 'uploaded']
        if len(found) != 1 or found[0]['size'] != info['bytes'] or found[0].get('digest') not in (None, 'sha256:'+info['sha256']):
            raise ValueError('Draft release asset verification failed: '+name+'; left unpublished')
    latest = api(f'repos/{repository}/releases/latest', missing=True)
    if (latest['id'] if latest else None) != (plan['previous']['id'] if plan['previous'] else None):
        raise ValueError('Latest release changed during upload; draft left unpublished')
    gh('release', 'edit', tag, '--repo', repository, '--draft=false', '--latest')
    print('Published https://github.com/'+repository+'/releases/tag/'+tag)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('prepare')
    p.add_argument('--version', help='Defaults to payload.json release for Moonlight')
    p.add_argument('--repository', required=True)
    p.add_argument('--target', required=True)
    p.add_argument('--kind', required=True, choices=['civhomebrew', 'moonlight'])
    p.add_argument('--directory', type=Path, default=Path('build/release'))
    for command in ('build', 'publish'):
        p = sub.add_parser(command)
        p.add_argument('--directory', type=Path, default=Path('build/release'))
    args = parser.parse_args(argv)
    try:
        if args.command == 'prepare':
            prepare(args.version, args.repository, args.target, args.kind, args.directory)
        elif args.command == 'build':
            build(args.directory)
        else:
            publish(args.directory)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.CalledProcessError) as error:
        parser.exit(1, 'Release stopped: '+str(error)+'\n')


if __name__ == '__main__':
    main()
