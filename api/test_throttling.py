"""O isolamento do cache nos testes deve preservar a proteção do login."""
from unittest.mock import patch

from .test_utils import IsolatedCacheAPITestCase


class LoginThrottleTests(IsolatedCacheAPITestCase):
    @patch('rest_framework.throttling.ScopedRateThrottle.timer', return_value=1000.0)
    def test_login_limits_failed_attempts_and_resets_after_one_minute(self, clock):
        payload = {'username': 'nonexistent-user', 'password': 'invalid-password'}
        for attempt in range(10):
            with self.subTest(attempt=attempt + 1):
                response = self.client.post('/api/v1/token/', payload, format='json')
                self.assertEqual(response.status_code, 401, response.data)

        blocked = self.client.post('/api/v1/token/', payload, format='json')
        self.assertEqual(blocked.status_code, 429, blocked.data)
        self.assertEqual(blocked['Retry-After'], '60')

        clock.return_value = 1061.0
        retried = self.client.post('/api/v1/token/', payload, format='json')
        self.assertEqual(retried.status_code, 401, retried.data)
