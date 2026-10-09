from datetime import timedelta
from unittest.mock import MagicMock, patch

import requests
from django.core.management import call_command
from django.db import transaction
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from .models import (
    Appointment,
    MessageAutomationConfig,
    Notification,
    Service,
    User,
)
from .whatsapp_service import WhatsAppService, _render_template


class MessageAutomationConfigApiTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username='admin-auto', password='test-pass', role='admin')
        self.professional = User.objects.create_user(username='pro-auto', password='test-pass', role='barber')
        self.client = APIClient()

    def test_admin_can_update_templates(self):
        self.client.force_authenticate(self.admin)
        response = self.client.patch('/api/v1/message-automation-config/', {
            'follow_up_template': 'Olá {nome}, como você está?',
            'google_review_url': 'https://g.page/r/exemplo/review',
        }, format='json', secure=True)

        self.assertEqual(response.status_code, 200)
        config = MessageAutomationConfig.load()
        self.assertEqual(config.follow_up_template, 'Olá {nome}, como você está?')
        self.assertEqual(config.google_review_url, 'https://g.page/r/exemplo/review')

    def test_only_admin_can_read_configuration(self):
        self.client.force_authenticate(self.professional)
        response = self.client.get('/api/v1/message-automation-config/', secure=True)
        self.assertEqual(response.status_code, 403)


