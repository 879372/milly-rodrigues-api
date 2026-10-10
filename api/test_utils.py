"""Isolamento de cache para testes HTTP, sem alterar os limites da API."""
from django.core.cache import cache
from django.test import override_settings
from rest_framework.test import APITestCase


@override_settings(CACHES={
    'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        'LOCATION': 'milly-api-tests',
    },
})
class IsolatedCacheAPITestCase(APITestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
