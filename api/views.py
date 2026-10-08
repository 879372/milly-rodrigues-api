from rest_framework import viewsets, permissions, filters
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView
from django.utils import timezone
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db.models import Sum, Count
from django_filters.rest_framework import DjangoFilterBackend
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from uuid import uuid4
from .models import User, Service, Product, Appointment, Payment, Expense, Goal, WorkingHour, TimeBlock, Notification, ProductSale, PaymentMethod, Sale, WaitlistEntry, BookingPaymentConfig, SpecialPriceConfig
from .serializers import (
    UserSerializer, PublicBarberSerializer, ServiceSerializer, ProductSerializer,
    AppointmentSerializer, PaymentSerializer, ExpenseSerializer,
    WorkingHourSerializer, TimeBlockSerializer, ProductSaleSerializer,
    GoalSerializer, PaymentMethodSerializer, SaleSerializer, WaitlistEntrySerializer,
    BookingPaymentConfigSerializer, SpecialPriceConfigSerializer
)
from .pagination import OptionalPageNumberPagination, paginate_plain_list
from .permissions import IsAdmin, IsStaff, IsFrontDesk, IsAdminOrReadOnly, IsStaffOrReadOnly, IsAdminOrOwnerBarber
from .portal import make_portal_token, read_portal_token, portal_base_url
from . import infinitepay
from .whatsapp_service import WhatsAppService

import csv
import os
from django.http import HttpResponse

import logging
logger = logging.getLogger(__name__)


def _csv_safe(value):
    """Neutraliza injeção de fórmula (CSV/Excel) prefixando valores perigosos."""
    s = '' if value is None else str(value)
    if s and s[0] in ('=', '+', '-', '@', '\t', '\r'):
        return "'" + s
    return s


def _is_staff_request(request):
    user = getattr(request, 'user', None)
    return bool(user and user.is_authenticated and (user.is_superuser or getattr(user, 'role', None) in ('admin', 'barber', 'receptionist')))


def _is_admin_request(request):
    user = getattr(request, 'user', None)
    return bool(user and user.is_authenticated and (user.is_superuser or getattr(user, 'role', None) == 'admin'))


def _enforce_barber_self(request, serializer):
    """Barbeiro (não-admin) só cria/edita registros com ``barber`` = ele mesmo."""
    if _is_admin_request(request):
        return
    from rest_framework.exceptions import PermissionDenied
    barber = serializer.validated_data.get('barber')
    if barber is None:
        serializer.validated_data['barber'] = request.user
    elif barber != request.user:
        raise PermissionDenied("Você só pode gerenciar a sua própria agenda.")

def _pending_payment_qs():
    return Appointment.objects.filter(payment_status='pending', status='pending')


def _has_scheduling_conflict(appointment):
    """Verifica se outro agendamento (não cancelado) do mesmo barbeiro conflita com o
    horário deste. Usada antes de reconfirmar um agendamento que havia sido cancelado
    (ex.: expirou por falta de pagamento e o pagamento chegou tarde)."""
    duration = sum(s.duration_minutes for s in appointment.services.all()) or 30
    start = appointment.date_time
    end = start + timedelta(minutes=duration)
    others = Appointment.objects.filter(
        barber_id=appointment.barber_id,
        date_time__date=timezone.localtime(start).date(),
    ).exclude(pk=appointment.pk).exclude(status='cancelled').prefetch_related('services')
    for other in others:
        o_duration = sum(s.duration_minutes for s in other.services.all()) or 30
        o_start = other.date_time
        o_end = o_start + timedelta(minutes=o_duration)
        if start < o_end and end > o_start:
            return True
    return False


def _check_payment_and_settle(appt, cfg_handle):
    """Consulta a InfinitePay para um agendamento ``pending`` e concilia se estiver pago.

    Devolve ``'paid'``, ``'unpaid'`` ou ``'unreachable'`` (não deu pra confirmar nem
    descartar — a InfinitePay não respondeu).
    """
    if not cfg_handle:
        return 'unreachable'
    try:
        result = infinitepay.check_payment(
            handle=cfg_handle,
            order_nsu=appt.payment_order_nsu,
            transaction_nsu=appt.payment_transaction_nsu,
            slug=appt.payment_slug,
        )
    except infinitepay.InfinitePayError:
        logger.warning("check_payment indisponível ao reconciliar o agendamento %s", appt.pk)
        return 'unreachable'

    if not result.get('paid'):
        return 'unpaid'

    _settle_appointment_payment(
        appt,
        transaction_nsu=result.get('transaction_nsu', ''),
        slug=result.get('slug', ''),
        capture_method=result.get('capture_method', ''),
    )
    return 'paid'


def expire_stale_pending_payments():
    """Libera slots de checkouts abandonados (caminho interativo, usado por
    ``available_times``/``check_availability``/``public_booking``).

    Só olha agendamentos bem além do TTL — a janela normal (``hold_minutes``) é
    tratada com mais frequência pela conciliação do cron
    (:func:`reconcile_pending_payments`) — então, na prática, processa poucas ou
    nenhuma linha na maioria das chamadas. SEMPRE confere o pagamento na InfinitePay
    antes de cancelar: nunca cancela às cegas um horário que pode ter sido pago. Se a
    InfinitePay estiver inacessível, deixa pendente para a próxima rodada (o cron tem
    seu próprio limite rígido para desistir).
    """
    cfg = BookingPaymentConfig.load()
    handle = cfg.effective_handle()
    grace = max((cfg.hold_minutes or 15) * 4, 60)
    cutoff = timezone.now() - timedelta(minutes=grace)

    cancelled = 0
    for appt in _pending_payment_qs().filter(created_at__lt=cutoff).order_by('created_at')[:20]:
        if _check_payment_and_settle(appt, handle) == 'unpaid':
            cancelled += _pending_payment_qs().filter(pk=appt.pk).update(status='cancelled')
    return cancelled


def reconcile_pending_payments(limit=100):
    """Conciliação de segurança (cron): confere cada agendamento aguardando pagamento
    na InfinitePay. Paga -> confirma; não paga e fora do TTL -> cancela; InfinitePay
    indisponível -> mantém pendente para a próxima rodada (até o limite rígido, quando
    cancela mesmo sem confirmação, pra não travar o horário pra sempre).

    Devolve ``(conciliados, cancelados)``.
    """
    cfg = BookingPaymentConfig.load()
    handle = cfg.effective_handle()
    now = timezone.now()
    ttl_cutoff = now - timedelta(minutes=cfg.hold_minutes or 15)
    hard_cutoff = now - timedelta(minutes=max((cfg.hold_minutes or 15) * 8, 240))

    settled = cancelled = 0
    for appt in _pending_payment_qs().order_by('created_at')[:limit]:
        outcome = _check_payment_and_settle(appt, handle)
        if outcome == 'paid':
            settled += 1
            continue

        past_ttl = appt.created_at < ttl_cutoff
        past_hard = appt.created_at < hard_cutoff
        should_cancel = (outcome == 'unpaid' and past_ttl) or past_hard
        if should_cancel:
            cancelled += _pending_payment_qs().filter(pk=appt.pk).update(status='cancelled')
    return settled, cancelled


def _settle_appointment_payment(appointment, *, transaction_nsu='', slug='', capture_method=''):
    """Marca o agendamento como pago (idempotente) e dispara a confirmação.

    Se o agendamento já havia sido cancelado (ex.: expirou por falta de pagamento e a
    confirmação chegou tarde) e o horário já está ocupado por outro agendamento, NÃO
    reconfirma — isso criaria um horário duplicado. Fica marcado como pago e cancelado;
    exige ação manual da equipe (reembolso ou reagendamento). Devolve o agendamento
    atualizado, com o atributo ``_slot_lost`` indicando esse caso.
    """
    from django.db import transaction as db_transaction

    # 'credit_card' (InfinitePay) -> 'credit' (Payment.METHOD_CHOICES); PIX e resto -> 'pix'.
    method = 'credit' if capture_method in ('credit_card', 'credit') else 'pix'

    with db_transaction.atomic():
        appt = Appointment.objects.select_for_update().get(pk=appointment.pk)
        if appt.payment_status == 'paid':
            appt._slot_lost = appt.status == 'cancelled'
            return appt

        slot_lost = appt.status == 'cancelled' and _has_scheduling_conflict(appt)

        appt.payment_status = 'paid'
        appt.payment_confirmed_at = timezone.now()
        if transaction_nsu:
            appt.payment_transaction_nsu = transaction_nsu
        if slug:
            appt.payment_slug = slug
        if capture_method:
            appt.payment_capture_method = capture_method
        update_fields = [
            'payment_status', 'payment_confirmed_at',
            'payment_transaction_nsu', 'payment_slug', 'payment_capture_method', 'updated_at',
        ]
        if slot_lost:
            logger.error(
                "Pagamento tardio do agendamento %s conciliado, mas o horário já está "
                "ocupado por outro agendamento. Requer ação manual (reembolso/reagendamento).",
                appt.pk,
            )
        else:
            appt.status = 'confirmed'
            update_fields.append('status')

        appt._skip_notification = True
        appt.save(update_fields=update_fields)

        # Lança o pagamento para a equipe ver ("Pago") e entrar no financeiro — mesmo
        # no caso de slot_lost, pra não perder o registro do dinheiro recebido.
        if not appt.payments.exists():
            amount = (
                appt.payment_amount_cents / 100
                if appt.payment_amount_cents
                else (appt.total_price or 0)
            )
            Payment.objects.create(
                appointment=appt,
                method=method,
                amount=amount,
                payment_date=timezone.localdate(),
            )

    appt._slot_lost = slot_lost
    if not slot_lost and not Notification.objects.filter(appointment=appt, type='confirmation', status='sent').exists():
        try:
            WhatsAppService.send_confirmation(appt)
        except Exception:  # noqa: BLE001 - a confirmação não pode derrubar o pagamento
            logger.exception("Falha ao enviar confirmação WhatsApp do agendamento %s", appt.pk)
    return appt


