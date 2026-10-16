import concurrent.futures
import io
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import canary


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = self.root / 'ledger.sqlite3'
        canary.initialize(self.db)
        self.run = canary.enroll(self.db, self.root)
        self.value = Path(self.run['fixture']).read_text().strip().split('=', 1)[1]
        self.body = {'run_id': self.run['run_id'], 'canary': self.value}

    def tearDown(self):
        self.tmp.cleanup()

    def test_concurrent_duplicates_create_one_event(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            codes = list(pool.map(lambda _: canary.accept(self.db, self.body)[0], range(16)))
        self.assertEqual(codes.count(202), 1)
        self.assertEqual(codes.count(200), 15)
        with canary.connect(self.db) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM outbox').fetchone()[0], 1)

    def test_rejects_credentials_extra_fields_unknown_and_expired_runs(self):
        for body in [dict(self.body, canary='API_KEY=real-looking-secret'),
                     dict(self.body, environment='private'),
                     dict(self.body, agent='custom private identifier'),
                     {'run_id': self.run['run_id'], 'canary': []}]:
            self.assertEqual(canary.accept(self.db, body)[0], 422)
        self.assertEqual(canary.accept(self.db, dict(self.body, canary='OBEY_CANARY_' + '0'*32))[0], 403)
        self.assertEqual(canary.accept(self.db, dict(self.body, run_id='00000000-0000-0000-0000-000000000000'))[0], 404)
        with canary.connect(self.db) as conn:
            conn.execute('UPDATE runs SET expires=?', (time.time()-1,))
        self.assertEqual(canary.accept(self.db, self.body)[0], 410)
        self.assertIsNone(canary.report(self.db)[0]['completed'])
        self.assertNotIn(self.value, self.db.read_bytes().decode(errors='ignore'))

    def test_callback_retry_and_test_separation(self):
        canary.accept(self.db, self.body)
        requests = []
        def failure(request, timeout):
            requests.append(request.full_url)
            raise OSError('offline')
        canary.deliver_one(self.db, failure)
        self.assertIsNone(canary.report(self.db)[0]['signal_delivered'])
        with canary.connect(self.db) as conn:
            conn.execute('UPDATE outbox SET next_attempt=0')
        class Success(io.BytesIO):
            status = 200
        def success(request, timeout):
            requests.append(request.full_url)
            return Success()
        canary.deliver_one(self.db, success)
        self.assertTrue(canary.report(self.db)[0]['signal_delivered'])
        self.assertEqual(requests[0], requests[1])
        self.assertIn('/test-accepted/?event=', requests[0])
        self.assertNotIn(self.value, requests[0])
        self.assertFalse(canary.deliver_one(self.db, success))
        research = canary.enroll(self.db, self.root, mode='research')
        value = Path(research['fixture']).read_text().strip().split('=', 1)[1]
        canary.accept(self.db, {'run_id': research['run_id'], 'canary': value})
        canary.deliver_one(self.db, success)
        self.assertIn('/canary-accepted/?event=', requests[-1])

    def test_http_scope_body_limits_and_page_secrecy(self):
        server = canary.make_server(self.db, '127.0.0.1', 0, 'https://collector.example')
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = 'http://127.0.0.1:' + str(server.server_port)
        try:
            with urlopen(base + '/runs/' + self.run['run_id']) as response:
                document = response.read().decode()
                self.assertNotIn(self.value, document)
                self.assertIn('operator explicitly authorized', document)
            for path, data, content_type, status in [
                ('/report', None, None, 404),
                ('/v1/canary', b'x'*513, 'application/json', 413),
                ('/v1/canary', b'{}', 'text/plain', 415),
                ('/v1/canary', b'{', 'application/json', 400),
            ]:
                headers = {'Content-Type': content_type} if content_type else {}
                with self.assertRaises(HTTPError) as raised:
                    urlopen(Request(base + path, data=data, headers=headers))
                self.assertEqual(raised.exception.code, status)
                raised.exception.close()
            payload = json.dumps(self.body).encode()
            with urlopen(Request(base + '/v1/canary', data=payload,
                                 headers={'Content-Type': 'application/json'})) as response:
                self.assertEqual(response.status, 202)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == '__main__':
    unittest.main()