class MessageAutomationScheduleTests(TestCase):
    def setUp(self):
        self.client_user = User.objects.create_user(
            username='cliente-auto', first_name='Ana', phone='84999999999', role='client'
        )
        self.professional = User.objects.create_user(
            username='milly-auto', first_name='Milly', role='barber'
        )
        self.service = Service.objects.create(
            name='Depilação', price='50.00', duration_minutes=30
        )
        self.now = timezone.now()
        self.config = MessageAutomationConfig.load()
        self.config.google_review_url = 'https://g.page/r/exemplo/review'
        activation = self.now - timedelta(days=60)
        self.config.follow_up_enabled_at = activation
        self.config.review_enabled_at = activation
        self.config.return_enabled_at = activation
        self.config.save()

    def _appointment(self, days_ago, appointment_days_ago):
        appointment = Appointment.objects.create(
            client=self.client_user,
            barber=self.professional,
            date_time=self.now - timedelta(days=appointment_days_ago),
            status='completed',
            total_price='50.00',
        )
        appointment.services.add(self.service)
        Appointment.objects.filter(pk=appointment.pk).update(
            completed_at=self.now - timedelta(days=days_ago)
        )
        appointment.refresh_from_db()
        return appointment

    @staticmethod
    def _record_sent(appointment, notification_type):
        Notification.objects.create(
            appointment=appointment,
            type=notification_type,
            message=notification_type,
            status='sent',
            sent_at=timezone.now(),
        )
        return True

    def test_sends_three_due_messages_and_does_not_duplicate(self):
        appointment = self._appointment(days_ago=31, appointment_days_ago=31)

        with (
            patch('api.management.commands.send_reminders.WhatsAppService.send_follow_up',
                  side_effect=lambda app, template: self._record_sent(app, 'follow_up')),
            patch('api.management.commands.send_reminders.WhatsAppService.send_review_request',
                  side_effect=lambda app, template, url: self._record_sent(app, 'review_request')),
            patch('api.management.commands.send_reminders.WhatsAppService.send_return_reminder',
                  side_effect=lambda app, template: self._record_sent(app, 'return_reminder')),
        ):
            call_command('send_reminders')
            call_command('send_reminders')

        automation_notifications = Notification.objects.filter(
            appointment=appointment,
            type__in=('follow_up', 'review_request', 'return_reminder'),
        )
        self.assertEqual(
            set(automation_notifications.values_list('type', flat=True)),
            {'follow_up', 'review_request', 'return_reminder'},
        )
        self.assertEqual(automation_notifications.count(), 3)

    def test_review_is_only_sent_for_first_completed_procedure(self):
        self._appointment(days_ago=10, appointment_days_ago=12)
        second = self._appointment(days_ago=3, appointment_days_ago=3)

        with (
            patch('api.management.commands.send_reminders.WhatsAppService.send_follow_up', return_value=True),
            patch('api.management.commands.send_reminders.WhatsAppService.send_review_request') as review,
            patch('api.management.commands.send_reminders.WhatsAppService.send_return_reminder', return_value=True),
        ):
            call_command('send_reminders')

        reviewed_ids = [call.args[0].id for call in review.call_args_list]
        self.assertNotIn(second.id, reviewed_ids)

    def test_template_variables_are_replaced(self):
        appointment = self._appointment(days_ago=2, appointment_days_ago=2)
        message = _render_template(
            'Oi {nome}: {servico}, {profissional}, {data} {hora}, {link_agendamento}, {link_gerenciamento}, {link_avaliacao}',
            appointment,
            review_url='https://google.test/review',
        )

        self.assertIn('Oi Ana: Depilação, Milly', message)
        self.assertIn(timezone.localtime(appointment.date_time).strftime('%H:%M'), message)
        self.assertIn('/meus-agendamentos?token=', message)
        self.assertIn('https://google.test/review', message)
        self.assertNotIn('{nome}', message)

    def test_operational_messages_use_editable_templates_and_distinct_types(self):
        appointment = self._appointment(days_ago=1, appointment_days_ago=1)
        self.config.confirmation_template = 'CONF {nome} {hora}'
        self.config.appointment_reminder_template = 'LEMBRETE {servico}'
        self.config.cancellation_template = 'CANCELOU {link_agendamento}'
        self.config.thank_you_template = 'OBRIGADO {nome}'
        self.config.save()

        with patch('api.whatsapp_service.WhatsAppService.send_message', return_value=True) as send:
            WhatsAppService.send_confirmation(appointment)
            WhatsAppService.send_reminder(appointment)
            WhatsAppService.send_cancellation(appointment)
            WhatsAppService.send_post_visit(appointment)

        self.assertEqual(
            [call.args[1] for call in send.call_args_list],
            ['confirmation', 'reminder', 'cancellation', 'thank_you'],
        )
        self.assertIn('CONF Ana', send.call_args_list[0].args[2])
        self.assertIn('LEMBRETE Depilação', send.call_args_list[1].args[2])
        self.assertIn('/agendar', send.call_args_list[2].args[2])
        self.assertEqual(send.call_args_list[3].args[2], 'OBRIGADO Ana')

    def test_disabled_operational_message_is_not_sent(self):
        appointment = self._appointment(days_ago=1, appointment_days_ago=1)
        self.config.confirmation_enabled = False
        self.config.save()

        with patch('api.whatsapp_service.WhatsAppService.send_message') as send:
            self.assertTrue(WhatsAppService.send_confirmation(appointment))
        send.assert_not_called()

    def test_completion_timestamp_tracks_status(self):
        appointment = Appointment.objects.create(
            client=self.client_user,
            barber=self.professional,
            date_time=self.now,
            status='confirmed',
        )
        self.assertIsNone(appointment.completed_at)

        appointment.status = 'completed'
        appointment.save(update_fields=['status'])
        self.assertIsNotNone(appointment.completed_at)

        appointment.status = 'confirmed'
        appointment.save(update_fields=['status'])
        self.assertIsNone(appointment.completed_at)


WHATSAPP_ENV = {
    'WHATSAPP_API_URL': 'https://evolution.test',
    'WHATSAPP_API_KEY': 'key',
    'WHATSAPP_INSTANCE': 'milly',
}


def _api_response(status_code):
    return MagicMock(status_code=status_code, text="")


