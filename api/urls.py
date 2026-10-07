from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    UserViewSet, ServiceViewSet, ProductViewSet, 
    AppointmentViewSet, PaymentViewSet, ExpenseViewSet,
    DashboardView, WorkingHourViewSet, TimeBlockViewSet, ProductSaleViewSet, GoalViewSet, FinancialSummaryView, PaymentMethodViewSet, SaleViewSet, GlobalHistoryView, DebtsView, ClientSpendingView, WaitlistViewSet,
    BookingConfigView, SpecialPriceConfigView
)

router = DefaultRouter()
router.register(r'users', UserViewSet)
router.register(r'services', ServiceViewSet)
router.register(r'products', ProductViewSet)
router.register(r'appointments', AppointmentViewSet)
router.register(r'payments', PaymentViewSet)
router.register(r'expenses', ExpenseViewSet)
router.register(r'working-hours', WorkingHourViewSet)
router.register(r'time-blocks', TimeBlockViewSet)
router.register(r'product-sales', ProductSaleViewSet)
router.register(r'goals', GoalViewSet)
router.register(r'payment-methods', PaymentMethodViewSet)
router.register(r'sales', SaleViewSet)
router.register(r'waitlist', WaitlistViewSet)

urlpatterns = [
    path('', include(router.urls)),
    path('dashboard-summary/', DashboardView.as_view(), name='dashboard-summary'),
    path('financial-summary/', FinancialSummaryView.as_view(), name='financial-summary'),
    path('global-history/', GlobalHistoryView.as_view(), name='global-history'),
    path('debts/', DebtsView.as_view(), name='debts'),
    path('client-spending/', ClientSpendingView.as_view(), name='client-spending'),
    path('booking-config/', BookingConfigView.as_view(), name='booking-config'),
    path('special-price-config/', SpecialPriceConfigView.as_view(), name='special-price-config'),
]
