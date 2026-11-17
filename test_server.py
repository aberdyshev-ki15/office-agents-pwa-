import os
import tempfile
import unittest

tmp = tempfile.TemporaryDirectory()
os.environ['OFFICE_DB'] = tmp.name + '/test.sqlite'
os.environ['OFFICE_TOKEN'] = 'test-token'
from fastapi.testclient import TestClient
from server import app, get


class OfficeTests(unittest.TestCase):
    def test_auth_storage_and_report(self):
        with TestClient(app) as client:
            self.assertEqual(client.get('/api/objects').status_code, 401)
            h = {'Authorization': 'Bearer test-token'}
            self.assertEqual(client.put('/api/objects/item1', headers=h, json={'address': 'Красноярск, тестовая комната', 'price': 900000}).status_code, 200)
            self.assertEqual(client.get('/api/objects', headers=h).json()[0]['address'], 'Красноярск, тестовая комната')
            result = client.post('/api/run', headers=h)
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json()['new'], 0)
            reports = client.get('/api/reports', headers=h).json()
            self.assertTrue(reports)
            response = client.get('/_blob/' + reports[0]['id'], headers=h)
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.content.startswith(b'%PDF'))


if __name__ == '__main__':
    unittest.main()
