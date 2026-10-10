"""Testes de autorização (matriz papel x endpoint) para os achados da auditoria."""
import datetime
from django.utils import timezone
from .test_utils import IsolatedCacheAPITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from .models import User, Service, Appointment, Payment
from .portal import make_portal_token

D1 = timezone.make_aware(datetime.datetime(2026, 9, 1, 12, 0))
D2 = timezone.make_aware(datetime.datetime(2026, 9, 2, 12, 0))


def auth(client, user):
    client.credentials(HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(user).access_token}')


class TipDebtTests(IsolatedCacheAPITestCase):
    def setUp(self):
        super().setUp()
        self.admin = User.objects.create_user('tip_admin', password='Sup3rSenha!', role='admin')
        self.customer = User.objects.create_user('tip_customer', password='Sup3rSenha!', role='client')
        self.appointment = Appointment.objects.create(
            client=self.customer,
            barber=self.admin,
            date_time=D1,
            total_price='40.00',
            status='confirmed',
        )
        auth(self.client, self.admin)

    def test_tip_does_not_create_debt_when_service_is_paid(self):
        completed = self.client.post(
            f'/api/v1/appointments/{self.appointment.id}/complete_with_payments/',
            {'payments': [{'method': 'pix', 'amount': '40.00'}], 'discount': '0', 'tip': '10.00'},
            format='json',
            secure=True,
        )
        self.assertEqual(completed.status_code, 200)

        debts = self.client.get('/api/v1/debts/', secure=True)
        self.assertEqual(debts.status_code, 200)
        self.assertFalse(any(
            debt['id'] == self.appointment.id and debt['type'] == 'appointment'
            for debt in debts.data
        ))

        self.appointment.refresh_from_db()
        self.assertIsNotNone(self.appointment.completed_at)


