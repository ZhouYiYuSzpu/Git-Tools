#!/usr/bin/env python3
"""Collect exposed Git metadata without requiring HEAD or running checkout."""
import argparse
from collections import deque
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from bs4 import BeautifulSoup
import dulwich.index
import dulwich.object_format
from dulwich.objects import Blob, Commit, ShaFile, Tag, Tree
from dulwich.pack import Pack, PackData, load_pack_index
import requests

COMMON = [
    '.git/HEAD', '.git/FETCH_HEAD', '.git/ORIG_HEAD', '.git/config',
    '.git/description', '.git/index', '.git/packed-refs', '.git/info/refs',
    '.git/info/exclude', '.git/logs/HEAD', '.git/objects/info/packs',
    '.git/refs/stash', '.git/logs/refs/stash',
]
BRANCHES = ('main', 'master', 'develop', 'development', 'staging', 'production')
for _branch in BRANCHES:
    for _prefix in ('refs/heads/', 'logs/refs/heads/', 'refs/remotes/origin/', 'logs/refs/remotes/origin/'):
        COMMON.append('.git/' + _prefix + _branch)
HEX = re.compile(r'(?<![0-9a-fA-F])[0-9a-f]{40}(?![0-9a-fA-F])')
REF = re.compile(r'(?<![\w/])refs/(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+')
PACK = re.compile(r'pack-([0-9a-f]{40})\.pack')


def safe_relative(path):
    """Reject URL and filesystem escapes, including Windows device names."""
    p = unquote(path).replace('\\', '/')
    parts = p.rstrip('/').split('/')
    if not p or p.startswith('/') or any(x in ('', '.', '..') for x in parts):
        return False
    for part in parts:
        if any(ord(c) < 32 or c in ':*?"<>|' for c in part) or part.endswith((' ', '.')):
            return False
        if part.split('.')[0].upper() in {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}:
            return False
    return True


def base_url(value):
    parts = urlsplit(value)
    if parts.scheme not in ('http', 'https') or not parts.netloc or parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError('URL must be an http(s) URL without credentials, query or fragment')
    path = parts.path.rstrip('/')
    for suffix in ('/.git/HEAD', '/.git'):
        if path.endswith(suffix):
            path = path[:-len(suffix)]
            break
    return urlunsplit((parts.scheme, parts.netloc, path, '', ''))


