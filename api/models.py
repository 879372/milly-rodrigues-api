import os
from django.db import models
from django.contrib.auth.models import AbstractUser
from simple_history.models import HistoricalRecords
from django.utils import timezone
class User(AbstractUser):
    ROLE_CHOICES = (
        ('admin', 'Admin'),
        ('barber', 'Profissional'),
        ('receptionist', 'Receptionist'),
        ('client', 'Client'),
    )
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default='client')
    phone = models.CharField(max_length=20, blank=True, null=True)
    birth_date = models.DateField(blank=True, null=True)
    internal_notes = models.TextField(blank=True, null=True)
    must_change_password = models.BooleanField(default=False)
    # Profissional agendável: aparece no portal de agendamento, tem horário de
    # trabalho e agenda. É independente do papel — um 'admin' pode ter
    # is_bookable=True (dono que também atende: visão de admin + agenda própria).
    is_bookable = models.BooleanField(default=False)
    history = HistoricalRecords()

    def save(self, *args, **kwargs):
        # Um barbeiro é sempre agendável; um admin/recepção só se marcado.
        if self.role == 'barber':
            self.is_bookable = True
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.first_name} {self.last_name}" if self.first_name else self.username

class Service(models.Model):
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True, null=True)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    # Valor especial opcional (pode ser maior OU menor que o normal — ex.: fim de semana
    # mais caro), cobrado nos dias marcados em SpecialPriceConfig.
    special_price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    duration_minutes = models.IntegerField(default=30)
    is_active = models.BooleanField(default=True)
    order = models.IntegerField(default=0)
    history = HistoricalRecords()

    class Meta:
        ordering = ['order', 'name']

    def effective_price(self, use_special: bool):
        if use_special and self.special_price is not None:
            return self.special_price
        return self.price

    def __str__(self):
        return self.name

class Product(models.Model):
    name = models.CharField(max_length=100)
    brand = models.CharField(max_length=100, blank=True, null=True)
    stock_quantity = models.IntegerField(default=0)
    unit_cost = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    sale_price = models.DecimalField(max_digits=10, decimal_places=2)
    min_stock_alert = models.IntegerField(default=5)
    history = HistoricalRecords()

    def __str__(self):
        return self.name

class Appointment(models.Model):
    STATUS_CHOICES = (
        ('pending', 'Aguardando confirmação'),
        ('confirmed', 'Confirmado'),
        ('completed', 'Concluído'),
        ('cancelled', 'Cancelado'),
        ('no_show', 'Não compareceu'),
    )
    PAYMENT_STATUS_CHOICES = (
        ('not_required', 'Não exigido'),
        ('pending', 'Aguardando pagamento'),
        ('paid', 'Pago'),
    )
    client = models.ForeignKey(User, on_delete=models.CASCADE, related_name='appointments_as_client')
    barber = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name='appointments_as_barber')
    services = models.ManyToManyField(Service, blank=True)
    date_time = models.DateTimeField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='confirmed')
    total_price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    discount = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    tip = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    notes = models.TextField(blank=True, null=True)

    # Pagamento antecipado via InfinitePay (portal público). Ver BookingPaymentConfig.
    payment_status = models.CharField(max_length=20, choices=PAYMENT_STATUS_CHOICES, default='not_required')
    payment_order_nsu = models.CharField(max_length=64, blank=True, default='', db_index=True)
    payment_slug = models.CharField(max_length=100, blank=True, default='')
    payment_transaction_nsu = models.CharField(max_length=64, blank=True, default='')
    payment_capture_method = models.CharField(max_length=20, blank=True, default='')  # 'pix' | 'credit_card'
    # URLs assinadas do checkout podem ultrapassar o limite padrão de 200.
    payment_checkout_url = models.URLField(max_length=1000, blank=True, default='')
    payment_amount_cents = models.PositiveIntegerField(null=True, blank=True)
    payment_confirmed_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    history = HistoricalRecords()

    def __str__(self):
        services_names = ", ".join([s.name for s in self.services.all()]) if self.pk else ""
        return f"{self.client} - {services_names} at {self.date_time}"

class PaymentMethod(models.Model):
    name = models.CharField(max_length=50)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name