class RoleMatrixTests(IsolatedCacheAPITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user('admin1', password='Sup3rSenha!', role='admin')
        cls.barber = User.objects.create_user('barber1', password='Sup3rSenha!', role='barber')
        cls.barber2 = User.objects.create_user('barber2', password='Sup3rSenha!', role='barber')
        cls.client_u = User.objects.create_user('cli1', password='Sup3rSenha!', role='client', phone='11999990000')
        cls.svc = Service.objects.create(name='Corte', price=50, duration_minutes=30)
        for kw in (dict(barber=cls.barber, date_time=D1), dict(barber=cls.barber2, date_time=D2)):
            a = Appointment(client=cls.client_u, total_price=50, **kw)
            a._skip_notification = True
            a.save()
        cls.appt_b1, cls.appt_b2 = Appointment.objects.order_by('date_time')

    # F01 — cadastro anônimo de admin
    def test_anonymous_cannot_create_user(self):
        r = self.client.post('/api/v1/users/', {'username': 'x', 'password': 'Zxq!91aa', 'role': 'admin'})
        self.assertIn(r.status_code, (401, 403))

    # F01/F02 — não-admin não escala papel
    def test_barber_cannot_create_admin(self):
        auth(self.client, self.barber)
        r = self.client.post('/api/v1/users/', {'username': 'nova', 'password': 'Zxq!91aa', 'role': 'admin', 'first_name': 'N'})
        self.assertEqual(r.status_code, 403)
        self.assertFalse(User.objects.filter(username='nova').exists())

    # Barbeiro que também é admin: visão de admin + continua agendável
    def test_admin_barber_is_bookable_and_has_admin_view(self):
        owner = User.objects.create_user('dono', password='Sup3rSenha!', role='admin', is_bookable=True)
        auth(self.client, owner)
        # aparece na lista pública de profissionais
        r = self.client.get('/api/v1/users/?role=barber')
        ids = {u['id'] for u in r.json()}
        self.assertIn(owner.id, ids)
        # tem visão de admin (relatório financeiro)
        self.assertEqual(self.client.get('/api/v1/financial-summary/').status_code, 200)
        # enxerga a agenda inteira (não só a própria)
        appt_ids = {a['id'] for a in self.client.get('/api/v1/appointments/').json()}
        self.assertEqual(appt_ids, {self.appt_b1.id, self.appt_b2.id})

    def test_barber_role_always_bookable(self):
        b = User.objects.create_user('b3', password='Sup3rSenha!', role='barber')
        self.assertTrue(User.objects.get(pk=b.pk).is_bookable)

    def test_non_admin_cannot_set_is_bookable(self):
        auth(self.client, self.barber)
        r = self.client.patch(f'/api/v1/users/{self.client_u.id}/', {'is_bookable': True})
        self.client_u.refresh_from_db()
        self.assertFalse(self.client_u.is_bookable)

    def test_staff_can_create_client(self):
        auth(self.client, self.barber)
        r = self.client.post('/api/v1/users/', {'username': 'novocli', 'first_name': 'C', 'role': 'client', 'phone': '11888887777'})
        self.assertIn(r.status_code, (200, 201))
        self.assertEqual(User.objects.get(username='novocli').role, 'client')

    def test_barber_cannot_promote_self(self):
        auth(self.client, self.barber)
        r = self.client.patch(f'/api/v1/users/{self.barber.id}/', {'role': 'admin'})
        self.barber.refresh_from_db()
        self.assertEqual(self.barber.role, 'barber')

    def test_barber_cannot_reset_other_password(self):
        auth(self.client, self.barber)
        r = self.client.patch(f'/api/v1/users/{self.admin.id}/', {'password': 'hackedpass1!'})
        self.assertEqual(r.status_code, 403)

    def test_only_admin_deletes_users(self):
        auth(self.client, self.barber)
        self.assertEqual(self.client.delete(f'/api/v1/users/{self.client_u.id}/').status_code, 403)
        auth(self.client, self.admin)
        self.assertIn(self.client.delete(f'/api/v1/users/{self.client_u.id}/').status_code, (204, 200))

    # F16 — check_phone slim
    def test_check_phone_is_slim(self):
        r = self.client.get('/api/v1/users/check_phone/?phone=11999990000')
        self.assertEqual(r.status_code, 200)
        self.assertNotIn('internal_notes', r.json().get('user', {}))
        self.assertNotIn('email', r.json().get('user', {}))

    def test_public_registration_requires_and_saves_valid_email(self):
        base = {
            'name': 'Cliente Portal', 'phone': '11877776666',
            'birth_date': '1995-05-20',
        }
        self.assertEqual(self.client.post('/api/v1/users/register_client/', base, format='json').status_code, 400)
        invalid = {**base, 'email': 'email-invalido'}
        self.assertEqual(self.client.post('/api/v1/users/register_client/', invalid, format='json').status_code, 400)

        valid = {**base, 'email': 'cliente.portal@example.com'}
        created = self.client.post('/api/v1/users/register_client/', valid, format='json')

        self.assertEqual(created.status_code, 200)
        self.assertTrue(created.data['has_email'])
        self.assertEqual(User.objects.get(phone='11877776666').email, 'cliente.portal@example.com')
        lookup = self.client.get('/api/v1/users/check_phone/?phone=11877776666')
        self.assertTrue(lookup.data['user']['has_email'])
        self.assertNotIn('email', lookup.data['user'])

    # F05 — isolamento de agenda por barbeiro
    def test_barber_only_sees_own_appointments(self):
        auth(self.client, self.barber)
        r = self.client.get('/api/v1/appointments/')
        ids = {a['id'] for a in r.json()}
        self.assertIn(self.appt_b1.id, ids)
        self.assertNotIn(self.appt_b2.id, ids)

    # F06 — export_csv só admin
    def test_export_csv_admin_only(self):
        auth(self.client, self.barber)
        self.assertEqual(self.client.get('/api/v1/appointments/export_csv/').status_code, 403)
        auth(self.client, self.admin)
        self.assertEqual(self.client.get('/api/v1/appointments/export_csv/').status_code, 200)

    # F07 — relatórios financeiros só admin
    def test_financial_reports_admin_only(self):
        auth(self.client, self.barber)
        for url in ('/api/v1/financial-summary/', '/api/v1/client-spending/'):
            self.assertEqual(self.client.get(url).status_code, 403, url)

    # F09/F10 — portal exige token assinado
    def test_public_list_requires_token(self):
        self.assertEqual(self.client.get('/api/v1/appointments/public_list/?phone=11999990000').status_code, 403)
        tok = make_portal_token(self.client_u.id)
        r = self.client.get(f'/api/v1/appointments/public_list/?token={tok}')
        self.assertEqual(r.status_code, 200)

    def test_public_cancel_requires_owner_token(self):
        other = make_portal_token(self.barber.id)  # token de outro usuário
        r = self.client.post(f'/api/v1/appointments/{self.appt_b1.id}/public_cancel/', {'token': other})
        self.assertEqual(r.status_code, 403)
        self.appt_b1.refresh_from_db()
        self.assertNotEqual(self.appt_b1.status, 'cancelled')

    # F11 — escrita de working-hours restrita
    def test_working_hours_write_scoped(self):
        auth(self.client, self.barber)
        r = self.client.post('/api/v1/working-hours/', {'barber': self.barber2.id, 'day_of_week': 1,
                                                        'start_time': '09:00', 'end_time': '18:00'})
        self.assertEqual(r.status_code, 403)

    # F17 — serviços/produtos escrita só admin
    def test_service_write_admin_only(self):
        auth(self.client, self.barber)
        self.assertEqual(self.client.post('/api/v1/services/', {'name': 'X', 'price': '10', 'duration_minutes': 20}).status_code, 403)

    # F08 — expenses só admin
    def test_expense_admin_only(self):
        auth(self.client, self.barber)
        self.assertEqual(self.client.get('/api/v1/expenses/').status_code, 403)

    # F26 — troca de senha exige senha atual
    def test_change_password_requires_current(self):
        auth(self.client, self.barber)
        r = self.client.post('/api/v1/users/change_password/', {'new_password': 'novaSenha123!'})
        self.assertEqual(r.status_code, 400)
        r = self.client.post('/api/v1/users/change_password/', {'current_password': 'Sup3rSenha!', 'new_password': 'novaSenha123!'})
        self.assertEqual(r.status_code, 200)


import datetime as _dt
from unittest.mock import patch
from .models import WorkingHour, BookingPaymentConfig

MON = timezone.make_aware(_dt.datetime(2026, 9, 7, 14, 0))  # segunda-feira


class DepositCheckoutTests(IsolatedCacheAPITestCase):
    """Concluir atendimento com entrada já paga pela InfinitePay."""

    def setUp(self):
        super().setUp()
        self.admin = User.objects.create_user('dep_admin', password='Sup3rSenha!', role='admin')
        self.customer = User.objects.create_user('dep_customer', password='Sup3rSenha!', role='client')
        self.appointment = Appointment.objects.create(
            client=self.customer,
            barber=self.admin,
            date_time=D1,
            total_price='20.00',
            status='confirmed',
            payment_status='paid',
            payment_amount_cents=600,
            payment_capture_method='pix',
            payment_confirmed_at=D1,
        )
        Payment.objects.create(appointment=self.appointment, method='pix', amount='6.00', payment_date=D1.date())
        auth(self.client, self.admin)

    def _complete(self, payments):
        return self.client.post(
            f'/api/v1/appointments/{self.appointment.id}/complete_with_payments/',
            {'payments': payments, 'discount': '0', 'tip': '0'},
            format='json',
            secure=True,
        )

    def test_exposes_online_paid_amount(self):
        r = self.client.get(f'/api/v1/appointments/{self.appointment.id}/', secure=True)
        self.assertEqual(r.data['online_paid_amount'], '6.00')
        self.assertEqual(r.data['remaining_amount'], '14.00')

    def test_only_remaining_is_charged_and_deposit_is_kept(self):
        r = self._complete([{'method': 'cash', 'amount': '14.00'}])
        self.assertEqual(r.status_code, 200)

        amounts = sorted((p.method, str(p.amount)) for p in self.appointment.payments.all())
        self.assertEqual(amounts, [('cash', '14.00'), ('pix', '6.00')])
        debts = self.client.get('/api/v1/debts/', secure=True)
        self.assertFalse(any(d['id'] == self.appointment.id and d['type'] == 'appointment' for d in debts.data))

    def test_charging_full_price_again_is_rejected(self):
        r = self._complete([{'method': 'pix', 'amount': '20.00'}])
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.appointment.payments.count(), 1)


