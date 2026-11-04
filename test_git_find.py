import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock
import zlib
import requests

from git_find import Collector, base_url, safe_relative, offline_extract


def object_file(kind, body):
    data = kind.encode() + b' ' + str(len(body)).encode() + b'\0' + body
    return hashlib.sha1(data).hexdigest(), zlib.compress(data)


class Response:
    def __init__(self, body=b'', status=200, content_type='application/octet-stream'):
        self.body, self.status_code = body, status
        self.headers = {'Content-Type': content_type}
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def iter_content(self, size): yield self.body


class CollectorTests(unittest.TestCase):
    def test_head_denied_but_reflog_discovers_commit_tree_blob(self):
        blob_id, blob = object_file('blob', b'flag{demo}\n')
        tree_id, tree = object_file('tree', b'100644 flag.txt\0' + bytes.fromhex(blob_id))
        commit_body = b'tree ' + tree_id.encode() + b'\nauthor X <x@x> 1 +0000\ncommitter X <x@x> 1 +0000\n\nhello\n'
        commit_id, commit = object_file('commit', commit_body)
        mapping = {
            '.git/HEAD': Response(b'forbidden', 403),
            '.git/logs/HEAD': Response((('0' * 40) + ' ' + commit_id + ' x\n').encode()),
            **{'.git/objects/' + oid[:2] + '/' + oid[2:]: Response(data)
               for oid, data in ((blob_id, blob), (tree_id, tree), (commit_id, commit))},
        }
        with tempfile.TemporaryDirectory() as td:
            collector = Collector('http://example.test', Path(td) / 'out', retries=0)
            collector.session.get = Mock(side_effect=lambda url, **kw: mapping.get(url.split('example.test/')[1], Response(status=404)))
            result = collector.run()
            self.assertEqual(result['objects'], 3)
            self.assertEqual(result['commits'][0]['hash'], commit_id)
            self.assertEqual(offline_extract([str(collector.root), commit_id]), 0)
            self.assertEqual((collector.root / 'extracted' / commit_id / 'flag.txt').read_bytes(), b'flag{demo}\n')
            with self.assertRaises(ValueError):
                collector.extract(commit_id)
            self.assertEqual(next(r for r in result['results'] if r['path'] == '.git/HEAD')['status'], 403)

    def test_directory_listing_does_not_require_head_and_rejects_traversal(self):
        html = b'<title>Index of /.git/</title><a href="objects/">objects/</a><a href="../evil">evil</a>'
        with tempfile.TemporaryDirectory() as td:
            collector = Collector('http://example.test', Path(td) / 'out')
            self.assertTrue(collector.listing('.git/', html))
            self.assertIn('.git/objects/', collector.seen)
            self.assertNotIn('.git/../evil', collector.seen)
            self.assertFalse(collector.listing('.git/', b'<html><a href="fake">home</a></html>'))
            collector.schedule('.git/objects/', 'second source')
            self.assertEqual(sum(1 for path, _ in collector.queue if path == '.git/objects/'), 1)

    def test_base_and_path_safety(self):
        self.assertEqual(base_url('http://example.test/site/.git/HEAD'), 'http://example.test/site')
        self.assertFalse(safe_relative('.git/../outside'))
        self.assertFalse(safe_relative('.git/%2e%2e/outside'))
        self.assertFalse(safe_relative('.git/CON'))
        self.assertTrue(safe_relative('.git/objects/13/abc'))

    def test_invalid_object_not_counted(self):
        oid = 'a' * 40
        with tempfile.TemporaryDirectory() as td:
            collector = Collector('http://example.test', Path(td) / 'out', retries=0)
            collector.session.get = Mock(return_value=Response(b'not a git object'))
            collector.fetch('.git/objects/' + oid[:2] + '/' + oid[2:], 'test')
            self.assertEqual(collector.records[-1]['result'], 'saved-unparsed')
            self.assertNotIn(oid, collector.objects)

    def test_head_connection_exception_does_not_stop_other_paths(self):
        with tempfile.TemporaryDirectory() as td:
            collector = Collector('http://example.test', Path(td) / 'out', retries=0)
            def fake_get(url, **kwargs):
                if url.endswith('/.git/HEAD'):
                    raise requests.ConnectionError('empty reply')
                if url.endswith('/.git/logs/HEAD'):
                    return Response(b'example log')
                return Response(status=404)
            collector.session.get = Mock(side_effect=fake_get)
            result = collector.run()
            self.assertTrue(any(r['path'] == '.git/HEAD' and r['result'] == 'error' for r in result['results']))
            self.assertTrue((collector.raw / '.git' / 'logs' / 'HEAD').is_file())

    def test_loopback_http_client(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == '/.git/logs/HEAD':
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b'local test log')
                else:
                    self.send_response(403)
                    self.end_headers()
            def log_message(self, *args): pass
        try:
            server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        except OSError as exc:
            self.skipTest('loopback server unavailable: ' + str(exc))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as td:
                collector = Collector(f'http://127.0.0.1:{server.server_port}', Path(td) / 'out', retries=0)
                collector.session.trust_env = False
                result = collector.run()
                self.assertEqual((collector.raw / '.git' / 'logs' / 'HEAD').read_bytes(), b'local test log')
                self.assertTrue(any(r.get('status') == 403 for r in result['results']))
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_real_git_pack_and_index_are_parsed(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / 'repo'
            repo.mkdir()
            env = dict(os.environ, GIT_AUTHOR_NAME='test', GIT_AUTHOR_EMAIL='test@example.test',
                       GIT_COMMITTER_NAME='test', GIT_COMMITTER_EMAIL='test@example.test')
            def git(*args):
                return subprocess.run(['git', *args], cwd=repo, env=env, check=True, capture_output=True)
            git('init')
            (repo / 'answer.txt').write_text('pack content', encoding='utf-8')
            git('add', 'answer.txt')
            git('commit', '-m', 'pack fixture')
            oid = git('rev-parse', 'HEAD').stdout.decode().strip()
            git('repack', '-ad')
            pack_path = next((repo / '.git' / 'objects' / 'pack').glob('*.pack'))
            pack_id = pack_path.stem
            mapping = {
                '.git/logs/HEAD': Response((('0' * 40) + ' ' + oid + ' x\n').encode()),
                '.git/objects/info/packs': Response(('P ' + pack_id + '.pack\n').encode()),
                '.git/objects/pack/' + pack_id + '.pack': Response(pack_path.read_bytes()),
                '.git/objects/pack/' + pack_id + '.idx': Response(pack_path.with_suffix('.idx').read_bytes()),
            }
            collector = Collector('http://example.test', Path(td) / 'out', retries=0)
            collector.session.get = Mock(side_effect=lambda url, **kw: mapping.get(url.split('example.test/')[1], Response(status=404)))
            result = collector.run()
            self.assertTrue(any(r['result'] == 'pack-parsed' for r in result['results']), result['results'])
            self.assertIn(oid, collector.objects)
            self.assertEqual(collector.extract(oid), [])
            self.assertEqual((collector.root / 'extracted' / oid / 'answer.txt').read_text(), 'pack content')


if __name__ == '__main__':
    unittest.main()
