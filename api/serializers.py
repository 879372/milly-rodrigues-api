from rest_framework import serializers
from django.utils import timezone
from .models import User, Service, Product, Appointment, Payment, Expense, Goal, WorkingHour, TimeBlock, Notification, ProductSale, PaymentMethod, Sale, WaitlistEntry, BookingPaymentConfig, SpecialPriceConfig


def _appointment_services_total(services, date_time):
    """Calcula o valor vigente para a data, incluindo preço especial."""
    if not date_time:
        return sum(s.price for s in services)
    is_special_day = SpecialPriceConfig.load().applies_on(
        timezone.localtime(date_time).weekday()
    )
    return sum(s.effective_price(is_special_day) for s in services)

class PublicBarberSerializer(serializers.ModelSerializer):
    """Exposição mínima de barbeiros para o portal público de agendamento."""
    class Meta:
        model = User
        fields = ('id', 'first_name', 'last_name')


class UserSerializer(serializers.ModelSerializer):
    debt_balance = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ('id', 'username', 'first_name', 'last_name', 'email', 'role', 'phone', 'birth_date',
                  'internal_notes', 'password', 'debt_balance', 'must_change_password', 'is_bookable')
        extra_kwargs = {
            'password': {'write_only': True, 'required': False},
            'must_change_password': {'read_only': True},
        }

    def validate_password(self, value):
        if value:
            from django.contrib.auth.password_validation import validate_password
            validate_password(value)
        return value

    def to_representation(self, instance):
        data = super().to_representation(instance)
        # Anotações internas e saldo devedor: apenas admin/recepção (não barbeiro).
        request = self.context.get('request')
        role = getattr(getattr(request, 'user', None), 'role', None)
        if role not in ('admin', 'receptionist') and not getattr(getattr(request, 'user', None), 'is_superuser', False):
            data.pop('internal_notes', None)
            data.pop('debt_balance', None)
        return data

    def get_debt_balance(self, obj):
        if obj.role != 'client':
            return "0.00"
        
        from django.db.models import Sum
        from .models import Appointment, Sale
        
        # Debts from completed appointments
        appointments = Appointment.objects.filter(client=obj, status='completed')
        total_appointment_debt = 0.00
        for app in appointments:
            # A gorjeta é informativa e não entra no saldo devedor.
            expected = float(app.total_price or 0) - float(app.discount or 0)
            paid = float(app.payments.aggregate(Sum('amount'))['amount__sum'] or 0)
            if expected > paid:
                total_appointment_debt += (expected - paid)
                
        # Debts from product sales
        sales = Sale.objects.filter(client=obj)
        total_sale_debt = 0.00
        for sale in sales:
            expected = float(sale.total_price or 0) - float(sale.discount or 0)
            paid = float(sale.payments.aggregate(Sum('amount'))['amount__sum'] or 0)
            if expected > paid:
                total_sale_debt += (expected - paid)
                
        return f"{total_appointment_debt + total_sale_debt:.2f}"

    def create(self, validated_data):
        password = validated_data.pop('password', None)
        user = super().create(validated_data)
        if password:
            user.set_password(password)
            user.save()
        return user

    def update(self, instance, validated_data):
        password = validated_data.pop('password', None)
        user = super().update(instance, validated_data)
        if password:
            user.set_password(password)
            user.save()
        return user

class ServiceSerializer(serializers.ModelSerializer):
    class Meta:
        model = Service
        fields = '__all__'

class ProductSerializer(serializers.ModelSerializer):
    class Meta:
        model = Product
        fields = '__all__'

class PaymentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Payment
        fields = '__all__'

class PaymentMethodSerializer(serializers.ModelSerializer):
    class Meta:
        model = PaymentMethod
        fields = '__all__'