class InfinitePayBookingTests(IsolatedCacheAPITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.barber = User.objects.create_user('barb_ip', password='Sup3rSenha!', role='barber',
                                              first_name='Rick', phone='11988887777')
        WorkingHour.objects.create(barber=cls.barber, day_of_week=0,
                                   start_time='09:00', end_time='18:00', is_active=True)
        cls.svc = Service.objects.create(name='Corte', price=50, duration_minutes=30)

    def setUp(self):
        super().setUp()
        BookingPaymentConfig.objects.all().delete()

    def _enable(self, handle='barbearia', days=None, deposit_percentage=100):
        cfg = BookingPaymentConfig.load()
        cfg.payment_days = days if days is not None else [0]  # MON = segunda (weekday 0)
        cfg.infinitepay_handle = handle
        cfg.deposit_percentage = deposit_percentage
        cfg.save()
        return cfg

    def _booking_payload(self, when=MON):
        return {
            'name': 'Cliente Teste', 'phone': '11999990000',
            'email': 'cliente@example.com',
            'services_ids': [self.svc.id], 'barber_id': self.barber.id,
            'date_time': when.isoformat(),
        }

    def test_config_hides_handle_from_anonymous(self):
        self._enable()
        r = self.client.get('/api/v1/booking-config/')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(set(r.data.keys()), {'payment_days', 'deposit_percentage'})
        self.assertEqual(r.data['payment_days'], [0])
        self.assertEqual(r.data['deposit_percentage'], 100)

    def test_config_hides_days_without_handle(self):
        cfg = BookingPaymentConfig.load()
        cfg.payment_days = [0, 1]
        cfg.infinitepay_handle = ''
        cfg.save()
        # Neutraliza o fallback de env (INFINITEPAY_HANDLE pode estar setado no .env local).
        with patch.dict('os.environ', {'INFINITEPAY_HANDLE': ''}):
            r = self.client.get('/api/v1/booking-config/')
        self.assertEqual(r.data['payment_days'], [])

    def test_config_patch_admin_only(self):
        admin = User.objects.create_user('adm_ip', password='Sup3rSenha!', role='admin')
        self.assertIn(self.client.patch('/api/v1/booking-config/', {'payment_days': [0]}).status_code, (401, 403))
        auth(self.client, admin)
        r = self.client.patch('/api/v1/booking-config/', {'payment_days': [4, 5], 'infinitepay_handle': 'x'}, format='json')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(BookingPaymentConfig.load().active_days(), [4, 5])

    def test_config_rejects_invalid_days(self):
        admin = User.objects.create_user('adm_bad', password='Sup3rSenha!', role='admin')
        auth(self.client, admin)
        r = self.client.patch('/api/v1/booking-config/', {'payment_days': [7]}, format='json')
        self.assertEqual(r.status_code, 400)

    def test_config_rejects_invalid_deposit_percentage(self):
        admin = User.objects.create_user('adm_pct', password='Sup3rSenha!', role='admin')
        auth(self.client, admin)
        for value in (0, 101):
            r = self.client.patch('/api/v1/booking-config/', {'deposit_percentage': value}, format='json')
            self.assertEqual(r.status_code, 400)

    def test_disabled_keeps_legacy_flow(self):
        r = self.client.post('/api/v1/appointments/public_booking/', self._booking_payload(), format='json')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data['status'], 'confirmed')

    def test_day_not_enabled_keeps_legacy_flow(self):
        # Handle configurado, mas só terça (1) exige pagamento; o agendamento é numa segunda (MON).
        self._enable(days=[1])
        r = self.client.post('/api/v1/appointments/public_booking/', self._booking_payload(), format='json')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data['status'], 'confirmed')
        self.assertNotIn('payment_required', r.data)

    @patch('api.infinitepay.create_link', return_value=('https://pay.infinitepay.io/abc', {}))
    def test_payment_required_returns_checkout(self, mock_link):
        self._enable()
        r = self.client.post('/api/v1/appointments/public_booking/', self._booking_payload(), format='json')
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.data['payment_required'])
        self.assertEqual(r.data['checkout_url'], 'https://pay.infinitepay.io/abc')
        appt = Appointment.objects.get(id=r.data['appointment_id'])
        self.assertEqual(appt.status, 'pending')
        self.assertEqual(appt.payment_status, 'pending')
        self.assertEqual(appt.payment_amount_cents, 5000)
        mock_link.assert_called_once()
        self.assertEqual(mock_link.call_args.kwargs['customer']['email'], 'cliente@example.com')

    @patch('api.infinitepay.create_link', return_value=('https://pay.infinitepay.io/abc', {}))
    def test_checkout_only_sends_valid_customer_email(self, mock_link):
        self._enable()
        User.objects.create_user(
            'cliente_email', role='client', phone='11999990000',
            first_name='Cliente', email='cliente@example.com',
        )
        payload = self._booking_payload()
        payload.pop('email')

        r = self.client.post('/api/v1/appointments/public_booking/', payload, format='json')

        self.assertEqual(r.status_code, 200)
        self.assertEqual(mock_link.call_args.kwargs['customer']['email'], 'cliente@example.com')

    def test_public_booking_rejects_invalid_email(self):
        payload = self._booking_payload()
        payload['email'] = 'email-invalido'

        r = self.client.post('/api/v1/appointments/public_booking/', payload, format='json')

        self.assertEqual(r.status_code, 400)
        self.assertIn('e-mail válido', r.data['error'])
        self.assertFalse(Appointment.objects.exists())

    @patch('api.infinitepay.create_link', return_value=('https://pay.infinitepay.io/entrada', {}))
    def test_percentage_charges_only_deposit_and_keeps_remaining_balance(self, mock_link):
        self._enable(deposit_percentage=30)
        r = self.client.post('/api/v1/appointments/public_booking/', self._booking_payload(), format='json')

        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data['deposit_percentage'], 30)
        self.assertEqual(r.data['payment_amount'], '15.00')
        self.assertEqual(r.data['remaining_amount'], '35.00')
        appt = Appointment.objects.get(id=r.data['appointment_id'])
        self.assertEqual(appt.total_price, 50)
        self.assertEqual(appt.payment_amount_cents, 1500)
        self.assertEqual(mock_link.call_args.kwargs['items'][0]['price'], 1500)

        with patch('api.infinitepay.check_payment', return_value={'paid': True, 'paid_amount': 1500, 'capture_method': 'pix'}):
            confirm = self.client.post('/api/v1/appointments/payment_confirm/', {'order_nsu': r.data['order_nsu']}, format='json')
        self.assertTrue(confirm.data['paid'])
        self.assertEqual(confirm.data['appointment']['paid_amount'], '15.00')
        self.assertEqual(confirm.data['appointment']['remaining_amount'], '35.00')
        appt.refresh_from_db()
        self.assertEqual(appt.payments.first().amount, 15)

    @patch('api.infinitepay.create_link')
    def test_payment_checkout_accepts_long_provider_url(self, mock_link):
        long_url = 'https://pay.infinitepay.io/checkout?' + ('token=' + 'a' * 400)
        mock_link.return_value = (long_url, {})
        self._enable()

        r = self.client.post('/api/v1/appointments/public_booking/', self._booking_payload(), format='json')

        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data['checkout_url'], long_url)
        self.assertEqual(Appointment.objects.get(id=r.data['appointment_id']).payment_checkout_url, long_url)

    @patch('api.infinitepay.create_link', return_value=('https://pay.infinitepay.io/abc', {}))
    def test_checkout_save_failure_releases_reserved_slot(self, _mock_link):
        self._enable()
        original_save = Appointment.save

        def fail_checkout_save(instance, *args, **kwargs):
            if 'payment_checkout_url' in (kwargs.get('update_fields') or []):
                raise ValueError('falha simulada ao persistir checkout')
            return original_save(instance, *args, **kwargs)

        with patch.object(Appointment, 'save', autospec=True, side_effect=fail_checkout_save):
            r = self.client.post('/api/v1/appointments/public_booking/', self._booking_payload(), format='json')

        self.assertEqual(r.status_code, 502)
        appointment = Appointment.objects.get()
        self.assertEqual(appointment.status, 'cancelled')

        # Um novo agendamento para o mesmo horário não encontra conflito.
        r2 = self.client.post('/api/v1/appointments/public_booking/', self._booking_payload(), format='json')
        self.assertEqual(r2.status_code, 200)

    @patch('api.infinitepay.check_payment', return_value={'paid': True, 'paid_amount': 5000, 'capture_method': 'pix'})
    @patch('api.infinitepay.create_link', return_value=('https://pay.infinitepay.io/abc', {}))
    def test_payment_confirm_settles_and_is_idempotent(self, mock_link, mock_check):
        self._enable()
        book = self.client.post('/api/v1/appointments/public_booking/', self._booking_payload(), format='json')
        nsu = book.data['order_nsu']

        r = self.client.post('/api/v1/appointments/payment_confirm/', {'order_nsu': nsu}, format='json')
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.data['paid'])
        appt = Appointment.objects.get(payment_order_nsu=nsu)
        self.assertEqual(appt.status, 'confirmed')
        self.assertEqual(appt.payment_status, 'paid')
        self.assertEqual(appt.payment_capture_method, 'pix')
        # Lança o pagamento para a equipe / financeiro.
        self.assertEqual(appt.payments.count(), 1)
        pay = appt.payments.first()
        self.assertEqual(pay.method, 'pix')
        self.assertEqual(float(pay.amount), 50.0)

        r2 = self.client.post('/api/v1/appointments/payment_confirm/', {'order_nsu': nsu}, format='json')
        self.assertTrue(r2.data['paid'])
        self.assertEqual(mock_check.call_count, 1)  # não reconsulta após pago
        self.assertEqual(appt.payments.count(), 1)  # não duplica

        # Concluir o atendimento mantém o pagamento online sem duplicá-lo: a tela
        # envia apenas o que foi recebido no balcão (nada, neste caso).
        admin = User.objects.create_user('adm_done', password='Sup3rSenha!', role='admin')
        auth(self.client, admin)
        r3 = self.client.post(f'/api/v1/appointments/{appt.id}/complete_with_payments/',
                              {'payments': [], 'discount': 0, 'tip': 0},
                              format='json')
        self.assertEqual(r3.status_code, 200)
        appt.refresh_from_db()
        self.assertEqual(appt.status, 'completed')
        self.assertEqual(appt.payments.count(), 1)

    @patch('api.infinitepay.check_payment', return_value={'paid': False})
    @patch('api.infinitepay.create_link', return_value=('https://pay.infinitepay.io/abc', {}))
    def test_payment_confirm_unpaid_stays_pending(self, mock_link, mock_check):
        self._enable()
        book = self.client.post('/api/v1/appointments/public_booking/', self._booking_payload(), format='json')
        r = self.client.post('/api/v1/appointments/payment_confirm/', {'order_nsu': book.data['order_nsu']}, format='json')
        self.assertFalse(r.data['paid'])
        self.assertEqual(Appointment.objects.get(payment_order_nsu=book.data['order_nsu']).status, 'pending')

    @patch('api.infinitepay.create_link', return_value=('https://pay.infinitepay.io/abc', {}))
    def test_late_payment_does_not_double_book_when_slot_taken(self, mock_link):
        """Pagamento chega tarde (após expirar e o horário ser ocupado por outra pessoa):
        não pode reconfirmar e duplicar o horário — fica pago, mas cancelado."""
        self._enable()
        book = self.client.post('/api/v1/appointments/public_booking/', self._booking_payload(), format='json')
        nsu = book.data['order_nsu']
        appt = Appointment.objects.get(payment_order_nsu=nsu)

        # Expira (simula checkout abandonado) — o horário fica livre de novo.
        appt.status = 'cancelled'
        appt.save(update_fields=['status'])

        # Outro cliente ocupa exatamente o mesmo horário/barbeiro.
        other_client = User.objects.create_user('outro_cli', password='Sup3rSenha!', role='client', phone='11988880000')
        taken = Appointment(client=other_client, barber=self.barber, date_time=MON,
                            total_price=50, status='confirmed')
        taken._skip_notification = True
        taken.save()
        taken.services.set([self.svc])

        # O pagamento do primeiro cliente chega tarde e é confirmado.
        with patch('api.infinitepay.check_payment', return_value={'paid': True, 'paid_amount': 5000, 'capture_method': 'pix'}):
            r = self.client.post('/api/v1/appointments/payment_confirm/', {'order_nsu': nsu}, format='json')

        self.assertFalse(r.data['paid'])
        self.assertTrue(r.data['slot_lost'])
        appt.refresh_from_db()
        self.assertEqual(appt.status, 'cancelled')  # não reconfirma por cima do outro
        self.assertEqual(appt.payment_status, 'paid')  # mas o dinheiro foi registrado
        self.assertEqual(appt.payments.count(), 1)
        taken.refresh_from_db()
        self.assertEqual(taken.status, 'confirmed')  # o outro agendamento não foi tocado

    @patch('api.infinitepay.create_link', return_value=('https://pay.infinitepay.io/abc', {}))
    def test_staff_sees_internal_payment_fields_public_does_not(self, _mock_link):
        self._enable()
        book = self.client.post('/api/v1/appointments/public_booking/', self._booking_payload(), format='json')
        appt_id = Appointment.objects.get(payment_order_nsu=book.data['order_nsu']).id

        admin = User.objects.create_user('adm_fields', password='Sup3rSenha!', role='admin')
        auth(self.client, admin)
        r = self.client.get(f'/api/v1/appointments/{appt_id}/')
        self.assertIn('payment_order_nsu', r.data)
        self.assertIn('payment_checkout_url', r.data)

        self.client.credentials()  # remove auth
        client_u = Appointment.objects.get(id=appt_id).client
        token = make_portal_token(client_u.id)
        r2 = self.client.get(f'/api/v1/appointments/public_list/?token={token}')
        self.assertNotIn('payment_order_nsu', r2.data[0])
        self.assertNotIn('payment_checkout_url', r2.data[0])
        self.assertIn('payment_status', r2.data[0])  # esse continua visível

    @patch('api.infinitepay.create_link', return_value=('https://pay.infinitepay.io/abc', {}))
    def test_provider_error_does_not_cancel_and_webhook_retries(self, mock_link):
        from api.infinitepay import InfinitePayError
        self._enable()
        book = self.client.post('/api/v1/appointments/public_booking/', self._booking_payload(), format='json')
        nsu = book.data['order_nsu']

        with patch('api.infinitepay.check_payment', side_effect=InfinitePayError('down')):
            r = self.client.post('/api/v1/appointments/payment_confirm/', {'order_nsu': nsu}, format='json')
            self.assertFalse(r.data['paid'])
            self.assertTrue(r.data['provider_error'])
            # webhook devolve 400 para a InfinitePay reenviar
            w = self.client.post('/api/v1/appointments/payment_webhook/', {'order_nsu': nsu}, format='json')
            self.assertEqual(w.status_code, 400)
        self.assertEqual(Appointment.objects.get(payment_order_nsu=nsu).status, 'pending')

    @patch('api.infinitepay.create_link', return_value=('https://pay.infinitepay.io/abc', {}))
    def test_reconcile_settles_paid_and_does_not_cancel_when_provider_down(self, mock_link):
        from api.infinitepay import InfinitePayError
        from api.views import reconcile_pending_payments
        from django.utils import timezone
        import datetime as _d
        self._enable()
        nsu = self.client.post('/api/v1/appointments/public_booking/', self._booking_payload(), format='json').data['order_nsu']
        # envelhece além do TTL
        Appointment.objects.filter(payment_order_nsu=nsu).update(
            created_at=timezone.now() - _d.timedelta(minutes=40))

        # InfinitePay fora do ar -> não cancela
        with patch('api.infinitepay.check_payment', side_effect=InfinitePayError('down')):
            settled, cancelled = reconcile_pending_payments()
        self.assertEqual((settled, cancelled), (0, 0))
        self.assertEqual(Appointment.objects.get(payment_order_nsu=nsu).status, 'pending')

        # InfinitePay volta e confirma o pagamento -> concilia tardiamente
        with patch('api.infinitepay.check_payment', return_value={'paid': True, 'paid_amount': 5000, 'capture_method': 'pix'}):
            settled, cancelled = reconcile_pending_payments()
        self.assertEqual(settled, 1)
        appt = Appointment.objects.get(payment_order_nsu=nsu)
        self.assertEqual(appt.status, 'confirmed')
        self.assertEqual(appt.payments.count(), 1)

    @patch('api.infinitepay.create_link', return_value=('https://pay.infinitepay.io/abc', {}))
    def test_reconcile_cancels_unpaid_past_ttl(self, mock_link):
        from api.views import reconcile_pending_payments
        from django.utils import timezone
        import datetime as _d
        self._enable()
        nsu = self.client.post('/api/v1/appointments/public_booking/', self._booking_payload(), format='json').data['order_nsu']
        Appointment.objects.filter(payment_order_nsu=nsu).update(
            created_at=timezone.now() - _d.timedelta(minutes=40))
        with patch('api.infinitepay.check_payment', return_value={'paid': False}):
            settled, cancelled = reconcile_pending_payments()
        self.assertEqual((settled, cancelled), (0, 1))
        self.assertEqual(Appointment.objects.get(payment_order_nsu=nsu).status, 'cancelled')


