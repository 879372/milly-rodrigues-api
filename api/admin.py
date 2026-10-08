from django.contrib import admin
from django.contrib.admin.models import LogEntry
from django.contrib.auth.admin import UserAdmin
from .models import (
    User, Service, Product, Appointment, PaymentMethod,
    Payment, Expense, Goal, WorkingHour, TimeBlock,
    Notification, ProductSale, Sale, BookingPaymentConfig, SpecialPriceConfig,
    MessageAutomationConfig
)


@admin.register(BookingPaymentConfig)
class BookingPaymentConfigAdmin(admin.ModelAdmin):
    list_display = ('__str__', 'payment_days', 'infinitepay_handle', 'hold_minutes', 'updated_at')


@admin.register(SpecialPriceConfig)
class SpecialPriceConfigAdmin(admin.ModelAdmin):
    list_display = ('__str__', 'special_price_days', 'updated_at')


@admin.register(MessageAutomationConfig)
class MessageAutomationConfigAdmin(admin.ModelAdmin):
    list_display = ('__str__', 'follow_up_enabled', 'review_enabled', 'return_enabled', 'updated_at')

@admin.register(User)
class CustomUserAdmin(UserAdmin):
    list_display = ('username', 'email', 'first_name', 'role', 'phone', 'is_staff')
    list_filter = ('role', 'is_staff', 'is_superuser', 'is_active')
    fieldsets = UserAdmin.fieldsets + (
        ('Informações Adicionais', {'fields': ('role', 'phone', 'birth_date', 'internal_notes')}),
    )
    add_fieldsets = UserAdmin.add_fieldsets + (
        ('Informações Adicionais', {'fields': ('role', 'phone', 'birth_date', 'internal_notes')}),
    )

@admin.register(Service)
class ServiceAdmin(admin.ModelAdmin):
    list_display = ('name', 'price', 'duration_minutes', 'is_active', 'order')
    search_fields = ('name',)
    list_editable = ('price', 'is_active', 'order')

@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ('name', 'brand', 'stock_quantity', 'sale_price', 'min_stock_alert')
    search_fields = ('name', 'brand')

@admin.register(Appointment)
class AppointmentAdmin(admin.ModelAdmin):
    list_display = ('client', 'barber', 'get_services', 'date_time', 'status', 'total_price')
    list_filter = ('status', 'date_time', 'barber')
    search_fields = ('client__username', 'client__first_name', 'services__name')

    def get_services(self, obj):
        return ", ".join([s.name for s in obj.services.all()])
    get_services.short_description = 'Serviços'
    date_hierarchy = 'date_time'

@admin.register(PaymentMethod)
class PaymentMethodAdmin(admin.ModelAdmin):
    list_display = ('name', 'is_active')

@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ('appointment', 'method', 'amount', 'created_at')
    list_filter = ('method', 'created_at')

@admin.register(Expense)
class ExpenseAdmin(admin.ModelAdmin):
    list_display = ('description', 'amount', 'date', 'category')
    list_filter = ('category', 'date')

@admin.register(Goal)
class GoalAdmin(admin.ModelAdmin):
    list_display = ('period', 'target_amount', 'start_date', 'end_date')

@admin.register(WorkingHour)
class WorkingHourAdmin(admin.ModelAdmin):
    list_display = ('barber', 'day_of_week', 'start_time', 'end_time', 'is_active')
    list_filter = ('day_of_week', 'barber')

@admin.register(TimeBlock)
class TimeBlockAdmin(admin.ModelAdmin):
    list_display = ('barber', 'start_time', 'end_time', 'reason')
    list_filter = ('barber', 'start_time')

@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ('appointment', 'type', 'status', 'created_at')
    list_filter = ('type', 'status')

@admin.register(Sale)
class SaleAdmin(admin.ModelAdmin):
    list_display = ('id', 'client', 'total_price', 'created_at')
    list_filter = ('created_at',)

@admin.register(ProductSale)
class ProductSaleAdmin(admin.ModelAdmin):
    list_display = ('product', 'quantity', 'total_price', 'created_at')
    list_filter = ('created_at',)

@admin.register(LogEntry)
class LogEntryAdmin(admin.ModelAdmin):
    list_display = ('action_time', 'user', 'content_type', 'object_repr', 'action_flag')
    list_filter = ('action_flag', 'content_type', 'user')
    search_fields = ('object_repr', 'change_message')
    date_hierarchy = 'action_time'
