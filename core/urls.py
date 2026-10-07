from django.conf import settings
from django.contrib import admin
from django.urls import path, include
from drf_spectacular.views import SpectacularAPIView, SpectacularRedocView, SpectacularSwaggerView
from core.views import health_check
from api.auth_views import ThrottledTokenObtainPairView, ThrottledTokenRefreshView

urlpatterns = [
    path('api/health/', health_check, name='health_check'),
    path('admin/', admin.site.urls),
    path('api/v1/', include('api.urls')),

    # Auth (com rate limiting)
    path('api/v1/token/', ThrottledTokenObtainPairView.as_view(), name='token_obtain_pair'),
    path('api/v1/token/refresh/', ThrottledTokenRefreshView.as_view(), name='token_refresh'),

    # Schema
    path('api/schema/', SpectacularAPIView.as_view(), name='schema'),
]

# UIs de schema apenas fora de produção.
if settings.DEBUG:
    urlpatterns += [
        path('api/schema/swagger-ui/', SpectacularSwaggerView.as_view(url_name='schema'), name='swagger-ui'),
        path('api/schema/redoc/', SpectacularRedocView.as_view(url_name='schema'), name='redoc'),
    ]
