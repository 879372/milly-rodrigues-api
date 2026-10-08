from django.db.models.signals import post_save
from django.dispatch import receiver
from .models import Appointment, Notification
from .whatsapp_service import WhatsAppService

@receiver(post_save, sender=Appointment)
def trigger_notifications(sender, instance, created, **kwargs):
    if getattr(instance, '_skip_notification', False):
        return

    if created:
        # Enviar confirmação apenas se não houver uma enviada com sucesso
        if not Notification.objects.filter(appointment=instance, type='confirmation', status='sent').exists():
            WhatsAppService.send_confirmation(instance)
    else:
        # Verificar mudanças de status
        if instance.status == 'cancelled':
            # Enviar cancelamento apenas se não houver um enviado com sucesso
            if not Notification.objects.filter(appointment=instance, type='cancellation', status='sent').exists():
                WhatsAppService.send_cancellation(instance)
        
        elif instance.status == 'completed':
            # Enviar agradecimento pós-visita apenas se não houver um enviado com sucesso
            if not Notification.objects.filter(appointment=instance, type='thank_you', status='sent').exists():
                WhatsAppService.send_post_visit(instance)
