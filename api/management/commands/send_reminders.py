from django.core.management.base import BaseCommand
from django.utils import timezone
from datetime import timedelta
from api.models import Appointment, Notification
from api.whatsapp_service import WhatsAppService

class Command(BaseCommand):
    help = 'Envia lembretes de agendamento via WhatsApp'

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

        self.stdout.write(f'Processo concluído. Lembretes enviados: {sent_count}')