class BookingConfigView(APIView):
    """Configuração do pagamento antecipado (singleton).

    GET  público  -> dias e porcentagem de entrada usados pelo portal. Vazio se não
                     houver handle configurado. Não expõe a InfiniteTag.
    GET  admin    -> objeto completo (handle, hold_minutes...).
    PATCH admin   -> atualiza a configuração.
    """
    def get_permissions(self):
        if self.request.method == 'GET':
            return [permissions.AllowAny()]
        return [IsAdmin()]

    def get(self, request):
        cfg = BookingPaymentConfig.load()
        if _is_admin_request(request):
            return Response(BookingPaymentConfigSerializer(cfg).data)
        days = cfg.active_days() if cfg.effective_handle() else []
        return Response({
            'payment_days': days,
            'deposit_percentage': cfg.deposit_percentage,
        })

    def patch(self, request):
        cfg = BookingPaymentConfig.load()
        serializer = BookingPaymentConfigSerializer(cfg, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(BookingPaymentConfigSerializer(cfg).data)


class SpecialPriceConfigView(APIView):
    """Dias (0=Segunda..6=Domingo) em que os serviços com ``special_price`` cobram
    o valor especial (pode ser maior ou menor que o normal) no portal público.
    Leitura livre (nada sensível); escrita apenas admin.
    """
    def get_permissions(self):
        if self.request.method == 'GET':
            return [permissions.AllowAny()]
        return [IsAdmin()]

    def get(self, request):
        cfg = SpecialPriceConfig.load()
        return Response(SpecialPriceConfigSerializer(cfg).data)

    def patch(self, request):
        cfg = SpecialPriceConfig.load()
        serializer = SpecialPriceConfigSerializer(cfg, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(SpecialPriceConfigSerializer(cfg).data)


class UserViewSet(viewsets.ModelViewSet):
    queryset = User.objects.all().order_by('first_name', 'username')
    serializer_class = UserSerializer
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    # 'role' é tratado manualmente em get_queryset (ver nota sobre is_bookable).
    search_fields = ['first_name', 'last_name', 'username', 'phone', 'email']
    ordering_fields = ['first_name', 'username', 'date_joined']
    pagination_class = OptionalPageNumberPagination

    # Endpoints públicos estritamente delimitados
    PUBLIC_ACTIONS = ('available_times', 'check_phone', 'register_client')

    def get_permissions(self):
        if self.action == 'me':
            return [permissions.IsAuthenticated()]
        if self.action in self.PUBLIC_ACTIONS:
            return [permissions.AllowAny()]
        # Lista pública SOMENTE de barbeiros (portal de agendamento), com serializer slim
        if self.action == 'list' and self.request.query_params.get('role') == 'barber' \
                and not (self.request.user and self.request.user.is_authenticated):
            return [permissions.AllowAny()]
        if self.action in ('create', 'list', 'retrieve', 'update', 'partial_update'):
            return [IsStaff()]
        if self.action == 'destroy':
            return [IsAdmin()]
        return [IsStaff()]

    def get_throttles(self):
        if self.action == 'check_phone':
            self.throttle_scope = 'phone_lookup'
        elif self.action == 'register_client':
            self.throttle_scope = 'public_write'
        return super().get_throttles()

    def get_serializer_class(self):
        if self.action == 'list' and self.request.query_params.get('role') == 'barber' \
                and not _is_staff_request(self.request):
            return PublicBarberSerializer
        return UserSerializer

    def get_queryset(self):
        from django.db.models import Q
        qs = User.objects.all().order_by('first_name', 'username')
        role_param = self.request.query_params.get('role')
        if role_param == 'barber':
            # "barbeiro" = qualquer profissional agendável — inclui um admin que
            # também atende (visão de admin + agenda própria).
            qs = qs.filter(Q(is_bookable=True) | Q(role='barber'), is_active=True)
        elif role_param:
            qs = qs.filter(role=role_param)
        return qs

    def _guard_role_escalation(self, serializer):
        """Impede que não-admin defina/altere papel privilegiado ou flags de staff."""
        data = serializer.validated_data
        if _is_admin_request(self.request):
            return
        target_role = data.get('role')
        if target_role and target_role != 'client':
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied("Somente administradores podem criar contas de equipe.")
        data['role'] = 'client'
        data.pop('is_bookable', None)

    def perform_create(self, serializer):
        self._guard_role_escalation(serializer)
        serializer.save()

    def perform_update(self, serializer):
        instance = serializer.instance
        # Não-admin não pode editar contas de OUTROS membros da equipe
        if not _is_admin_request(self.request) and instance != self.request.user \
                and getattr(instance, 'role', 'client') != 'client':
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied("Sem permissão para editar esta conta.")
        # Não-admin não altera papel/flags/senha de terceiros
        if not _is_admin_request(self.request):
            for f in ('role', 'is_staff', 'is_superuser', 'is_active', 'is_bookable'):
                serializer.validated_data.pop(f, None)
            if instance != self.request.user:
                serializer.validated_data.pop('password', None)
        serializer.save()

    @action(detail=False, methods=['get'], permission_classes=[permissions.AllowAny])
    def check_phone(self, request):
        phone = request.query_params.get('phone')
        if not phone:
            return Response({'error': 'Phone is required'}, status=400)

        import re
        clean_phone = re.sub(r'\D', '', phone)

        user = User.objects.filter(phone=clean_phone, role='client').first()
        if user:
            # Resposta mínima para pré-preencher o formulário de agendamento.
            # NÃO expõe internal_notes / email / debt_balance / id. Informa apenas
            # se o cadastro já possui e-mail para o portal decidir se mostra o campo.
            return Response({
                'exists': True,
                'user': {
                    'first_name': user.first_name or '',
                    'birth_date': user.birth_date,
                    'has_email': bool((user.email or '').strip()),
                },
            })
        return Response({'exists': False})

    @action(detail=False, methods=['post'], permission_classes=[permissions.AllowAny])
    def register_client(self, request):
        name = request.data.get('name')
        phone = request.data.get('phone')
        birth_date = request.data.get('birth_date')
        email = (request.data.get('email') or '').strip().lower()

        import re
        clean_phone = re.sub(r'\D', '', phone) if phone else ''

        if not clean_phone:
            return Response({'error': 'Telefone é obrigatório'}, status=400)

        client = User.objects.filter(phone=clean_phone, role='client').first()
        if not email and not (client and (client.email or '').strip()):
            return Response({'error': 'E-mail é obrigatório'}, status=400)
        if email:
            try:
                validate_email(email)
            except ValidationError:
                return Response({'error': 'Informe um e-mail válido'}, status=400)

        if not client:
            client = User.objects.create(
                phone=clean_phone,
                username=f"user_{clean_phone}",
                first_name=name or '',
                birth_date=birth_date if birth_date else None,
                email=email,
                role='client',
            )
        else:
            # Só preenche campos ainda vazios — nunca sobrescreve cadastro existente.
            changed = False
            if name and not client.first_name:
                client.first_name = name
                changed = True
            if birth_date and not client.birth_date:
                client.birth_date = birth_date
                changed = True
            if email and not client.email:
                client.email = email
                changed = True
            if changed:
                client.save()

        return Response({
            'id': client.id,
            'first_name': client.first_name,
            'birth_date': client.birth_date,
            'has_email': bool((client.email or '').strip()),
        })

    @action(detail=False, methods=['get'])
    def me(self, request):
        serializer = self.get_serializer(request.user)
        return Response(serializer.data)

    @action(detail=False, methods=['post'], permission_classes=[permissions.IsAuthenticated])
    def change_password(self, request):
        user = request.user
        current_password = request.data.get('current_password')
        new_password = request.data.get('new_password')

        if not new_password:
            return Response({'error': 'A nova senha é obrigatória.'}, status=400)
        if len(new_password) < 8:
            return Response({'error': 'A nova senha deve ter pelo menos 8 caracteres.'}, status=400)

        # Exige a senha atual, salvo quando a conta está marcada para troca obrigatória
        # (primeiro acesso de barbeiro criado pelo admin).
        if not user.must_change_password:
            if not current_password or not user.check_password(current_password):
                return Response({'error': 'Senha atual incorreta.'}, status=400)

        try:
            from django.contrib.auth.password_validation import validate_password
            validate_password(new_password, user)
        except Exception as e:
            return Response({'error': ' '.join(getattr(e, 'messages', [str(e)]))}, status=400)

        user.set_password(new_password)
        user.must_change_password = False
        user.save()

        # Invalida refresh tokens antigos desta conta.
        try:
            from rest_framework_simplejwt.token_blacklist.models import OutstandingToken, BlacklistedToken
            for t in OutstandingToken.objects.filter(user=user):
                BlacklistedToken.objects.get_or_create(token=t)
        except Exception:
            pass

        return Response({'status': 'Senha alterada com sucesso.'})

    @action(detail=True, methods=['get'])
    def available_times(self, request, pk=None):
        expire_stale_pending_payments()
        barber = self.get_object()
        date_str = request.query_params.get('date')
        services_ids_str = request.query_params.get('services_ids')
        service_id = request.query_params.get('service_id') # fallback for old clients
        
        if not date_str:
            return Response({'error': 'Data é obrigatória'}, status=400)
            
        try:
            date = datetime.strptime(date_str, '%Y-%m-%d').date()
        except ValueError:
            return Response({'error': 'Formato de data inválido. Use YYYY-MM-DD.'}, status=400)

        requested_duration = 0
        if services_ids_str:
            try:
                ids = [int(i) for i in services_ids_str.split(',')]
                services = Service.objects.filter(id__in=ids)
                requested_duration = sum(s.duration_minutes for s in services)
            except (ValueError, TypeError):
                pass
        elif service_id:
            try:
                service = Service.objects.get(id=service_id)
                requested_duration = service.duration_minutes
            except Service.DoesNotExist:
                pass
                
        if requested_duration == 0:
            requested_duration = 30

        day_of_week = date.weekday()
        
        # 1. Get working hours for the day
        working_hour = WorkingHour.objects.filter(barber=barber, day_of_week=day_of_week, is_active=True).first()
        if not working_hour:
            return Response([])
            
        # Use naive datetimes for internal day comparisons to avoid TZ issues
        start_dt = datetime.combine(date, working_hour.start_time)
        end_dt = datetime.combine(date, working_hour.end_time)
        
        break_start = None
        break_end = None
        if working_hour.break_start_time and working_hour.break_end_time:
            break_start = datetime.combine(date, working_hour.break_start_time)
            break_end = datetime.combine(date, working_hour.break_end_time)
        
        # 2. Get appointments and blocks
        appointments = Appointment.objects.filter(
            barber=barber, 
            date_time__date=date
        ).exclude(status='cancelled').prefetch_related('services')
        
        blocks = TimeBlock.objects.filter(
            barber=barber,
            start_time__date=date
        )
        
        # 3. Generate slots
        available_slots = []
        now = timezone.now() # now is aware
        
        # If today, we need an aware reference for the past check
        today = timezone.localtime(now).date()
        
        # Determine search step: 30 mins default, or service duration if > 30
        step_minutes = 30
        if requested_duration > 30:
            step_minutes = requested_duration

        curr_dt = start_dt
        while curr_dt + timedelta(minutes=requested_duration) <= end_dt:
            # Check if in the past
            curr_aware = timezone.make_aware(curr_dt)
            if curr_aware < now:
                curr_dt += timedelta(minutes=step_minutes)
                continue
                
            req_start = curr_dt
            req_end = curr_dt + timedelta(minutes=requested_duration)
            is_busy = False

            # Check lunch break
            if break_start and break_end:
                if req_start < break_end and req_end > break_start:
                    is_busy = True
                    if curr_dt < break_end:
                        curr_dt = break_end
                        continue
            
            # Check appointments
            if not is_busy:
                for app in appointments:
                    # Convert app.date_time to naive for comparison
                    app_start = timezone.localtime(app.date_time).replace(tzinfo=None)
                    
                    app_services = app.services.all()
                    if app_services:
                        app_duration = sum(s.duration_minutes for s in app_services)
                    else:
                        app_duration = 30
                        
                    app_end = app_start + timedelta(minutes=app_duration)
                    if req_start < app_end and req_end > app_start:
                        is_busy = True
                        curr_dt = app_end # Move to end of appointment
                        break
            
            # Check manual blocks
            if not is_busy:
                for block in blocks:
                    b_start = timezone.localtime(block.start_time).replace(tzinfo=None)
                    b_end = timezone.localtime(block.end_time).replace(tzinfo=None)
                    if req_start < b_end and req_end > b_start:
                        is_busy = True
                        curr_dt = b_end # Move to end of block
                        break
            
            if not is_busy:
                available_slots.append(curr_dt.strftime('%H:%M'))
                curr_dt += timedelta(minutes=step_minutes)
            elif is_busy and not (break_start and curr_dt < break_end):
                # If busy because of an appointment or block, we already moved curr_dt to its end.
                # If we were ALREADY at or past the end (e.g. by break check), we don't need to do anything here,
                # but if we just found an appointment, curr_dt is now app_end.
                # We do NOT increment again, because app_end might be the start of a valid slot.
                pass
                
        return Response(available_slots)

    @action(detail=False, methods=['get'])
    def birthdays(self, request):
        today = timezone.localdate()
        # Today's birthdays
        today_bdays = User.objects.filter(
            role='client',
            birth_date__month=today.month,
            birth_date__day=today.day
        )
        
        # Week's birthdays
        next_week = today + timedelta(days=7)
        # Handle year rollover if needed, but for simplicity:
        week_bdays = User.objects.filter(
            role='client',
            birth_date__isnull=False
        )
        # Filter in python for complex date range across months
        upcoming = []
        for user in week_bdays:
            # Check if bday is in next 7 days
            bday_this_year = user.birth_date.replace(year=today.year)
            if today <= bday_this_year <= next_week:
                upcoming.append(user)
        
        return Response({
            'today': UserSerializer(today_bdays, many=True).data,
            'week': UserSerializer(upcoming, many=True).data
        })

class ServiceViewSet(viewsets.ModelViewSet):
    queryset = Service.objects.all().order_by('name')
    serializer_class = ServiceSerializer
    # Leitura pública (portal de agendamento); escrita apenas admin.
    permission_classes = [IsAdminOrReadOnly]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['name', 'description']
    ordering_fields = ['name', 'price', 'duration_minutes']
    pagination_class = OptionalPageNumberPagination

class ProductViewSet(viewsets.ModelViewSet):
    queryset = Product.objects.all().order_by('name')
    serializer_class = ProductSerializer
    # Leitura para a equipe; escrita apenas admin.
    permission_classes = [IsStaff, IsAdminOrReadOnly]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['name', 'brand']
    ordering_fields = ['name', 'sale_price', 'stock_quantity']
    pagination_class = OptionalPageNumberPagination

class AppointmentViewSet(viewsets.ModelViewSet):
    queryset = Appointment.objects.all()
    serializer_class = AppointmentSerializer
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = {
        'status': ['exact', 'in'],
        'barber': ['exact'],
        'client': ['exact'],
        'date_time': ['exact', 'date', 'gte', 'lte'],
    }
    ordering_fields = ['date_time']
    pagination_class = OptionalPageNumberPagination

    def partial_update(self, request, *args, **kwargs):
        new_status = request.data.get('status')
        payments_data = request.data.get('payments')
        discount = request.data.get('discount')
        tip = request.data.get('tip')
        
        response = super().partial_update(request, *args, **kwargs)
        
        if response.status_code == 200:
            appointment = self.get_object()
            
            # Update discount and tip if provided
            updated_fields = []
            if discount is not None:
                appointment.discount = discount
                updated_fields.append('discount')
            if tip is not None:
                appointment.tip = tip
                updated_fields.append('tip')
                
            if updated_fields:
                appointment.save(update_fields=updated_fields)

            # If cancelled, remove all payments
            if new_status == 'cancelled':
                appointment.payments.all().delete()
            
            if payments_data is not None:
                from django.db import transaction
                with transaction.atomic():
                    # Remove old payments and add new ones
                    appointment.payments.all().delete()
                    for p_data in payments_data:
                        payment_kwargs = {
                            'appointment': appointment,
                            'method': p_data['method'],
                            'amount': p_data['amount']
                        }
                        if 'payment_date' in p_data and p_data['payment_date']:
                            payment_kwargs['payment_date'] = p_data['payment_date']
                        Payment.objects.create(**payment_kwargs)
        return response

    # ---- Portal público do cliente: identificado por token assinado, não por telefone ----
    def _portal_client_id(self, request):
        token = request.query_params.get('token') or request.data.get('token')
        return read_portal_token(token)

    @action(detail=False, methods=['get'], permission_classes=[permissions.AllowAny])
    def public_list(self, request):
        client_id = self._portal_client_id(request)
        if not client_id:
            return Response({'error': 'Link inválido ou expirado. Abra o link enviado no seu WhatsApp.'}, status=403)

        appointments = Appointment.objects.filter(
            client_id=client_id
        ).order_by('-date_time')

        return Response(AppointmentSerializer(appointments, many=True).data)

    @action(detail=True, methods=['post'], permission_classes=[permissions.AllowAny])
    def public_cancel(self, request, pk=None):
        client_id = self._portal_client_id(request)
        appointment = self.get_object()

        if not client_id or appointment.client_id != client_id:
            return Response({'error': 'Link inválido ou expirado.'}, status=403)

        if appointment.status in ['completed', 'cancelled', 'no_show']:
            return Response({'error': 'Este agendamento não pode mais ser cancelado.'}, status=400)

        appointment.status = 'cancelled'
        appointment.payments.all().delete()
        appointment.save()

        return Response({'status': 'Agendamento cancelado com sucesso.'})

    @action(detail=True, methods=['post'], permission_classes=[permissions.AllowAny])
    def public_cancel_recurring(self, request, pk=None):
        client_id = self._portal_client_id(request)
        appointment = self.get_object()

        if not client_id or appointment.client_id != client_id:
            return Response({'error': 'Link inválido ou expirado.'}, status=403)

        if not appointment.notes or "Recorrente" not in appointment.notes:
            return Response({'error': 'Este agendamento não faz parte de uma série recorrente.'}, status=400)

        # Cancel all future recurring appointments for THIS client only
        recurring_apps = Appointment.objects.filter(
            client_id=client_id,
            status__in=['pending', 'confirmed'],
            date_time__gte=appointment.date_time,
            notes__icontains="Recorrente"
        )

        count = recurring_apps.count()
        Payment.objects.filter(appointment__in=recurring_apps).delete()
        recurring_apps.update(status='cancelled')

        return Response({'status': f'{count} agendamentos recorrentes cancelados com sucesso.'})

    @action(detail=False, methods=['get'], permission_classes=[IsAdmin])
    def export_csv(self, request):
        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = 'attachment; filename="agendamentos.csv"'

        writer = csv.writer(response)
        writer.writerow(['ID', 'Cliente', 'Profissional', 'Serviço', 'Data/Hora', 'Status', 'Preço Total'])

        appointments = self.filter_queryset(self.get_queryset())
        for app in appointments:
            writer.writerow([
                _csv_safe(app.id),
                _csv_safe(app.client.get_full_name() if app.client else 'N/A'),
                _csv_safe(app.barber.get_full_name() if app.barber else 'N/A'),
                _csv_safe(", ".join([s.name for s in app.services.all()]) if app.pk else 'N/A'),
                _csv_safe(app.date_time.strftime('%Y-%m-%d %H:%M')),
                _csv_safe(app.get_status_display()),
                _csv_safe(app.total_price),
            ])

        return response

    def _payment_summary(self, appointment):
        local_dt = timezone.localtime(appointment.date_time)
        paid = sum((payment.amount for payment in appointment.payments.all()), Decimal('0.00'))
        remaining = max((appointment.total_price or Decimal('0.00')) - paid, Decimal('0.00'))
        return {
            'service_name': ", ".join(s.name for s in appointment.services.all()),
            'barber_name': appointment.barber.get_full_name() if appointment.barber else '',
            'barber_phone': appointment.barber.phone if appointment.barber else '',
            'date_time': appointment.date_time.isoformat(),
            'date': local_dt.strftime('%d/%m/%Y'),
            'time': local_dt.strftime('%H:%M'),
            'total_price': str(appointment.total_price or ''),
            'paid_amount': f'{paid:.2f}',
            'remaining_amount': f'{remaining:.2f}',
            'portal_token': make_portal_token(appointment.client_id) if appointment.client_id else '',
        }

    def _try_settle(self, appointment, transaction_nsu='', slug=''):
        """Consulta a InfinitePay e concilia o agendamento.

        Retorna: ``'paid'`` | ``'unpaid'`` | ``'underpaid'`` | ``'provider_error'``.
        """
        if appointment.payment_status == 'paid':
            return 'paid'
        cfg = BookingPaymentConfig.load()
        if not cfg.effective_handle():
            return 'provider_error'
        try:
            result = infinitepay.check_payment(
                handle=cfg.effective_handle(),
                order_nsu=appointment.payment_order_nsu,
                transaction_nsu=transaction_nsu or appointment.payment_transaction_nsu,
                slug=slug or appointment.payment_slug,
            )
        except infinitepay.InfinitePayError:
            logger.exception("payment_check falhou para o agendamento %s", appointment.pk)
            return 'provider_error'

        if not result.get('paid'):
            return 'unpaid'

        paid_cents = result.get('paid_amount') or result.get('amount') or 0
        expected = appointment.payment_amount_cents or 0
        if expected and paid_cents + 1 < expected:
            logger.warning(
                "Pagamento InfinitePay abaixo do esperado (ag %s): %s < %s",
                appointment.pk, paid_cents, expected,
            )
            return 'underpaid'

        _settle_appointment_payment(
            appointment,
            transaction_nsu=transaction_nsu or result.get('transaction_nsu', ''),
            slug=slug or result.get('slug', ''),
            capture_method=result.get('capture_method', ''),
        )
        return 'paid'

    @action(detail=False, methods=['post', 'get'], permission_classes=[permissions.AllowAny], url_path='payment_confirm')
    def payment_confirm(self, request):
        """Retorno do checkout: o portal chama para saber se o pagamento foi concluído."""
        data = request.data if request.method == 'POST' else request.query_params
        order_nsu = data.get('order_nsu')
        if not order_nsu:
            return Response({'error': 'order_nsu é obrigatório.'}, status=400)

        appointment = Appointment.objects.filter(payment_order_nsu=order_nsu).first()
        if not appointment:
            return Response({'error': 'Agendamento não encontrado.'}, status=404)

        outcome = self._try_settle(
            appointment,
            transaction_nsu=data.get('transaction_nsu', '') or '',
            slug=data.get('slug', '') or '',
        )
        appointment.refresh_from_db()
        was_paid = outcome == 'paid' or appointment.payment_status == 'paid'
        # Pagamento chegou tarde demais e o horário já foi ocupado por outra pessoa
        # (ver _settle_appointment_payment): o dinheiro foi recebido, mas o agendamento
        # continua cancelado — não é um "paid" normal, a equipe precisa agir.
        slot_lost = was_paid and appointment.status == 'cancelled'
        return Response({
            'paid': was_paid and not slot_lost,
            'slot_lost': slot_lost,
            'provider_error': outcome == 'provider_error',
            'underpaid': outcome == 'underpaid',
            'appointment': self._payment_summary(appointment),
        })

    @action(detail=False, methods=['post'], permission_classes=[permissions.AllowAny], url_path='payment_webhook')
    def payment_webhook(self, request):
        """Webhook da InfinitePay (pagamento aprovado). Verificamos via payment_check.

        Responde 400 quando não conseguimos confirmar (inclui indisponibilidade da
        InfinitePay) — assim a InfinitePay reenvia mais tarde.
        """
        order_nsu = request.data.get('order_nsu')
        if not order_nsu:
            return Response({'error': 'order_nsu ausente'}, status=400)

        appointment = Appointment.objects.filter(payment_order_nsu=order_nsu).first()
        if not appointment:
            return Response({'error': 'desconhecido'}, status=400)

        outcome = self._try_settle(
            appointment,
            transaction_nsu=request.data.get('transaction_nsu', '') or '',
            slug=request.data.get('invoice_slug') or request.data.get('slug', '') or '',
        )
        if outcome == 'paid' or appointment.payment_status == 'paid':
            return Response({}, status=200)
        return Response({'error': f'pagamento não confirmado ({outcome})'}, status=400)

    PUBLIC_ACTIONS = (
        'public_booking', 'public_cancel', 'public_cancel_recurring', 'public_list',
        'payment_confirm', 'payment_webhook',
    )

    def get_permissions(self):
        if self.action in self.PUBLIC_ACTIONS:
            return [permissions.AllowAny()]
        if self.action == 'export_csv':
            return [IsAdmin()]
        return [IsStaff()]

    def get_throttles(self):
        if self.action in ('public_booking',):
            self.throttle_scope = 'public_write'
        elif self.action in ('public_cancel', 'public_cancel_recurring', 'public_list', 'payment_confirm'):
            self.throttle_scope = 'portal'
        return super().get_throttles()

    def get_queryset(self):
        qs = Appointment.objects.all()
        user = self.request.user
        if not (user and user.is_authenticated):
            return qs
        if user.is_superuser or getattr(user, 'role', None) in ('admin', 'receptionist'):
            return qs
        if getattr(user, 'role', None) == 'barber':
            return qs.filter(barber=user)
        return qs.none()

    @action(detail=True, methods=['post'])
    def cancel_recurring(self, request, pk=None):
        appointment = self.get_object()
        
        # Find all future recurring appointments for this client
        recurring_apps = Appointment.objects.filter(
            client=appointment.client,
            status__in=['pending', 'confirmed'],
            date_time__gte=appointment.date_time,
            notes__icontains="Recorrente"
        )
        
        count = recurring_apps.count()
        # Delete payments for these appointments
        Payment.objects.filter(appointment__in=recurring_apps).delete()
        recurring_apps.update(status='cancelled')
        
        return Response({'status': f'{count} agendamentos recorrentes cancelados com sucesso.'})

    @action(detail=False, methods=['post'])
    def check_availability(self, request):
        expire_stale_pending_payments()
        barber_id = request.data.get('barber_id')
        services_ids = request.data.get('services_ids')
        time_str = request.data.get('time') # HH:MM
        dates = request.data.get('dates', []) # [YYYY-MM-DD, ...]
        
        if not all([barber_id, services_ids, time_str, dates]):
            return Response({'error': 'Parâmetros incompletos'}, status=400)
            
        try:
            barber = User.objects.get(id=barber_id)
            services = Service.objects.filter(id__in=services_ids)
            if not services:
                raise Service.DoesNotExist
            total_duration = sum(s.duration_minutes for s in services)
        except (User.DoesNotExist, Service.DoesNotExist):
            return Response({'error': 'Profissional ou serviço não encontrado'}, status=404)
            
        results = []
        for d_str in dates:
            try:
                date = datetime.strptime(d_str, '%Y-%m-%d').date()
                time_obj = datetime.strptime(time_str, '%H:%M').time()
                
                # Check Working Hours
                working_hour = WorkingHour.objects.filter(barber=barber, day_of_week=date.weekday(), is_active=True).first()
                if not working_hour:
                    results.append({'date': d_str, 'available': False, 'reason': 'Fechado'})
                    continue
                
                # Check if time is within working hours
                if not (working_hour.start_time <= time_obj < working_hour.end_time):
                    results.append({'date': d_str, 'available': False, 'reason': 'Fora do horário'})
                    continue
                
                # Check Lunch Break
                if working_hour.break_start_time and working_hour.break_end_time:
                    if working_hour.break_start_time <= time_obj < working_hour.break_end_time:
                        results.append({'date': d_str, 'available': False, 'reason': 'Intervalo'})
                        continue
                
                # Check Conflicts
                req_start = timezone.make_aware(datetime.combine(date, time_obj))
                req_end = req_start + timedelta(minutes=total_duration)
                
                has_conflict = Appointment.objects.filter(
                    barber=barber,
                    date_time__lt=req_end,
                    date_time__gte=req_start - timedelta(minutes=total_duration) # Simplified logic for intersection
                ).exclude(status='cancelled').exists()
                
                if not has_conflict:
                    # Double check intersection with actual end times
                    day_apps = Appointment.objects.filter(barber=barber, date_time__date=date).exclude(status='cancelled').prefetch_related('services')
                    for app in day_apps:
                        app_start = app.date_time
                        
                        app_services = app.services.all()
                        if app_services:
                            app_duration = sum(s.duration_minutes for s in app_services)
                        else:
                            app_duration = 30
                        app_end = app_start + timedelta(minutes=app_duration)
                        if req_start < app_end and req_end > app_start:
                            has_conflict = True
                            break
                
                if has_conflict:
                    results.append({'date': d_str, 'available': False, 'reason': 'Ocupado'})
                else:
                    results.append({'date': d_str, 'available': True})
            except Exception:
                results.append({'date': d_str, 'available': False, 'reason': 'Erro'})
                
        return Response(results)

    @action(detail=False, methods=['post'], permission_classes=[permissions.AllowAny])
    def public_booking(self, request):
        name = request.data.get('name')
        phone = request.data.get('phone')
        birth_date = request.data.get('birth_date')
        submitted_email = (request.data.get('email') or '').strip().lower()
        
        import re
        clean_phone = re.sub(r'\D', '', phone) if phone else ''

        expire_stale_pending_payments()

        services_ids = request.data.get('services_ids')
        barber_id = request.data.get('barber_id')
        from django.utils.dateparse import parse_datetime
        date_time_str = request.data.get('date_time')
        date_time = parse_datetime(date_time_str) if date_time_str else None
        
        # 1. Find or create client (identifying ONLY by phone)
        client = User.objects.filter(phone=clean_phone, role='client').first()
        created = False

        if not submitted_email and not (client and (client.email or '').strip()):
            return Response({'error': 'E-mail é obrigatório para realizar o agendamento.'}, status=400)
        if submitted_email:
            try:
                validate_email(submitted_email)
            except ValidationError:
                return Response({'error': 'Informe um e-mail válido.'}, status=400)
        
        if not client:
            # Create new client if not found
            client = User.objects.create(
                phone=clean_phone,
                username=f"user_{clean_phone}",
                first_name=name or '',
                birth_date=birth_date,
                email=submitted_email,
                role='client'
            )
            created = True

        if not created:
            # Só completa campos vazios — nunca sobrescreve o cadastro de um cliente existente.
            changed = False
            if name and not client.first_name:
                client.first_name = name
                changed = True
            if birth_date and not client.birth_date:
                client.birth_date = birth_date
                changed = True
            if submitted_email and not client.email:
                client.email = submitted_email
                changed = True
            if changed:
                client.save()

        notes = request.data.get('notes')
        
        from django.db import transaction
        from datetime import timedelta
        
        # 2. Prevent Double Booking & Create appointment
        try:
            with transaction.atomic():
                services = Service.objects.filter(id__in=services_ids)
                if not services:
                    raise Service.DoesNotExist
                barber = User.objects.get(id=barber_id)
                
                total_duration = sum(s.duration_minutes for s in services)

                special_cfg = SpecialPriceConfig.load()
                is_special_day = bool(date_time) and special_cfg.applies_on(timezone.localtime(date_time).weekday())
                total_price = sum(s.effective_price(is_special_day) for s in services)

                if date_time:
                    req_start = date_time
                    req_end = req_start + timedelta(minutes=total_duration)
                    
                    # === Validação de Horário de Expediente ===
                    local_start = timezone.localtime(req_start)
                    local_end = timezone.localtime(req_end)
                    local_time = local_start.time()
                    local_end_time = local_end.time()
                    local_date = local_start.date()
                    
                    working_hour = WorkingHour.objects.filter(
                        barber=barber,
                        day_of_week=local_date.weekday(),
                        is_active=True
                    ).first()
                    
                    if not working_hour:
                        return Response({'error': 'A profissional não atende neste dia da semana.'}, status=400)
                        
                    if local_time < working_hour.start_time or local_end_time > working_hour.end_time:
                        return Response({'error': 'Horário fora do expediente da profissional.'}, status=400)
                        
                    if working_hour.break_start_time and working_hour.break_end_time:
                        if local_time < working_hour.break_end_time and local_end_time > working_hour.break_start_time:
                            return Response({'error': 'Horário conflita com o intervalo da profissional.'}, status=400)
                    
                    # Travar a tabela de agendamentos deste barbeiro neste dia para leitura
                    # Isso impede que outra requisição simultânea crie um conflito no mesmo milisegundo
                    day_appointments = Appointment.objects.select_for_update().filter(
                        barber=barber,
                        date_time__date=date_time.date()
                    ).exclude(status='cancelled').prefetch_related('services')
                    
                    for app in day_appointments:
                        app_start = app.date_time
                        app_services = app.services.all()
                        if app_services:
                            app_duration = sum(s.duration_minutes for s in app_services)
                        else:
                            app_duration = 30
                        app_end = app_start + timedelta(minutes=app_duration)
                        
                        # Verifica intersecção de tempo: (InicioA < FimB) e (FimA > InicioB)
                        if req_start < app_end and req_end > app_start:
                            return Response({'error': 'Conflito de horário. Este horário acabou de ser reservado por outra pessoa.'}, status=400)
                    
                    # Verifica também blocos manuais (TimeBlocks)
                    blocks = TimeBlock.objects.filter(
                        barber=barber,
                        start_time__date=date_time.date()
                    )
                    for block in blocks:
                        b_start = block.start_time
                        b_end = block.end_time
                        if req_start < b_end and req_end > b_start:
                            return Response({'error': 'Conflito de horário com um bloqueio na agenda da profissional.'}, status=400)

                cfg = BookingPaymentConfig.load()
                payment_required = bool(date_time) and cfg.requires_payment_on(timezone.localtime(date_time).weekday())
                deposit_amount = (
                    (total_price * Decimal(cfg.deposit_percentage) / Decimal('100'))
                    .quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                ) if payment_required else None

                # Se passou por tudo e não tem conflito, cria o agendamento de forma segura
                appointment = Appointment(
                    client=client,
                    barber=barber,
                    date_time=date_time,
                    total_price=total_price,
                    status='pending' if payment_required else 'confirmed',
                    payment_status='pending' if payment_required else 'not_required',
                    payment_amount_cents=max(1, int(deposit_amount * 100)) if payment_required else None,
                    payment_order_nsu=f"apt-{uuid4().hex[:16]}" if payment_required else '',
                    notes=notes
                )
                appointment._skip_notification = True
                appointment.save()

                appointment.services.set(services)

                if not payment_required:
                    if not Notification.objects.filter(appointment=appointment, type='confirmation', status='sent').exists():
                        WhatsAppService.send_confirmation(appointment)
                    serializer = self.get_serializer(appointment)
                    data = dict(serializer.data)
                    data['special_pricing_applied'] = is_special_day
                    # Pro portal poder linkar "Ver Meus Agendamentos" sem precisar do telefone.
                    data['portal_token'] = make_portal_token(client.id)
                    return Response(data)

        except Service.DoesNotExist:
            return Response({'error': 'Serviço não encontrado.'}, status=404)
        except User.DoesNotExist:
            return Response({'error': 'Profissional não encontrada.'}, status=404)

        # --- Pagamento antecipado: gera o link de checkout fora da transação ---
        service_names = ", ".join(s.name for s in appointment.services.all()) or "Serviços"
        local_dt = timezone.localtime(appointment.date_time)
        items = [{
            "quantity": 1,
            "price": appointment.payment_amount_cents,
            "description": f"{service_names} — {local_dt.strftime('%d/%m %H:%M')}",
        }]
        customer = {"name": client.first_name or "Cliente"}
        phone_digits = re.sub(r'\D', '', client.phone or '')
        if phone_digits:
            customer["phone_number"] = f"+55{phone_digits}" if len(phone_digits) <= 11 else f"+{phone_digits}"
        # Dados do cliente são opcionais na API da InfinitePay. Só envie e-mail real e
        # válido: domínios artificiais como ``.local`` são rejeitados com HTTP 422.
        customer_email = (client.email or '').strip()
        if customer_email:
            try:
                validate_email(customer_email)
            except ValidationError:
                logger.warning("E-mail inválido omitido do checkout do agendamento %s", appointment.pk)
            else:
                customer["email"] = customer_email

        api_base = os.getenv('PUBLIC_API_BASE_URL', '').rstrip('/') or request.build_absolute_uri('/').rstrip('/')
        redirect_url = f"{portal_base_url()}/agendar/pagamento?order_nsu={appointment.payment_order_nsu}"
        webhook_url = f"{api_base}/api/v1/appointments/payment_webhook/"

        try:
            logger.info(
                "Criando checkout InfinitePay | appointment_id=%s order_nsu=%s "
                "total_price=%s payment_amount_cents=%s items=%r",
                appointment.pk,
                appointment.payment_order_nsu,
                appointment.total_price,
                appointment.payment_amount_cents,
                items,
            )
            checkout_url, _raw = infinitepay.create_link(
                handle=cfg.effective_handle(),
                items=items,
                order_nsu=appointment.payment_order_nsu,
                redirect_url=redirect_url,
                webhook_url=webhook_url,
                customer=customer,
            )
        except infinitepay.InfinitePayError:
            logger.exception("Falha ao gerar link InfinitePay para o agendamento %s", appointment.pk)
            # O agendamento provisório já foi gravado; libere o horário sem
            # depender de signals ou de outro save da instância.
            Appointment.objects.filter(pk=appointment.pk).update(status='cancelled')
            return Response(
                {'error': 'Não foi possível iniciar o pagamento. Tente novamente em instantes.'},
                status=502,
            )

        appointment.payment_checkout_url = checkout_url
        try:
            appointment.save(update_fields=['payment_checkout_url', 'updated_at'])
        except Exception:
            logger.exception("Falha ao salvar checkout do agendamento %s; liberando horário", appointment.pk)
            Appointment.objects.filter(pk=appointment.pk).update(status='cancelled')
            return Response(
                {'error': 'Não foi possível concluir a reserva. Tente novamente em instantes.'},
                status=502,
            )

        return Response({
            'payment_required': True,
            'checkout_url': checkout_url,
            'order_nsu': appointment.payment_order_nsu,
            'appointment_id': appointment.id,
            'deposit_percentage': cfg.deposit_percentage,
            'payment_amount': f'{Decimal(appointment.payment_amount_cents) / Decimal("100"):.2f}',
            'remaining_amount': f'{max((appointment.total_price or Decimal("0.00")) - (Decimal(appointment.payment_amount_cents) / Decimal("100")), Decimal("0.00")):.2f}',
            'special_pricing_applied': is_special_day,
        })

    @action(detail=True, methods=['post'])
    def complete_with_payments(self, request, pk=None):
        appointment = self.get_object()
        payments_data = request.data.get('payments', [])
        discount = request.data.get('discount', 0.00)
        tip = request.data.get('tip', 0.00)
        
        total_paid = sum(float(p['amount']) for p in payments_data)
        
        service_total = float(appointment.total_price) - float(discount)
        maximum_payment_total = service_total + float(tip)
        
        # A gorjeta é informativa e não compõe o saldo devedor. Ela pode estar
        # incluída no valor recebido, mas nunca deve exigir uma cobrança futura.
        if total_paid > maximum_payment_total + 0.01:
            return Response({
                'error': f'A soma dos pagamentos (R$ {total_paid:.2f}) excede o valor máximo permitido (R$ {maximum_payment_total:.2f}).'
            }, status=400)
            
        # Substitui os pagamentos (semântica igual à do partial_update). Assim, quando um
        # agendamento já vem pago pela InfinitePay, o pagamento pré-preenchido reenviado
        # pela tela substitui o lançamento anterior em vez de duplicá-lo.
        from django.db import transaction
        with transaction.atomic():
            appointment.payments.all().delete()
            for p_data in payments_data:
                payment_kwargs = {
                    'appointment': appointment,
                    'method': p_data['method'],
                    'amount': p_data['amount']
                }
                if 'payment_date' in p_data and p_data['payment_date']:
                    payment_kwargs['payment_date'] = p_data['payment_date']
                Payment.objects.create(**payment_kwargs)

            # Update status and fields
            appointment.status = 'completed'
            appointment.discount = discount
            appointment.tip = tip
            appointment.save()
        
        return Response({'status': 'Agendamento concluído com sucesso!'})

    @action(detail=True, methods=['post'])
    def cancel_completed(self, request, pk=None):
        """Cancel a completed appointment, reversing all financial records."""
        from django.db import transaction
        
        appointment = self.get_object()
        
        if appointment.status != 'completed':
            return Response({'error': 'Apenas agendamentos concluídos podem ser cancelados por esta ação.'}, status=400)
        
        with transaction.atomic():
            # Delete all payments for this appointment
            appointment.payments.all().delete()
            
            # Handle associated sales
            for sale in appointment.associated_sales.all():
                # Restore product stock for each sale item
                for item in sale.items.all():
                    item.product.stock_quantity += item.quantity
                    item.product.save()
                # Delete sale payments, items, and the sale itself
                sale.payments.all().delete()
                sale.items.all().delete()
                sale.delete()
            
            # Reset financial fields and set status to cancelled
            appointment.status = 'cancelled'
            appointment.discount = 0
            appointment.tip = 0
            appointment.save()
        
        return Response({'status': 'Agendamento cancelado com sucesso. Todos os pagamentos e vendas associadas foram removidos.'})

    @action(detail=True, methods=['get', 'patch'], url_path='sale')
    def sale(self, request, pk=None):
        appointment = self.get_object()
        sale = appointment.associated_sales.order_by('-created_at').first()

        if not sale:
            return Response({'error': 'Este agendamento não possui venda de produtos associada.'}, status=404)

        if request.method == 'GET':
            return Response(SaleSerializer(sale).data)

        if appointment.status != 'completed':
            return Response({'error': 'Apenas agendamentos concluídos podem ter a venda editada por esta ação.'}, status=400)

        products_data = request.data.get('products')
        payments_data = request.data.get('payments', None)
        discount_raw = request.data.get('discount', sale.discount)

        if products_data is None:
            return Response({'error': 'Produtos são obrigatórios.'}, status=400)
        if not isinstance(products_data, list) or len(products_data) == 0:
            return Response({'error': 'Informe ao menos 1 produto.'}, status=400)

        from decimal import Decimal, InvalidOperation
        from django.db import transaction
        from django.db.models import Sum

        try:
            discount = Decimal(str(discount_raw or '0'))
        except (InvalidOperation, TypeError):
            return Response({'error': 'Desconto inválido.'}, status=400)

        try:
            parsed_products = []
            total_price = Decimal('0')
            for p in products_data:
                product_id = p.get('id') or p.get('product')
                if not product_id:
                    return Response({'error': 'Produto inválido.'}, status=400)
                qty = int(p.get('quantity', 0))
                if qty <= 0:
                    return Response({'error': 'Quantidade inválida.'}, status=400)
                try:
                    unit_price = Decimal(str(p.get('unit_price')))
                except (InvalidOperation, TypeError):
                    return Response({'error': 'Preço unitário inválido.'}, status=400)

                line_total = unit_price * qty
                total_price += line_total
                parsed_products.append({
                    'product_id': int(product_id),
                    'quantity': qty,
                    'unit_price': unit_price,
                    'total_price': line_total
                })
        except (ValueError, TypeError):
            return Response({'error': 'Produtos inválidos.'}, status=400)

        expected_total = total_price - discount

        if payments_data is None:
            total_paid = Decimal(str(sale.payments.aggregate(Sum('amount'))['amount__sum'] or 0))
        else:
            if not isinstance(payments_data, list):
                return Response({'error': 'Pagamentos inválidos.'}, status=400)
            try:
                total_paid = sum(Decimal(str(p['amount'])) for p in payments_data)
            except (InvalidOperation, TypeError, KeyError):
                return Response({'error': 'Pagamentos inválidos.'}, status=400)

        if total_paid > expected_total + Decimal('0.01'):
            return Response(
                {'error': f'O valor dos pagamentos (R$ {float(total_paid):.2f}) excede o valor total da venda (R$ {float(expected_total):.2f}).'},
                status=400
            )

        try:
            with transaction.atomic():
                for item in sale.items.select_related('product').all():
                    item.product.stock_quantity += item.quantity
                    item.product.save(update_fields=['stock_quantity'])

                sale.items.all().delete()

                for p_item in parsed_products:
                    product = Product.objects.select_for_update().get(id=p_item['product_id'])
                    if product.stock_quantity < p_item['quantity']:
                        raise ValueError(f'Estoque insuficiente para {product.name}.')

                    ProductSale.objects.create(
                        sale=sale,
                        product=product,
                        quantity=p_item['quantity'],
                        unit_price=p_item['unit_price'],
                        total_price=p_item['total_price']
                    )

                sale.total_price = total_price
                sale.discount = discount
                sale.save(update_fields=['total_price', 'discount'])

                if payments_data is not None:
                    sale.payments.all().delete()
                    for pay in payments_data:
                        payment_kwargs = {
                            'sale': sale,
                            'method': pay['method'],
                            'amount': pay['amount']
                        }
                        if 'payment_date' in pay and pay['payment_date']:
                            payment_kwargs['payment_date'] = pay['payment_date']
                        Payment.objects.create(**payment_kwargs)
        except Product.DoesNotExist:
            return Response({'error': 'Produto não encontrado.'}, status=404)
        except ValueError as e:
            return Response({'error': str(e)}, status=400)

        return Response(SaleSerializer(sale).data)

    @action(detail=False, methods=['post'])
    def walk_in(self, request):
        try:
            services_ids = request.data.get('services_ids', [])
            barber_id = request.data.get('barber_id')
            client_id = request.data.get('client_id')
            client_name = request.data.get('client_name')
            
            if not services_ids or not barber_id:
                return Response({'error': 'Parâmetros obrigatórios faltando.'}, status=400)
                
            try:
                services = Service.objects.filter(id__in=services_ids)
                if not services.exists():
                    raise Service.DoesNotExist
                barber = User.objects.get(id=barber_id)
            except (Service.DoesNotExist, User.DoesNotExist):
                return Response({'error': 'Serviço ou profissional não encontrada.'}, status=404)
                
            if client_id:
                try:
                    client = User.objects.get(id=client_id, role='client')
                except User.DoesNotExist:
                    return Response({'error': 'Cliente não encontrado.'}, status=404)
            else:
                client, _ = User.objects.get_or_create(
                    username='cliente_avulso',
                    defaults={
                        'first_name': client_name or 'Cliente Avulso',
                        'role': 'client',
                        'phone': '00000000000'
                    }
                )
                
            date_time_str = request.data.get('date_time')
            if date_time_str:
                from django.utils.dateparse import parse_datetime
                date_time = parse_datetime(date_time_str)
                if date_time and timezone.is_naive(date_time):
                    date_time = timezone.make_aware(date_time)
            else:
                date_time = timezone.now()
                
            from django.db import transaction
            with transaction.atomic():
                total_price = sum(s.price for s in services)
                appointment = Appointment(
                    client=client,
                    barber=barber,
                    date_time=date_time,
                    total_price=total_price,
                    status='confirmed',
                    notes=f"Atendimento Avulso. Nome: {client_name}" if not client_id and client_name else "Atendimento Avulso"
                )
                appointment._skip_notification = True
                appointment.save()
                appointment.services.set(services)
                    
            return Response(self.get_serializer(appointment).data, status=201)
        except Exception:
            logger.exception("Falha ao criar atendimento avulso (walk_in)")
            return Response({'error': 'Erro interno ao registrar o atendimento.'}, status=500)

class PaymentViewSet(viewsets.ModelViewSet):
    queryset = Payment.objects.all()
    serializer_class = PaymentSerializer
    permission_classes = [IsStaff]  # barbeiro vê só os próprios (get_queryset)

    def get_queryset(self):
        qs = Payment.objects.all()
        user = self.request.user
        if getattr(user, 'role', None) == 'barber' and not user.is_superuser:
            return qs.filter(appointment__barber=user)
        return qs

class PaymentMethodViewSet(viewsets.ModelViewSet):
    queryset = PaymentMethod.objects.all()
    serializer_class = PaymentMethodSerializer
    # Leitura para a equipe (checkout); escrita apenas admin.
    permission_classes = [IsStaff, IsAdminOrReadOnly]

class ExpenseViewSet(viewsets.ModelViewSet):
    queryset = Expense.objects.all()
    serializer_class = ExpenseSerializer
    permission_classes = [IsAdmin]

class DashboardView(APIView):
    permission_classes = [IsStaff]

    def get(self, request):
        today = timezone.localdate()

        # Barbeiro enxerga apenas os próprios números; admin/recepção veem a barbearia toda.
        user = request.user
        is_barber = getattr(user, 'role', None) == 'barber' and not user.is_superuser

        appt_qs = Appointment.objects.filter(date_time__date=today)
        if is_barber:
            appt_qs = appt_qs.filter(barber=user)

        total_appointments = appt_qs.exclude(status='cancelled').count()
        completed_appointments = appt_qs.filter(status='completed').count()
        predicted_revenue = appt_qs.exclude(status='cancelled').aggregate(Sum('total_price'))['total_price__sum'] or 0

        from django.db.models import Q

        date_filter = Q(
            appointment__isnull=False, appointment__date_time__date=today
        ) | Q(
            appointment__isnull=True, created_at__date=today
        )

        payments_qs = Payment.objects.filter(date_filter)
        if is_barber:
            payments_qs = payments_qs.filter(appointment__barber=user)

        completed_revenue = payments_qs.aggregate(Sum('amount'))['amount__sum'] or 0
        service_revenue = payments_qs.filter(appointment__isnull=False).aggregate(Sum('amount'))['amount__sum'] or 0
        product_revenue = payments_qs.filter(sale__isnull=False).aggregate(Sum('amount'))['amount__sum'] or 0

        goal = Goal.objects.filter(period='daily', start_date__lte=today).order_by('-start_date').first()
        daily_goal = goal.target_amount if (goal and not is_barber) else 0

        return Response({
            'total_appointments': total_appointments,
            'completed_appointments': completed_appointments,
            'predicted_revenue': float(predicted_revenue),
            'completed_revenue': float(completed_revenue),
            'service_revenue': float(service_revenue),
            'product_revenue': float(product_revenue),
            'daily_goal': float(daily_goal),
            'progress_percentage': (float(completed_revenue) / float(daily_goal) * 100) if daily_goal > 0 else 0,
            'scope': 'barber' if is_barber else 'shop',
        })

class WorkingHourViewSet(viewsets.ModelViewSet):
    queryset = WorkingHour.objects.all()
    serializer_class = WorkingHourSerializer
    # Leitura pública (cálculo de horários no portal); escrita = admin ou o próprio barbeiro.
    permission_classes = [permissions.IsAuthenticated]
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ['barber']

    def get_permissions(self):
        if self.request.method in permissions.SAFE_METHODS:
            return [permissions.AllowAny()]
        return [IsAdminOrOwnerBarber()]

    def perform_create(self, serializer):
        _enforce_barber_self(self.request, serializer)
        serializer.save()

    def perform_update(self, serializer):
        _enforce_barber_self(self.request, serializer)
        serializer.save()

class TimeBlockViewSet(viewsets.ModelViewSet):
    queryset = TimeBlock.objects.all()
    serializer_class = TimeBlockSerializer
    permission_classes = [IsAdminOrOwnerBarber]
    filter_backends = [DjangoFilterBackend]
    filterset_fields = {
        'barber': ['exact'],
        'start_time': ['exact', 'date', 'gte', 'lte'],
    }

    def get_queryset(self):
        qs = TimeBlock.objects.all()
        user = self.request.user
        if getattr(user, 'role', None) == 'barber' and not user.is_superuser:
            return qs.filter(barber=user)
        return qs

    def perform_create(self, serializer):
        _enforce_barber_self(self.request, serializer)
        serializer.save()

    def perform_update(self, serializer):
        _enforce_barber_self(self.request, serializer)
        serializer.save()

class ProductSaleViewSet(viewsets.ModelViewSet):
    queryset = ProductSale.objects.all()
    serializer_class = ProductSaleSerializer
    permission_classes = [IsStaff]

class SaleViewSet(viewsets.ModelViewSet):
    queryset = Sale.objects.all().select_related('client', 'appointment').prefetch_related('items__product', 'payments').order_by('-created_at')
    serializer_class = SaleSerializer
    permission_classes = [IsStaff]
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = {
        'client': ['exact'],
        'appointment': ['exact', 'isnull'],
        'created_at': ['date', 'date__gte', 'date__lte'],
    }
    ordering_fields = ['created_at', 'total_price']
    pagination_class = OptionalPageNumberPagination

    def create(self, request, *args, **kwargs):
        # Custom bulk creation for Sale, Items and Payments
        data = request.data
        products_data = data.get('products', [])
        payments_data = data.get('payments', [])
        appointment_id = data.get('appointment')
        client_id = data.get('client')
        discount = data.get('discount', 0.00)

        if not products_data:
            return Response({'error': 'Produtos são obrigatórios.'}, status=400)

        total_price = sum(float(p['unit_price']) * int(p['quantity']) for p in products_data)
        total_paid = sum(float(p['amount']) for p in payments_data)
        
        expected_total = total_price - float(discount)

        if total_paid > expected_total + 0.01:
            return Response({'error': f'O valor dos pagamentos (R$ {total_paid:.2f}) excede o valor total da venda (R$ {expected_total:.2f}).'}, status=400)

        # Create Sale
        sale = Sale.objects.create(
            appointment_id=appointment_id,
            client_id=client_id,
            total_price=total_price,
            discount=discount
        )

        # Create Items
        for p_item in products_data:
            try:
                product = Product.objects.get(id=p_item['id'])
            except (Product.DoesNotExist, KeyError, ValueError):
                sale.delete()
                return Response({'error': 'Produto inválido na venda.'}, status=400)
            ProductSale.objects.create(
                sale=sale,
                product=product,
                quantity=int(p_item['quantity']),
                unit_price=p_item['unit_price'],
                total_price=float(p_item['unit_price']) * int(p_item['quantity'])
            )

        # Create Payments
        for pay in payments_data:
            payment_kwargs = {
                'sale': sale,
                'method': pay['method'],
                'amount': pay['amount']
            }
            if 'payment_date' in pay and pay['payment_date']:
                payment_kwargs['payment_date'] = pay['payment_date']
            Payment.objects.create(**payment_kwargs)

        return Response(SaleSerializer(sale).data, status=201)

class GoalViewSet(viewsets.ModelViewSet):
    queryset = Goal.objects.all()
    serializer_class = GoalSerializer
    permission_classes = [IsAdmin]

class FinancialSummaryView(APIView):
    permission_classes = [IsAdmin]

    def get(self, request):
        start_date_str = request.query_params.get('start_date')
        end_date_str = request.query_params.get('end_date')
        
        if start_date_str and end_date_str:
            try:
                from django.utils.dateparse import parse_date
                start_date = parse_date(start_date_str)
                end_date = parse_date(end_date_str)
                
                # Make them aware datetimes
                start_datetime = timezone.make_aware(datetime.combine(start_date, datetime.min.time()))
                end_datetime = timezone.make_aware(datetime.combine(end_date, datetime.max.time()))
            except (ValueError, TypeError):
                return Response({'error': 'Datas inválidas'}, status=400)
        else:
            # Default to current month
            today = timezone.localtime(timezone.now())
            start_datetime = timezone.make_aware(datetime(today.year, today.month, 1))
            if today.month == 12:
                next_month = timezone.make_aware(datetime(today.year + 1, 1, 1))
            else:
                next_month = timezone.make_aware(datetime(today.year, today.month + 1, 1))
            end_datetime = next_month - timedelta(seconds=1)

        expenses = Expense.objects.filter(date__range=[start_datetime.date(), end_datetime.date()])
        total_expenses = expenses.aggregate(Sum('amount'))['amount__sum'] or 0
        
        from django.db.models import Q

        # All revenue
        date_filter = Q(
            appointment__isnull=False, appointment__date_time__range=[start_datetime, end_datetime]
        ) | Q(
            appointment__isnull=True, created_at__range=[start_datetime, end_datetime]
        )

        all_payments = Payment.objects.filter(date_filter).select_related(
            'appointment', 'appointment__client',
            'sale', 'sale__client'
        ).prefetch_related('sale__items', 'sale__items__product', 'appointment__services')
        
        total_revenue = all_payments.aggregate(Sum('amount'))['amount__sum'] or 0
        service_revenue = all_payments.filter(appointment__isnull=False).aggregate(Sum('amount'))['amount__sum'] or 0
        product_revenue = all_payments.filter(sale__isnull=False).aggregate(Sum('amount'))['amount__sum'] or 0
        
        method_summary = {}
        
        # Helper to add transactions
        def add_to_summary(method, amount, count, detail):
            if method not in method_summary:
                method_summary[method] = {
                    'method': method,
                    'total_amount': 0.0,
                    'count': 0,
                    'details': []
                }
            method_summary[method]['total_amount'] += float(amount)
            method_summary[method]['count'] += count
            method_summary[method]['details'].append(detail)

        # Process All Payments
        for p in all_payments:
            if p.appointment:
                desc = ", ".join([s.name for s in p.appointment.services.all()]) if p.appointment.services.exists() else 'Serviço'
                client_name = p.appointment.client.get_full_name() if p.appointment.client else 'Cliente Anônimo'
                p_type = 'service'
            elif p.sale:
                desc = ", ".join([f"{item.quantity}x {item.product.name}" for item in p.sale.items.all()]) or "Venda de Produtos"
                client_name = p.sale.client.get_full_name() if p.sale.client else 'Venda Avulsa'
                p_type = 'product'
            else:
                desc = "Outro"
                client_name = "N/A"
                p_type = 'other'

            add_to_summary(
                p.method, 
                p.amount, 
                1, 
                {
                    'type': p_type,
                    'client': client_name,
                    'description': desc,
                    'amount': float(p.amount),
                    'date': p.created_at.isoformat()
                }
            )

        return Response({
            'total_revenue': float(total_revenue),
            'service_revenue': float(service_revenue),
            'product_revenue': float(product_revenue),
            'total_expenses': float(total_expenses),
            'net_profit': float(total_revenue) - float(total_expenses),
            'method_summary': sorted(method_summary.values(), key=lambda x: x['total_amount'], reverse=True),
            'expenses': ExpenseSerializer(expenses, many=True).data
        })

class GlobalHistoryView(APIView):
    permission_classes = [IsAdmin]

    def get(self, request):

        # Get history for Appointments
        from django.db.models import Q as _Q
        history_qs = Appointment.history.all().select_related('history_user', 'client')

        search = request.query_params.get('search')
        if search:
            history_qs = history_qs.filter(
                _Q(client__first_name__icontains=search) |
                _Q(client__last_name__icontains=search) |
                _Q(history_user__first_name__icontains=search)
            )

        paginate = 'page' in request.query_params
        if paginate:
            try:
                page = max(1, int(request.query_params.get('page', 1)))
            except (TypeError, ValueError):
                page = 1
            try:
                page_size = min(100, max(1, int(request.query_params.get('page_size', 20))))
            except (TypeError, ValueError):
                page_size = 20
            total_count = history_qs.count()
            start = (page - 1) * page_size
            history_records = history_qs[start:start + page_size]
        else:
            history_records = history_qs[:100]

        data = []
        for h in history_records:
            action_map = {'+': 'Criou', '~': 'Editou', '-': 'Apagou'}
            
            # Simplified change list
            changes = []
            if h.history_type == '~' and h.prev_record:
                delta = h.diff_against(h.prev_record)
                for change in delta.changes:
                    changes.append({
                        'field': change.field,
                        'old': change.old,
                        'new': change.new
                    })

            data.append({
                'history_id': h.history_id,
                'id': h.id,
                'user': h.history_user.first_name if h.history_user else 'Sistema',
                'action': action_map.get(h.history_type, 'Alterou'),
                'date': h.history_date,
                'status': h.status,
                'client_name': h.client.get_full_name() if h.client else 'N/A',
                'service_name': ", ".join([s.name for s in h.services.all()]) if getattr(h, 'services', None) and h.services.exists() else 'N/A',
                'changes': changes
            })

        if paginate:
            return Response({
                'count': total_count,
                'next': page + 1 if start + page_size < total_count else None,
                'previous': page - 1 if page > 1 else None,
                'results': data,
            })

        return Response(data)

class DebtsView(APIView):
    permission_classes = [IsStaff]  # operacional (cobrança); relatórios agregados ficam em FinancialSummary/ClientSpending (admin)

    def get(self, request):
        from django.db.models import Sum
        from .models import Appointment, Sale
        
        debts = []
        
        # Completed appointments with unpaid balances
        appointments = Appointment.objects.filter(status='completed').select_related('client').prefetch_related('services')
        for app in appointments:
            # Gorjetas registradas não são dívida do cliente.
            expected = float(app.total_price or 0) - float(app.discount or 0)
            paid = float(app.payments.aggregate(Sum('amount'))['amount__sum'] or 0)
            if expected - paid > 0.01:
                debts.append({
                    'type': 'appointment',
                    'id': app.id,
                    'client_id': app.client.id if app.client else None,
                    'client_name': app.client.get_full_name() if app.client else 'Cliente Anônimo',
                    'date': app.date_time.isoformat(),
                    'description': f"Serviço: {', '.join([s.name for s in app.services.all()]) if app.services.exists() else 'Serviço'}",
                    'total_price': expected,
                    'total_paid': paid,
                    'remaining_debt': expected - paid
                })
                
        # Sales with unpaid balances
        sales = Sale.objects.all().select_related('client').prefetch_related('items__product')
        for sale in sales:
            expected = float(sale.total_price or 0) - float(sale.discount or 0)
            paid = float(sale.payments.aggregate(Sum('amount'))['amount__sum'] or 0)
            if expected - paid > 0.01:
                items_desc = ", ".join([f"{item.quantity}x {item.product.name}" for item in sale.items.all()]) or "Venda de Produtos"
                debts.append({
                    'type': 'sale',
                    'id': sale.id,
                    'client_id': sale.client.id if sale.client else None,
                    'client_name': sale.client.get_full_name() if sale.client else 'Consumidor',
                    'date': sale.created_at.isoformat(),
                    'description': f"Produtos: {items_desc}",
                    'total_price': expected,
                    'total_paid': paid,
                    'remaining_debt': expected - paid
                })
                
        # Sort by date descending
        debts.sort(key=lambda x: x['date'], reverse=True)
        return Response(debts)

    def post(self, request):
        debt_type = request.data.get('type')
        debt_id = request.data.get('id')
        payments_data = request.data.get('payments', [])
        
        if not debt_type or not debt_id or not payments_data:
            return Response({'error': 'Parâmetros obrigatórios faltando.'}, status=400)
            
        from django.db import transaction
        from django.db.models import Sum
        from .models import Appointment, Sale, Payment
        
        with transaction.atomic():
            if debt_type == 'appointment':
                try:
                    app = Appointment.objects.get(id=debt_id)
                except Appointment.DoesNotExist:
                    return Response({'error': 'Agendamento não encontrado.'}, status=404)
                    
                # Gorjetas registradas não são dívida do cliente.
                expected = float(app.total_price or 0) - float(app.discount or 0)
                paid = float(app.payments.aggregate(Sum('amount'))['amount__sum'] or 0)
                remaining = expected - paid
                
                new_payment_sum = sum(float(p['amount']) for p in payments_data)
                if new_payment_sum > remaining + 0.01:
                    return Response({'error': f'O valor pago (R$ {new_payment_sum:.2f}) excede o saldo devedor restante (R$ {remaining:.2f}).'}, status=400)
                    
                for p_data in payments_data:
                    payment_kwargs = {
                        'appointment': app,
                        'method': p_data['method'],
                        'amount': p_data['amount']
                    }
                    if 'payment_date' in p_data and p_data['payment_date']:
                        payment_kwargs['payment_date'] = p_data['payment_date']
                    Payment.objects.create(**payment_kwargs)
                
                updated_paid = paid + new_payment_sum
                return Response({
                    'type': 'appointment',
                    'id': app.id,
                    'remaining_debt': expected - updated_paid
                })
                
            elif debt_type == 'sale':
                try:
                    sale = Sale.objects.get(id=debt_id)
                except Sale.DoesNotExist:
                    return Response({'error': 'Venda não encontrada.'}, status=404)
                    
                expected = float(sale.total_price or 0) - float(sale.discount or 0)
                paid = float(sale.payments.aggregate(Sum('amount'))['amount__sum'] or 0)
                remaining = expected - paid
                
                new_payment_sum = sum(float(p['amount']) for p in payments_data)
                if new_payment_sum > remaining + 0.01:
                    return Response({'error': f'O valor pago (R$ {new_payment_sum:.2f}) excede o saldo devedor restante (R$ {remaining:.2f}).'}, status=400)
                    
                for p_data in payments_data:
                    payment_kwargs = {
                        'sale': sale,
                        'method': p_data['method'],
                        'amount': p_data['amount']
                    }
                    if 'payment_date' in p_data and p_data['payment_date']:
                        payment_kwargs['payment_date'] = p_data['payment_date']
                    Payment.objects.create(**payment_kwargs)
                    
                updated_paid = paid + new_payment_sum
                return Response({
                    'type': 'sale',
                    'id': sale.id,
                    'remaining_debt': expected - updated_paid
                })
            else:
                return Response({'error': 'Tipo de débito inválido.'}, status=400)

class ClientSpendingView(APIView):
    permission_classes = [IsAdmin]

    def get(self, request):
        start_date_str = request.query_params.get('start_date')
        end_date_str = request.query_params.get('end_date')
        
        if start_date_str and end_date_str:
            try:
                from django.utils.dateparse import parse_date
                start_date = parse_date(start_date_str)
                end_date = parse_date(end_date_str)
                start_datetime = timezone.make_aware(datetime.combine(start_date, datetime.min.time()))
                end_datetime = timezone.make_aware(datetime.combine(end_date, datetime.max.time()))
            except (ValueError, TypeError):
                return Response({'error': 'Datas inválidas'}, status=400)
        else:
            today = timezone.localtime(timezone.now())
            start_datetime = timezone.make_aware(datetime(today.year, today.month, 1))
            if today.month == 12:
                next_month = timezone.make_aware(datetime(today.year + 1, 1, 1))
            else:
                next_month = timezone.make_aware(datetime(today.year, today.month + 1, 1))
            end_datetime = next_month - timedelta(seconds=1)

        from django.db.models import Sum, Q
        
        client_spending = {}
        
        date_filter = Q(
            appointment__isnull=False, appointment__date_time__range=[start_datetime, end_datetime]
        ) | Q(
            appointment__isnull=True, created_at__range=[start_datetime, end_datetime]
        )

        all_payments = Payment.objects.filter(date_filter).select_related(
            'appointment__client', 'sale__client'
        )

        for p in all_payments:
            client = None
            is_service = False
            is_product = False
            
            if p.appointment and p.appointment.client:
                client = p.appointment.client
                is_service = True
            elif p.sale and p.sale.client:
                client = p.sale.client
                is_product = True
                
            if client:
                client_id = client.id
                if client_id not in client_spending:
                    client_spending[client_id] = {
                        'client_id': client.id,
                        'client_name': client.get_full_name() or client.username,
                        'total_spent': 0.0,
                        'services_spent': 0.0,
                        'products_spent': 0.0,
                        'transactions': 0
                    }
                client_spending[client_id]['total_spent'] += float(p.amount)
                client_spending[client_id]['transactions'] += 1
                if is_service:
                    client_spending[client_id]['services_spent'] += float(p.amount)
                if is_product:
                    client_spending[client_id]['products_spent'] += float(p.amount)
            else:
                avulso_key = 'avulso'
                if avulso_key not in client_spending:
                    client_spending[avulso_key] = {
                        'client_id': None,
                        'client_name': 'Cliente Avulso',
                        'total_spent': 0.0,
                        'services_spent': 0.0,
                        'products_spent': 0.0,
                        'transactions': 0
                    }
                client_spending[avulso_key]['total_spent'] += float(p.amount)
                client_spending[avulso_key]['transactions'] += 1
                if is_service:
                    client_spending[avulso_key]['services_spent'] += float(p.amount)
                if is_product:
                    client_spending[avulso_key]['products_spent'] += float(p.amount)


        result = sorted(client_spending.values(), key=lambda x: x['total_spent'], reverse=True)

        search = request.query_params.get('search')
        if search:
            search_lower = search.lower()
            result = [r for r in result if search_lower in (r['client_name'] or '').lower()]

        return paginate_plain_list(request, result)


class WaitlistViewSet(viewsets.ModelViewSet):
    """
    Fila de espera. Clientes se cadastram publicamente; barbeiros gerenciam pelo painel.
    """
    queryset = WaitlistEntry.objects.select_related('client', 'barber').prefetch_related('services').all()
    serializer_class = WaitlistEntrySerializer
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = {
        'status':  ['exact', 'in'],
        'barber':  ['exact'],
        'preferred_date': ['exact', 'gte', 'lte'],
    }
    ordering_fields = ['created_at', 'preferred_date']

    def get_permissions(self):
        if self.action == 'public_join':
            return [permissions.AllowAny()]
        return [IsStaff()]

    def get_throttles(self):
        if self.action == 'public_join':
            self.throttle_scope = 'public_write'
        return super().get_throttles()

    def get_queryset(self):
        qs = WaitlistEntry.objects.select_related('client', 'barber').prefetch_related('services').all()
        user = self.request.user
        if getattr(user, 'role', None) == 'barber' and not user.is_superuser:
            return qs.filter(barber=user)
        return qs

    @action(detail=False, methods=['post'], permission_classes=[permissions.AllowAny])
    def public_join(self, request):
        """
        Endpoint público para o cliente entrar na fila de espera.
        Fluxo idêntico ao public_booking: localiza ou cria o cliente por telefone.
        """
        import re
        name       = request.data.get('name', '')
        phone      = request.data.get('phone', '')
        birth_date = request.data.get('birth_date')
        clean_phone = re.sub(r'\D', '', phone) if phone else ''

        if not clean_phone:
            return Response({'error': 'Telefone é obrigatório.'}, status=400)

        service_id       = request.data.get('service_id')
        services_ids     = request.data.get('services_ids')
        barber_id        = request.data.get('barber_id')
        preferred_date   = request.data.get('preferred_date')   # YYYY-MM-DD
        preferred_period = request.data.get('preferred_period', 'any')
        notes            = request.data.get('notes', '')

        if not barber_id or not preferred_date or (not service_id and not services_ids):
            return Response({'error': 'Serviço, profissional e data preferida são obrigatórios.'}, status=400)

        # 1. Localiza ou cria o cliente (nunca sobrescreve cadastro existente)
        client = User.objects.filter(phone=clean_phone, role='client').first()
        if not client:
            client = User.objects.create(
                phone=clean_phone,
                username=f"user_{clean_phone}",
                first_name=name or '',
                birth_date=birth_date if birth_date else None,
                role='client'
            )
        else:
            changed = False
            if name and not client.first_name:
                client.first_name = name
                changed = True
            if birth_date and not client.birth_date:
                client.birth_date = birth_date
                changed = True
            if changed:
                client.save()

        try:
            barber  = User.objects.get(id=barber_id)
        except User.DoesNotExist:
            return Response({'error': 'Profissional não encontrada.'}, status=404)

        services = []
        if service_id:
            try:
                services.append(Service.objects.get(id=service_id))
            except Service.DoesNotExist:
                return Response({'error': 'Serviço não encontrado.'}, status=404)
        elif services_ids:
            services = list(Service.objects.filter(id__in=services_ids))
            if len(services) != len(services_ids):
                return Response({'error': 'Um ou mais serviços não foram encontrados.'}, status=404)

        # 2. Cria a entrada na fila
        entry = WaitlistEntry.objects.create(
            client=client,
            barber=barber,
            preferred_date=preferred_date,
            preferred_period=preferred_period,
            notes=notes,
            status='waiting'
        )
        if services:
            entry.services.set(services)

        return Response(WaitlistEntrySerializer(entry).data, status=201)