class AppointmentSerializer(serializers.ModelSerializer):
    payments = PaymentSerializer(many=True, read_only=True)
    client_name = serializers.CharField(source='client.get_full_name', read_only=True)
    client_phone = serializers.CharField(source='client.phone', read_only=True)
    barber_name = serializers.CharField(source='barber.get_full_name', read_only=True)
    barber_phone = serializers.CharField(source='barber.phone', read_only=True)
    service_name = serializers.SerializerMethodField()
    services_details = serializers.SerializerMethodField()
    skip_notification = serializers.BooleanField(write_only=True, required=False, default=False)

    class Meta:
        model = Appointment
        fields = '__all__'

    # Detalhes internos do checkout: visíveis pra equipe (útil pra suporte/depuração),
    # ocultos do cliente (portal público — anônimo ou sem papel de equipe).
    _INTERNAL_PAYMENT_FIELDS = ('payment_order_nsu', 'payment_slug', 'payment_transaction_nsu', 'payment_checkout_url')

    def to_representation(self, instance):
        data = super().to_representation(instance)
        request = self.context.get('request')
        role = getattr(getattr(request, 'user', None), 'role', None)
        is_staff = bool(
            request and request.user and request.user.is_authenticated
            and (request.user.is_superuser or role in ('admin', 'barber', 'receptionist'))
        )
        if not is_staff:
            for f in self._INTERNAL_PAYMENT_FIELDS:
                data.pop(f, None)
        return data

    def get_service_name(self, obj):
        return ", ".join([s.name for s in obj.services.all()])

    def get_services_details(self, obj):
        return [{'id': s.id, 'name': s.name, 'price': s.price, 'duration_minutes': s.duration_minutes} for s in obj.services.all()]

    def create(self, validated_data):
        services = validated_data.pop('services', [])
        skip_notification = validated_data.pop('skip_notification', False)

        if services:
            # O servidor é a fonte da verdade para preço normal/especial.
            validated_data['total_price'] = _appointment_services_total(
                services, validated_data.get('date_time')
            )
        
        instance = Appointment(**validated_data)
        instance._skip_notification = True
        instance.save()
        
        if services:
            instance.services.set(services)
            
        if not skip_notification:
            from .whatsapp_service import WhatsAppService
            from .models import Notification
            if not Notification.objects.filter(appointment=instance, type='confirmation', status='sent').exists():
                WhatsAppService.send_confirmation(instance)
            
        return instance

    def update(self, instance, validated_data):
        services = validated_data.pop('services', None)
        if services is not None and instance.status != 'completed':
            date_time = validated_data.get('date_time', instance.date_time)
            validated_data['total_price'] = _appointment_services_total(services, date_time)
        instance = super().update(instance, validated_data)
        if services is not None:
            instance.services.set(services)
        return instance

class ExpenseSerializer(serializers.ModelSerializer):
    class Meta:
        model = Expense
        fields = '__all__'

class WorkingHourSerializer(serializers.ModelSerializer):
    class Meta:
        model = WorkingHour
        fields = '__all__'

class TimeBlockSerializer(serializers.ModelSerializer):
    barber_name = serializers.CharField(source='barber.get_full_name', read_only=True)
    class Meta:
        model = TimeBlock
        fields = '__all__'

class ProductSaleSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(source='product.name', read_only=True)

    class Meta:
        model = ProductSale
        fields = '__all__'

class GoalSerializer(serializers.ModelSerializer):
    class Meta:
        model = Goal
        fields = '__all__'

class SaleSerializer(serializers.ModelSerializer):
    items = ProductSaleSerializer(many=True, read_only=True)
    payments = PaymentSerializer(many=True, read_only=True)
    client_name = serializers.SerializerMethodField()
    total_paid = serializers.SerializerMethodField()

    class Meta:
        model = Sale
        fields = '__all__'

    def get_client_name(self, obj):
        if obj.client:
            return obj.client.get_full_name() or obj.client.first_name or obj.client.username
        return 'Consumidor final'

    def get_total_paid(self, obj):
        return float(sum(p.amount for p in obj.payments.all()))

class WaitlistEntrySerializer(serializers.ModelSerializer):
    client_name   = serializers.SerializerMethodField()
    client_phone  = serializers.SerializerMethodField()
    barber_name   = serializers.SerializerMethodField()
    service_name  = serializers.SerializerMethodField()
    preferred_period_display = serializers.CharField(source='get_preferred_period_display', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)

    class Meta:
        model = WaitlistEntry
        fields = '__all__'

    def get_client_name(self, obj):
        return obj.client.get_full_name() or obj.client.first_name or obj.client.username

    def get_client_phone(self, obj):
        return obj.client.phone or ''

    def get_barber_name(self, obj):
        return obj.barber.get_full_name() if obj.barber else ''

    def get_service_name(self, obj):
        return ", ".join([s.name for s in obj.services.all()])


def _validate_weekday_list(value):
    """Compartilhado pelos configs de dias (pagamento antecipado / valores especiais)."""
    if not isinstance(value, list) or not all(isinstance(d, int) and 0 <= d <= 6 for d in value):
        raise serializers.ValidationError('Use uma lista de dias entre 0 (segunda) e 6 (domingo).')
    return sorted(set(value))


class BookingPaymentConfigSerializer(serializers.ModelSerializer):
    payment_enabled = serializers.SerializerMethodField()

    def get_payment_enabled(self, obj):
        return obj.payment_enabled()

    class Meta:
        model = BookingPaymentConfig
        fields = ('payment_days', 'infinitepay_handle', 'hold_minutes', 'payment_enabled', 'updated_at')
        read_only_fields = ('updated_at',)

    def validate_payment_days(self, value):
        return _validate_weekday_list(value)

    def validate_hold_minutes(self, value):
        if value < 1 or value > 120:
            raise serializers.ValidationError('Use um valor entre 1 e 120 minutos.')
        return value


class SpecialPriceConfigSerializer(serializers.ModelSerializer):
    class Meta:
        model = SpecialPriceConfig
        fields = ('special_price_days', 'updated_at')
        read_only_fields = ('updated_at',)

    def validate_special_price_days(self, value):
        return _validate_weekday_list(value)