from .models import SpecialPriceConfig


class SpecialPriceTests(IsolatedCacheAPITestCase):
    @classmethod
    def setUpTestData(cls):
        cls.barber = User.objects.create_user('barb_sp', password='Sup3rSenha!', role='barber',
                                              first_name='Rick', phone='11988887777')
        WorkingHour.objects.create(barber=cls.barber, day_of_week=0,
                                   start_time='09:00', end_time='18:00', is_active=True)
        cls.svc = Service.objects.create(name='Corte', price=50, duration_minutes=30, special_price=30)
        cls.svc_no_promo = Service.objects.create(name='Barba', price=20, duration_minutes=15)

    def setUp(self):
        super().setUp()
        SpecialPriceConfig.objects.all().delete()

    def _booking_payload(self, services_ids, when=MON):
        return {
            'name': 'Cliente Teste', 'phone': '11999991111',
            'email': 'cliente@example.com',
            'services_ids': services_ids, 'barber_id': self.barber.id,
            'date_time': when.isoformat(),
        }

    def test_config_public_and_admin_same_shape(self):
        cfg = SpecialPriceConfig.load()
        cfg.special_price_days = [0]
        cfg.save()
        r = self.client.get('/api/v1/special-price-config/')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data['special_price_days'], [0])

    def test_config_patch_admin_only(self):
        admin = User.objects.create_user('adm_sp', password='Sup3rSenha!', role='admin')
        self.assertIn(self.client.patch('/api/v1/special-price-config/', {'special_price_days': [0]}).status_code, (401, 403))
        auth(self.client, admin)
        r = self.client.patch('/api/v1/special-price-config/', {'special_price_days': [0, 2]}, format='json')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(SpecialPriceConfig.load().active_days(), [0, 2])

    def test_config_rejects_invalid_days(self):
        admin = User.objects.create_user('adm_sp2', password='Sup3rSenha!', role='admin')
        auth(self.client, admin)
        r = self.client.patch('/api/v1/special-price-config/', {'special_price_days': [9]}, format='json')
        self.assertEqual(r.status_code, 400)

    def test_special_day_charges_special_price(self):
        SpecialPriceConfig.objects.create(special_price_days=[0])  # MON = segunda
        r = self.client.post('/api/v1/appointments/public_booking/',
                             self._booking_payload([self.svc.id]), format='json')
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.data['special_pricing_applied'])
        self.assertEqual(r.data['total_price'], '30.00')

    def test_non_special_day_charges_normal_price(self):
        SpecialPriceConfig.objects.create(special_price_days=[1])  # só terça, não a MON usada no teste
        r = self.client.post('/api/v1/appointments/public_booking/',
                             self._booking_payload([self.svc.id]), format='json')
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.data['special_pricing_applied'])
        self.assertEqual(r.data['total_price'], '50.00')

    def test_special_day_keeps_normal_price_for_service_without_promo(self):
        SpecialPriceConfig.objects.create(special_price_days=[0])
        r = self.client.post('/api/v1/appointments/public_booking/',
                             self._booking_payload([self.svc.id, self.svc_no_promo.id]), format='json')
        self.assertEqual(r.status_code, 200)
        # Corte com promo (30) + Barba sem promo (20) = 50
        self.assertEqual(r.data['total_price'], '50.00')

    @patch('api.infinitepay.create_link', return_value=('https://pay.infinitepay.io/abc', {}))
    def test_special_price_feeds_infinitepay_amount(self, mock_link):
        SpecialPriceConfig.objects.create(special_price_days=[0])
        from .models import BookingPaymentConfig
        pay_cfg = BookingPaymentConfig.load()
        pay_cfg.payment_days = [0]
        pay_cfg.infinitepay_handle = 'barbearia'
        pay_cfg.save()

        r = self.client.post('/api/v1/appointments/public_booking/',
                             self._booking_payload([self.svc.id]), format='json')
        self.assertTrue(r.data['payment_required'])
        self.assertTrue(r.data['special_pricing_applied'])
        appt = Appointment.objects.get(id=r.data['appointment_id'])
        self.assertEqual(appt.payment_amount_cents, 3000)  # R$30, não R$50

    def test_staff_edit_and_financial_keep_special_price(self):
        SpecialPriceConfig.objects.create(special_price_days=[0])
        admin = User.objects.create_user('adm_special_flow', password='Sup3rSenha!', role='admin')
        auth(self.client, admin)

        created = self.client.post('/api/v1/appointments/', {
            'client': admin.id,
            'barber': self.barber.id,
            'services': [self.svc.id],
            'date_time': MON.isoformat(),
            'status': 'confirmed',
            'total_price': '50.00',  # cliente antigo tenta mandar o preço normal
        }, format='json')
        self.assertEqual(created.status_code, 201)
        appointment = Appointment.objects.get(pk=created.data['id'])
        self.assertEqual(appointment.total_price, 30)

        edited = self.client.patch(f'/api/v1/appointments/{appointment.id}/', {
            'services': [self.svc.id],
            'total_price': '50.00',
        }, format='json')
        self.assertEqual(edited.status_code, 200)
        appointment.refresh_from_db()
        self.assertEqual(appointment.total_price, 30)

        Payment.objects.create(appointment=appointment, method='pix', amount=appointment.total_price)
        financial = self.client.get('/api/v1/financial-summary/?start_date=2026-09-07&end_date=2026-09-07')
        self.assertEqual(financial.status_code, 200)
        self.assertEqual(financial.data['service_revenue'], 30.0)
        self.assertEqual(financial.data['total_revenue'], 30.0)
