from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver
from .models import Appointment
from .whatsapp_service import WhatsAppService

STATUS_MESSAGES = {
    'cancelled': WhatsAppService.send_cancellation,
    'completed': WhatsAppService.send_post_visit,
}


@receiver(post_save, sender=Appointment)
def trigger_notifications(sender, instance, created, **kwargs):
    previous_status = getattr(instance, '_loaded_status', None)
    instance._loaded_status = instance.status

    if getattr(instance, '_skip_notification', False):
        return

    if created:
        sender_fn = WhatsAppService.send_confirmation
    elif instance.status != previous_status:
        # Edições que não mudam o status (desconto, pagamentos...) não reenviam nada.
        sender_fn = STATUS_MESSAGES.get(instance.status)
    else:
        sender_fn = None

    if sender_fn:
        # Só envia depois que a transação grava; se ela for desfeita, nada sai.
        transaction.on_commit(lambda: sender_fn(instance))
