from datetime import timedelta
from unittest.mock import patch

from django.core.management import call_command
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
from .whatsapp_service import _render_template


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
        }, format='json')

        self.assertEqual(response.status_code, 200)
        config = MessageAutomationConfig.load()
        self.assertEqual(config.follow_up_template, 'Olá {nome}, como você está?')
        self.assertEqual(config.google_review_url, 'https://g.page/r/exemplo/review')

    def test_only_admin_can_read_configuration(self):
        self.client.force_authenticate(self.professional)
        response = self.client.get('/api/v1/message-automation-config/')
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
            'Oi {nome}: {servico}, {profissional}, {data}, {link_agendamento}, {link_avaliacao}',
            appointment,
            review_url='https://google.test/review',
        )

        self.assertIn('Oi Ana: Depilação, Milly', message)
        self.assertIn('https://google.test/review', message)
        self.assertNotIn('{nome}', message)

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
