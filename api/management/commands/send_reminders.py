from django.core.management.base import BaseCommand
from django.utils import timezone
from datetime import timedelta
from api.models import Appointment, Notification, MessageAutomationConfig
from api.whatsapp_service import WhatsAppService

class Command(BaseCommand):
    help = 'Envia lembretes de agendamento e mensagens pós-atendimento via WhatsApp'

    def _due_appointments(self, now, delay, enabled_at, notification_type):
        return (
            Appointment.objects.filter(
                status='completed',
                completed_at__isnull=False,
                completed_at__gte=enabled_at,
                completed_at__lte=now - delay,
            )
            .exclude(notifications__type=notification_type, notifications__status='sent')
            .select_related('client', 'barber')
            .prefetch_related('services')
            .order_by('completed_at')
        )

    def _send_post_service_automations(self, now):
        cfg = MessageAutomationConfig.load()
        sent_count = 0

        automations = []
        if cfg.follow_up_enabled:
            automations.append((
                'follow_up', timedelta(hours=24), cfg.follow_up_enabled_at,
                lambda app: WhatsAppService.send_follow_up(app, cfg.follow_up_template),
            ))
        if cfg.review_enabled and cfg.google_review_url.strip():
            automations.append((
                'review_request', timedelta(hours=48), cfg.review_enabled_at,
                lambda app: WhatsAppService.send_review_request(
                    app, cfg.review_template, cfg.google_review_url.strip()
                ),
            ))
        if cfg.return_enabled:
            automations.append((
                'return_reminder', timedelta(days=30), cfg.return_enabled_at,
                lambda app: WhatsAppService.send_return_reminder(app, cfg.return_template),
            ))

        for notification_type, delay, enabled_at, sender in automations:
            for app in self._due_appointments(now, delay, enabled_at, notification_type):
                if notification_type == 'review_request':
                    has_previous_procedure = Appointment.objects.filter(
                        client_id=app.client_id,
                        status='completed',
                        date_time__lt=app.date_time,
                    ).exclude(pk=app.pk).exists()
                    if has_previous_procedure:
                        continue

                if sender(app):
                    sent_count += 1
                    self.stdout.write(self.style.SUCCESS(
                        f'{notification_type} enviado para {app.client.first_name}'
                    ))
                else:
                    self.stdout.write(self.style.ERROR(
                        f'Falha em {notification_type} para {app.client.first_name}'
                    ))
        return sent_count

    def handle(self, *args, **options):
        # Realizar backup apenas uma vez por dia, entre 03:00 e 03:15 da manhã
        from django.utils import timezone
        now_local = timezone.localtime()
        
        if now_local.hour == 3 and now_local.minute < 15:
            from django.core.management import call_command
            try:
                call_command('backup_db')
            except Exception as e:
                self.stdout.write(self.style.ERROR(f'Falha no backup automático: {e}'))

        # Conciliação de segurança dos pagamentos InfinitePay: confirma os que foram
        # pagos mas não conciliaram (webhook/retorno falharam) e libera os abandonados.
        try:
            from api.views import reconcile_pending_payments, expire_stale_pending_payments
            settled, cancelled = reconcile_pending_payments()
            if settled:
                self.stdout.write(self.style.SUCCESS(f'{settled} pagamento(s) InfinitePay conciliados tardiamente.'))
            if cancelled:
                self.stdout.write(self.style.WARNING(f'{cancelled} agendamento(s) sem pagamento cancelados.'))
            expire_stale_pending_payments()
        except Exception as e:
            self.stdout.write(self.style.ERROR(f'Falha na conciliação de pagamentos pendentes: {e}'))

        now = timezone.now()
        # Buscar agendamentos confirmados para a próxima 1 hora
        upcoming_limit = now + timedelta(hours=1)
        
        appointments = Appointment.objects.filter(
            status='confirmed',
            date_time__gte=now,
            date_time__lte=upcoming_limit
        )

        sent_count = 0
        for app in appointments:
            # Verificar se já enviamos lembrete para este agendamento
            already_sent = Notification.objects.filter(
                appointment=app,
                type='reminder',
                status='sent'
            ).exists()

            if not already_sent:
                success = WhatsAppService.send_reminder(app)
                if success:
                    sent_count += 1
                    self.stdout.write(self.style.SUCCESS(f'Lembrete enviado para {app.client.first_name}'))
                else:
                    self.stdout.write(self.style.ERROR(f'Falha ao enviar para {app.client.first_name}'))

        post_service_count = self._send_post_service_automations(now)
        self.stdout.write(
            f'Processo concluído. Lembretes enviados: {sent_count}. '
            f'Mensagens pós-atendimento: {post_service_count}.'
        )