class Sale(models.Model):
    client = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='product_sales_as_client')
    appointment = models.ForeignKey(Appointment, on_delete=models.SET_NULL, null=True, blank=True, related_name='associated_sales')
    total_price = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    discount = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Venda {self.id} - {self.client or 'Consumidor'}"

class Payment(models.Model):
    METHOD_CHOICES = (
        ('pix', 'PIX'),
        ('cash', 'Espécie'),
        ('credit', 'Cartão de Crédito'),
        ('debit', 'Cartão de Débito'),
    )
    appointment = models.ForeignKey(Appointment, on_delete=models.CASCADE, related_name='payments', null=True, blank=True)
    sale = models.ForeignKey(Sale, on_delete=models.CASCADE, related_name='payments', null=True, blank=True)
    method = models.CharField(max_length=20, choices=METHOD_CHOICES)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    payment_date = models.DateField(default=timezone.now)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.method} - {self.amount}"

class Expense(models.Model):
    CATEGORY_CHOICES = (
        ('fixed', 'Fixa'),
        ('variable', 'Variável'),
    )
    description = models.CharField(max_length=255)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    date = models.DateField()
    category = models.CharField(max_length=20, choices=CATEGORY_CHOICES, default='variable')
    created_at = models.DateTimeField(auto_now_add=True)
    history = HistoricalRecords()

    def __str__(self):
        return f"{self.description} - {self.amount}"

class Goal(models.Model):
    PERIOD_CHOICES = (
        ('daily', 'Diária'),
        ('weekly', 'Semanal'),
        ('monthly', 'Mensal'),
    )
    period = models.CharField(max_length=20, choices=PERIOD_CHOICES)
    target_amount = models.DecimalField(max_digits=10, decimal_places=2)
    start_date = models.DateField()
    end_date = models.DateField(null=True, blank=True)

    def __str__(self):
        return f"Meta {self.get_period_display()} - R$ {self.target_amount}"

class WorkingHour(models.Model):
    DAY_CHOICES = (
        (0, 'Segunda-feira'),
        (1, 'Terça-feira'),
        (2, 'Quarta-feira'),
        (3, 'Quinta-feira'),
        (4, 'Sexta-feira'),
        (5, 'Sábado'),
        (6, 'Domingo'),
    )
    barber = models.ForeignKey(User, on_delete=models.CASCADE, related_name='working_hours')
    day_of_week = models.IntegerField(choices=DAY_CHOICES)
    start_time = models.TimeField()
    end_time = models.TimeField()
    break_start_time = models.TimeField(null=True, blank=True)
    break_end_time = models.TimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        unique_together = ('barber', 'day_of_week')

    def __str__(self):
        return f"{self.barber} - {self.get_day_of_week_display()}"

class TimeBlock(models.Model):
    barber = models.ForeignKey(User, on_delete=models.CASCADE, related_name='time_blocks')
    start_time = models.DateTimeField()
    end_time = models.DateTimeField()
    reason = models.CharField(max_length=255, blank=True, null=True)

    def __str__(self):
        return f"Bloqueio {self.barber}: {self.start_time} - {self.end_time}"

class Notification(models.Model):
    TYPE_CHOICES = (
        ('confirmation', 'Confirmação'),
        ('reminder', 'Lembrete'),
        ('cancellation', 'Cancelamento'),
    )
    STATUS_CHOICES = (
        ('pending', 'Pendente'),
        ('sent', 'Enviado'),
        ('failed', 'Falhou'),
    )
    appointment = models.ForeignKey(Appointment, on_delete=models.CASCADE, related_name='notifications')
    type = models.CharField(max_length=20, choices=TYPE_CHOICES)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    message = models.TextField()
    sent_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.type} - {self.appointment.client} - {self.status}"

class ProductSale(models.Model):
    sale = models.ForeignKey(Sale, on_delete=models.CASCADE, related_name='items', null=True, blank=True)
    appointment = models.ForeignKey(Appointment, on_delete=models.CASCADE, related_name='product_sales', null=True, blank=True)
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='sales')
    quantity = models.IntegerField()
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)
    total_price = models.DecimalField(max_digits=10, decimal_places=2)
    created_at = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        if not self.pk: # New sale
            self.product.stock_quantity -= self.quantity
            self.product.save()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"Venda {self.product.name} x{self.quantity}"