@patch.dict('os.environ', WHATSAPP_ENV)
class DuplicateMessageProtectionTests(TestCase):
    def setUp(self):
        self.client_user = User.objects.create_user(
            username='cliente-dup', first_name='Bia', phone='84988887777', role='client'
        )
        self.professional = User.objects.create_user(
            username='milly-dup', first_name='Milly', role='barber'
        )
        self.service = Service.objects.create(name='Depilação', price='50.00', duration_minutes=30)
        self.now = timezone.now()

    def _appointment(self, status='confirmed', date_time=None):
        appointment = Appointment(
            client=self.client_user,
            barber=self.professional,
            date_time=date_time or self.now + timedelta(days=1),
            status=status,
            total_price='50.00',
        )
        appointment._skip_notification = True
        appointment.save()
        appointment.services.add(self.service)
        return Appointment.objects.get(pk=appointment.pk)

    def test_same_message_is_sent_only_once(self):
        appointment = self._appointment()
        with patch('api.whatsapp_service.requests.post', return_value=_api_response(200)) as post:
            self.assertTrue(WhatsAppService.send_confirmation(appointment))
            self.assertIsNone(WhatsAppService.send_confirmation(appointment))
        self.assertEqual(post.call_count, 1)

    def test_timeout_is_never_resent(self):
        appointment = self._appointment()
        with patch('api.whatsapp_service.requests.post', side_effect=requests.exceptions.ReadTimeout) as post:
            self.assertFalse(WhatsAppService.send_confirmation(appointment))
            self.assertIsNone(WhatsAppService.send_confirmation(appointment))
        self.assertEqual(post.call_count, 1)
        self.assertEqual(Notification.objects.get(appointment=appointment).status, 'pending')

    def test_api_error_is_retried_a_limited_number_of_times(self):
        appointment = self._appointment()
        with patch('api.whatsapp_service.requests.post', return_value=_api_response(500)) as post:
            for _ in range(4):
                WhatsAppService.send_confirmation(appointment)
        self.assertEqual(post.call_count, 2)

    def test_editing_without_status_change_does_not_resend(self):
        appointment = self._appointment()
        with patch('api.whatsapp_service.requests.post', return_value=_api_response(500)) as post:
            with self.captureOnCommitCallbacks(execute=True):
                appointment.status = 'completed'
                appointment.save()
            with self.captureOnCommitCallbacks(execute=True):
                appointment = Appointment.objects.get(pk=appointment.pk)
                appointment.discount = 10
                appointment.save(update_fields=['discount'])
        self.assertEqual(post.call_count, 1)

    def test_rolled_back_transaction_sends_nothing(self):
        appointment = self._appointment()
        with patch('api.whatsapp_service.requests.post', return_value=_api_response(200)) as post:
            with self.captureOnCommitCallbacks(execute=True):
                try:
                    with transaction.atomic():
                        appointment.status = 'cancelled'
                        appointment.save()
                        raise RuntimeError('falha no meio do cancelamento')
                except RuntimeError:
                    pass
        post.assert_not_called()

    def test_return_reminder_skipped_when_client_already_came_back(self):
        config = MessageAutomationConfig.load()
        config.return_enabled_at = self.now - timedelta(days=90)
        config.follow_up_enabled = False
        config.review_enabled = False
        config.save()
        old = self._appointment(status='completed', date_time=self.now - timedelta(days=40))
        Appointment.objects.filter(pk=old.pk).update(completed_at=self.now - timedelta(days=40))
        self._appointment(status='confirmed', date_time=self.now + timedelta(days=2))

        with patch('api.management.commands.send_reminders.WhatsAppService.send_return_reminder') as send:
            call_command('send_reminders')
        send.assert_not_called()

    def test_review_requested_once_for_simultaneous_first_appointments(self):
        config = MessageAutomationConfig.load()
        config.google_review_url = 'https://g.page/r/exemplo/review'
        config.review_enabled_at = self.now - timedelta(days=90)
        config.follow_up_enabled = False
        config.return_enabled = False
        config.save()
        same_time = self.now - timedelta(days=3)
        for _ in range(2):
            appointment = self._appointment(status='completed', date_time=same_time)
            Appointment.objects.filter(pk=appointment.pk).update(completed_at=same_time)

        with patch('api.whatsapp_service.requests.post', return_value=_api_response(200)) as post:
            call_command('send_reminders')
            call_command('send_reminders')
        self.assertEqual(post.call_count, 1)
