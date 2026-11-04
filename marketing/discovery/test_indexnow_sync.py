import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

spec = importlib.util.spec_from_file_location('indexnow_sync', Path(__file__).with_name('indexnow_sync.py'))
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)


def response(status=200, body=b'', content_type='text/html', etag=''):
    result = Mock(status_code=status, headers={'Content-Type': content_type, 'ETag': etag})
    result.iter_content.return_value = [body]
    return result


def sitemap(*paths):
    return ('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' +
            ''.join('<url><loc>' + sync.ORIGIN + p + '</loc></url>' for p in paths) + '</urlset>').encode()


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.client = Mock(headers={})
        self.client.post.return_value = response()

    def setup_reads(self, *pages, paths=('/proizvod/27/',)):
        self.client.get.side_effect = [response(body=b'public-proof-12345'),
                                      response(body=sitemap(*paths)), *pages]

    def accepted(self, pages):
        sync.write_json(self.directory / 'accepted.json', {'origin': sync.ORIGIN, 'pages': pages})

    def test_first_receipt_and_no_duplicate_for_unchanged_html(self):
        self.setup_reads(response(body=b'<h1>Dell 150 EUR</h1>', etag='version1'))
        result = sync.synchronize(self.directory, self.client)
        self.assertEqual(result['status'], 'received')
        payload = self.client.post.call_args.kwargs['json']
        self.assertEqual(payload['urlList'], [sync.ORIGIN + '/proizvod/27/'])
        state = json.loads((self.directory / 'accepted.json').read_text())
        state['last_attempt'] = 0
        sync.write_json(self.directory / 'accepted.json', state)
        self.client.post.reset_mock()
        self.setup_reads(response(304))
        self.assertEqual(sync.synchronize(self.directory, self.client)['status'], 'unchanged')
        self.client.post.assert_not_called()
        self.assertEqual(self.client.get.call_args.kwargs['headers']['If-None-Match'], 'version1')

    def test_deleted_product_notifies_only_after_actual_public_404(self):
        self.accepted({sync.ORIGIN + '/proizvod/25/': {'sha256': 'old'}})
        self.setup_reads(response(body=b'home'), response(404), paths=('/',))
        result = sync.synchronize(self.directory, self.client)
        self.assertIn(sync.ORIGIN + '/proizvod/25/', result['changed_urls'])
        state = json.loads((self.directory / 'accepted.json').read_text())
        self.assertNotIn(sync.ORIGIN + '/proizvod/25/', state['pages'])

    def test_incomplete_deployment_never_acknowledges_deletion(self):
        self.accepted({sync.ORIGIN + '/proizvod/25/': {'sha256': 'old'}})
        original = (self.directory / 'accepted.json').read_bytes()
        self.setup_reads(response(body=b'home'), response(body=b'still public'), paths=('/',))
        with self.assertRaises(ValueError):
            sync.synchronize(self.directory, self.client)
        self.client.post.assert_not_called()
        self.assertEqual((self.directory / 'accepted.json').read_bytes(), original)

    def test_rejection_keeps_previous_snapshot_for_retry(self):
        self.accepted({})
        original = (self.directory / 'accepted.json').read_bytes()
        self.setup_reads(response(body=b'new'))
        self.client.post.return_value = response(429)
        self.assertEqual(sync.synchronize(self.directory, self.client)['status'], 'retry')
        self.assertEqual((self.directory / 'accepted.json').read_bytes(), original)

    def test_private_foreign_and_parameterized_urls_rejected(self):
        for value in [sync.ORIGIN + '/admin/', sync.ORIGIN + '/checkout/',
                      sync.ORIGIN + '/proizvod/27/?buy=1', 'https://other.example/proizvod/27/',
                      'https://reklik.shop@other.example/proizvod/27/']:
            self.assertFalse(sync.public_url(value), value)
        with self.assertRaises(ValueError):
            sync.sitemap_urls(sitemap('/admin/'))

    def test_202_is_receipt_not_a_claim_of_indexing(self):
        self.setup_reads(response(body=b'new'))
        self.client.post.return_value = response(202)
        result = sync.synchronize(self.directory, self.client)
        self.assertEqual(result['status'], 'received')
        self.assertEqual(result['http_status'], 202)

    def test_prevent_repeated_submission_within_five_minutes(self):
        self.setup_reads(response(body=b'new'))
        sync.synchronize(self.directory, self.client)
        self.client.get.reset_mock()
        self.assertEqual(sync.synchronize(self.directory, self.client)['status'], 'debounced')
        self.client.get.assert_not_called()


if __name__ == '__main__':
    unittest.main()