class Collector:
    def __init__(self, url, output, timeout=5, retries=1, max_requests=500, max_bytes=20_000_000, verify=True, any_status=False):
        self.url = base_url(url)
        self.root = Path(output).resolve()
        if self.root.exists() and any(self.root.iterdir()):
            raise ValueError('output directory must be empty; choose a new directory')
        self.raw = self.root / 'raw'
        self.analysis = self.root / 'analysis'
        self.raw.mkdir(parents=True, exist_ok=True)
        self.analysis.mkdir(parents=True, exist_ok=True)
        self.timeout, self.retries = timeout, retries
        self.max_requests, self.max_bytes = max_requests, max_bytes
        self.any_status = any_status
        self.session = requests.Session()
        self.session.verify = verify
        self.queue = deque()
        self.seen = set()
        self.records = []
        self.objects = {}
        self.packs_done = set()
        self.count = 0

    def schedule(self, path, source):
        if not path.startswith('.git/') or not safe_relative(path):
            return
        if path not in self.seen:
            self.seen.add(path)
            self.queue.append((path, source))

    def schedule_hash(self, oid, source):
        if re.fullmatch(r'[0-9a-f]{40}', oid) and oid != '0' * 40 and oid not in self.objects:
            self.schedule('.git/objects/' + oid[:2] + '/' + oid[2:], source)

    def refs_from_object(self, obj, source):
        if isinstance(obj, Commit):
            refs = [obj.tree, *obj.parents]
        elif isinstance(obj, Tree):
            refs = [entry.sha for entry in obj.iteritems() if entry.mode != 0o160000]
        elif isinstance(obj, Tag):
            refs = [obj.object[1]]
        else:
            refs = []
        for oid in refs:
            self.schedule_hash(oid.decode('ascii'), source)

    def inspect(self, path, local, data):
        match = re.fullmatch(r'\.git/objects/([0-9a-f]{2})/([0-9a-f]{38})', path)
        if match:
            expected = ''.join(match.groups())
            try:
                obj = ShaFile.from_path(local)
                if obj.id.decode('ascii') != expected:
                    raise ValueError('object hash mismatch')
                self.objects[expected] = obj
                self.refs_from_object(obj, path)
            except Exception as exc:
                raise ValueError('invalid Git object: ' + str(exc)) from exc
        elif path == '.git/index':
            try:
                for entry in dulwich.index.Index(local).iterobjects():
                    self.schedule_hash(entry[1].decode('ascii'), path)
            except Exception as exc:
                raise ValueError('invalid index: ' + str(exc)) from exc
        elif not path.endswith(('.pack', '.idx')):
            text = data.decode('utf-8', errors='replace')
            for oid in HEX.findall(text):
                self.schedule_hash(oid, path)
            for ref in REF.findall(text):
                self.schedule('.git/' + ref, path)
                self.schedule('.git/logs/' + ref, path)
            if path == '.git/objects/info/packs':
                for oid in PACK.findall(text):
                    for ext in ('pack', 'idx'):
                        self.schedule('.git/objects/pack/pack-' + oid + '.' + ext, path)

    def listing(self, path, data):
        soup = BeautifulSoup(data, 'html.parser')
        title = soup.title.get_text(' ', strip=True) if soup.title else ''
        header = soup.find('h1')
        heading = header.get_text(' ', strip=True) if header else ''
        if not (title.lower().startswith('index of ') or heading.lower().startswith('index of ')):
            return False
        for link in soup.find_all('a', href=True):
            parsed = urlsplit(link['href'])
            if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment or parsed.path.startswith('/'):
                continue
            child = unquote(parsed.path)
            if not safe_relative(child):
                continue
            self.schedule(path + child, path)
        return True

    def fetch(self, path, source):
        record = {'path': path, 'source': source}
        self.records.append(record)
        object_match = re.fullmatch(r'\.git/objects/([0-9a-f]{2})/([0-9a-f]{38})', path)
        if object_match and ''.join(object_match.groups()) in self.objects:
            record['result'] = 'already-in-pack'
            return
        target = self.url + '/' + '/'.join(quote(p, safe='') for p in path.split('/'))
        if path.endswith('/') and not target.endswith('/'):
            target += '/'
        response = None
        try:
            for attempt in range(self.retries + 1):
                if self.count >= self.max_requests:
                    record['result'] = 'request-limit'
                    return
                self.count += 1
                try:
                    response = self.session.get(target, timeout=self.timeout, allow_redirects=False, stream=True)
                    break
                except requests.RequestException:
                    if attempt == self.retries:
                        raise
            with response:
                record['status'] = response.status_code
                if response.status_code != 200 and not self.any_status:
                    record['result'] = 'http-error'
                    return
                data = bytearray()
                for chunk in response.iter_content(65536):
                    data.extend(chunk)
                    if len(data) > self.max_bytes:
                        raise ValueError('response exceeds --max-bytes')
            data = bytes(data)
            ctype = response.headers.get('Content-Type', '').lower()
            if path.endswith('/'):
                record['result'] = 'directory-listing' if self.listing(path, data) else 'not-a-directory-listing'
                return
            if not data or 'text/html' in ctype or data.lstrip().lower().startswith((b'<!doctype html', b'<html')):
                record['result'] = 'empty-or-html'
                return
            local = self.raw.joinpath(*PurePosixPath(path).parts)
            local.parent.mkdir(parents=True, exist_ok=True)
            if not local.resolve().is_relative_to(self.raw.resolve()):
                raise ValueError('unsafe output path')
            local.write_bytes(data)
            record['size'] = len(data)
            record['result'] = 'saved'
            try:
                self.inspect(path, local, data)
            except (ValueError, OSError) as exc:
                record['result'] = 'saved-unparsed'
                record['error'] = str(exc)
        except (requests.RequestException, ValueError, OSError) as exc:
            record['result'] = 'error'
            record['error'] = str(exc)

    def inspect_packs(self):
        folder = self.raw / '.git' / 'objects' / 'pack'
        if not folder.exists():
            return
        for data_path in folder.glob('pack-*.pack'):
            if data_path.name in self.packs_done:
                continue
            idx = data_path.with_suffix('.idx')
            if not idx.is_file():
                continue
            self.packs_done.add(data_path.name)
            record = {'path': str(data_path.relative_to(self.raw)).replace('\\', '/'), 'source': 'pack-analysis'}
            self.records.append(record)
            pack = None
            try:
                fmt = dulwich.object_format.DEFAULT_OBJECT_FORMAT
                pack = Pack.from_objects(PackData(data_path, object_format=fmt), load_pack_index(idx, object_format=fmt))
                for obj in pack.iterobjects():
                    oid = obj.id.decode('ascii')
                    self.objects[oid] = obj
                    self.refs_from_object(obj, record['path'])
                record['result'] = 'pack-parsed'
            except Exception as exc:
                record['result'] = 'pack-error'
                record['error'] = str(exc)
            finally:
                if pack is not None:
                    pack.close()

    def run(self):
        self.schedule('.git/', 'initial')
        for path in COMMON:
            self.schedule(path, 'common-path')
        while self.queue:
            while self.queue:
                path, source = self.queue.popleft()
                self.fetch(path, source)
            self.inspect_packs()
        commits = []
        for oid, obj in sorted(self.objects.items()):
            if isinstance(obj, Commit):
                commits.append({'hash': oid, 'tree': obj.tree.decode('ascii'),
                    'parents': [p.decode('ascii') for p in obj.parents],
                    'message': obj.message.decode('utf-8', errors='replace')})
        summary = {'target': self.url, 'requests': self.count, 'objects': len(self.objects),
                   'commits': commits, 'results': self.records}
        (self.analysis / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
        return summary

    def extract(self, commit_id):
        commit = self.objects.get(commit_id)
        if not isinstance(commit, Commit):
            raise ValueError('commit not available: ' + commit_id)
        root = self.root / 'extracted' / commit_id
        if root.exists() and any(root.iterdir()):
            raise ValueError('extraction destination is not empty: ' + str(root))
        missing = []
        def walk(tree_id, parts, ancestry):
            if tree_id in ancestry:
                missing.append({'path': '/'.join(parts), 'error': 'tree cycle'})
                return
            tree = self.objects.get(tree_id)
            if not isinstance(tree, Tree):
                missing.append({'path': '/'.join(parts), 'hash': tree_id})
                return
            for item in tree.iteritems():
                name = item.path.decode('utf-8', errors='replace')
                if not safe_relative(name) or '/' in name or name == '.git':
                    missing.append({'path': '/'.join(parts + [name]), 'error': 'unsafe filename'})
                    continue
                oid = item.sha.decode('ascii')
                if item.mode == 0o160000:
                    missing.append({'path': '/'.join(parts + [name]), 'hash': oid, 'error': 'submodule'})
                elif item.mode == 0o40000:
                    walk(oid, parts + [name], ancestry | {tree_id})
                else:
                    blob = self.objects.get(oid)
                    if not isinstance(blob, Blob):
                        missing.append({'path': '/'.join(parts + [name]), 'hash': oid})
                        continue
                    dest = root.joinpath(*parts, name)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(blob.data)
        walk(commit.tree.decode('ascii'), [], set())
        return missing


def offline_extract(argv):
    parser = argparse.ArgumentParser(description='Extract previously collected objects without network access.')
    parser.add_argument('directory', help='existing collection directory')
    parser.add_argument('commit', nargs='+', help='full commit ID(s), or all')
    args = parser.parse_args(argv)
    root = Path(args.directory).resolve()
    raw = root / 'raw'
    if not (raw / '.git').is_dir():
        parser.error('directory does not contain raw/.git')
    collector = Collector.__new__(Collector)
    collector.root, collector.raw, collector.analysis = root, raw, root / 'analysis'
    collector.objects, collector.packs_done = {}, set()
    collector.seen, collector.queue, collector.records = set(), deque(), []
    object_root = raw / '.git' / 'objects'
    if object_root.is_dir():
        for folder in object_root.iterdir():
            if not re.fullmatch(r'[0-9a-f]{2}', folder.name) or not folder.is_dir():
                continue
            for item in folder.iterdir():
                if not re.fullmatch(r'[0-9a-f]{38}', item.name) or not item.is_file():
                    continue
                try:
                    obj = ShaFile.from_path(item)
                    if obj.id.decode('ascii') == folder.name + item.name:
                        collector.objects[folder.name + item.name] = obj
                except Exception as exc:
                    print(f'Unable to parse {item}: {exc}', file=sys.stderr)
    collector.inspect_packs()
    selected = args.commit
    if selected == ['all']:
        selected = sorted(oid for oid, obj in collector.objects.items() if isinstance(obj, Commit))
    elif 'all' in selected or any(not re.fullmatch(r'[0-9a-f]{40}', oid) for oid in selected):
        parser.error('supply full lowercase commit IDs or the single word all')
    if not selected:
        print('No commits found', file=sys.stderr)
        return 1
    incomplete = False
    for oid in selected:
        missing = collector.extract(oid)
        incomplete = incomplete or bool(missing)
        print(f'{oid}: {len(missing)} missing or skipped entries')
        for item in missing:
            print('  ', item)
    return 1 if incomplete else 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == 'extract':
        try:
            return offline_extract(argv[1:])
        except (ValueError, OSError) as exc:
            print(f'Error: {exc}', file=sys.stderr)
            return 2
    parser = argparse.ArgumentParser(description='Collect exposed Git files and follow object references; never runs checkout.')
    parser.add_argument('url', help='authorized target URL')
    parser.add_argument('directory', help='new or empty output directory')
    parser.add_argument('--timeout', type=float, default=5)
    parser.add_argument('--retries', type=int, default=1, help='retries after a connection error')
    parser.add_argument('--max-requests', type=int, default=500)
    parser.add_argument('--max-bytes', type=int, default=20_000_000, help='maximum bytes per response')
    parser.add_argument('--insecure', action='store_true', help='disable TLS certificate verification')
    parser.add_argument('--any-status', action='store_true', help='also inspect non-200 response bodies; may save invalid data')
    parser.add_argument('--proxy', help='HTTP(S) or SOCKS proxy URL, e.g. http://127.0.0.1:8080')
    parser.add_argument('-H', '--header', action='append', default=[], metavar='NAME=VALUE')
    parser.add_argument('-b', '--branch', action='append', default=[], help='additional branch name to probe')
    parser.add_argument('--extract', action='append', default=[], metavar='COMMIT', help='extract a full commit ID; may be repeated')
    parser.add_argument('--extract-all', action='store_true', help='extract every discovered commit into a separate directory')
    args = parser.parse_args(argv)
    if args.timeout <= 0 or args.retries < 0 or args.max_requests < 1 or args.max_bytes < 1:
        parser.error('timeout, limits must be positive; retries cannot be negative')
    try:
        headers = {}
        for header in args.header:
            name, separator, value = header.partition('=')
            if not separator or not name.strip():
                raise ValueError('headers must use NAME=VALUE')
            headers[name.strip()] = value.strip()
        for branch in args.branch:
            if not safe_relative(branch):
                raise ValueError('unsafe branch name: ' + branch)
        for commit in args.extract:
            if not re.fullmatch(r'[0-9a-f]{40}', commit):
                raise ValueError('extract requires a full 40-character lowercase commit ID')
        collector = Collector(args.url, args.directory, args.timeout, args.retries, args.max_requests, args.max_bytes, not args.insecure, args.any_status)
        collector.session.headers.update(headers)
        if args.proxy:
            collector.session.proxies.update({'http': args.proxy, 'https': args.proxy})
        for branch in args.branch:
            for prefix in ('refs/heads/', 'logs/refs/heads/', 'refs/remotes/origin/', 'logs/refs/remotes/origin/'):
                collector.schedule('.git/' + prefix + branch, 'user-branch')
        result = collector.run()
        commits_to_extract = set(args.extract)
        if args.extract_all:
            commits_to_extract.update(commit['hash'] for commit in result['commits'])
        for commit in sorted(commits_to_extract):
            missing = collector.extract(commit)
            result.setdefault('extractions', {})[commit] = {'missing': missing}
        (collector.analysis / 'summary.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print(f"Requests: {result['requests']}; objects: {result['objects']}; commits: {len(result['commits'])}")
        for commit in result['commits']:
            print(commit['hash'], commit['message'].splitlines()[0] if commit['message'] else '')
        print(f"Results: {collector.root}")
        return 0 if any(r.get('result') in ('saved', 'saved-unparsed') for r in result['results']) else 1
    except (ValueError, OSError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