class WaitlistEntry(models.Model):
    STATUS_CHOICES = (
        ('waiting',   'Aguardando'),
        ('contacted', 'Contactado'),
        ('scheduled', 'Agendado'),
        ('cancelled', 'Cancelado'),
    )
    PERIOD_CHOICES = (
        ('morning',   'Manhã'),
        ('afternoon', 'Tarde'),
        ('any',       'Qualquer horário'),
    )
    client           = models.ForeignKey(User, on_delete=models.CASCADE, related_name='waitlist_as_client')
    barber           = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name='waitlist_as_barber')
    services         = models.ManyToManyField(Service, blank=True)
    preferred_date   = models.DateField()
    preferred_period = models.CharField(max_length=20, choices=PERIOD_CHOICES, default='any')
    notes            = models.TextField(blank=True, null=True)
    status           = models.CharField(max_length=20, choices=STATUS_CHOICES, default='waiting')
    created_at       = models.DateTimeField(auto_now_add=True)
    updated_at       = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        services_names = ", ".join([s.name for s in self.services.all()]) if self.pk else ""
        return f"Fila: {self.client} → {services_names} em {self.preferred_date}"


def _weekday_list(raw):
    """Normaliza uma lista de dias da semana (0=Segunda..6=Domingo, convenção de
    ``WorkingHour``/``date.weekday()``), descartando valores fora do range ou inválidos."""
    try:
        return sorted({int(d) for d in (raw or []) if 0 <= int(d) <= 6})
    except (TypeError, ValueError):
        return []


class SingletonConfigModel(models.Model):
    """Base para configurações únicas (uma linha só, sempre ``pk=1``)."""

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class BookingPaymentConfig(SingletonConfigModel):
    """Configuração única (singleton) do pagamento antecipado no portal público.

    ``payment_days`` guarda os dias da semana (0=Segunda ... 6=Domingo, mesma
    convenção de :class:`WorkingHour` / ``date.weekday()``) em que um agendamento
    feito em ``/agendar`` só é confirmado após o pagamento via InfinitePay. O
    barbeiro liga/desliga dia a dia; dias fora da lista seguem o fluxo normal.
    """
    payment_days = models.JSONField(default=list, blank=True)
    infinitepay_handle = models.CharField(max_length=100, blank=True, default='')
    deposit_percentage = models.PositiveSmallIntegerField(default=100)
    hold_minutes = models.PositiveIntegerField(default=15)  # TTL de um checkout abandonado
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Configuração de pagamento do agendamento'
        verbose_name_plural = 'Configuração de pagamento do agendamento'

    def effective_handle(self):
        raw = (self.infinitepay_handle or os.getenv('INFINITEPAY_HANDLE', '') or '')
        return raw.strip().lstrip('$').strip()

    def active_days(self):
        return _weekday_list(self.payment_days)

    def requires_payment_on(self, day_of_week: int) -> bool:
        """``day_of_week``: 0=Segunda ... 6=Domingo (``date.weekday()``)."""
        return bool(self.effective_handle()) and day_of_week in self.active_days()

    def payment_enabled(self):
        """Ao menos um dia ativo e handle configurado (para a UI de admin)."""
        return bool(self.effective_handle() and self.active_days())

    def __str__(self):
        return f"Pagamento antecipado: {'ativo' if self.payment_enabled() else 'inativo'}"


class SpecialPriceConfig(SingletonConfigModel):
    """Configuração única (singleton) dos dias com valores especiais.

    ``special_price_days`` guarda os dias da semana (0=Segunda ... 6=Domingo, mesma
    convenção de :class:`WorkingHour` / ``date.weekday()``) em que o portal público
    cobra ``Service.special_price`` no lugar de ``Service.price`` (para os serviços
    que tiverem um valor especial configurado — pode ser mais caro ou mais barato).
    """
    special_price_days = models.JSONField(default=list, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Configuração de valores especiais'
        verbose_name_plural = 'Configuração de valores especiais'

    def active_days(self):
        return _weekday_list(self.special_price_days)

    def applies_on(self, day_of_week: int) -> bool:
        """``day_of_week``: 0=Segunda ... 6=Domingo (``date.weekday()``)."""
        return day_of_week in self.active_days()

    def __str__(self):
        return f"Valores especiais: dias {self.active_days() or 'nenhum'}"
