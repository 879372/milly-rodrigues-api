import logging
import requests
import os
import re
from django.db import transaction
from django.utils import timezone
from .models import Notification
from .portal import make_portal_token, portal_base_url

logger = logging.getLogger(__name__)


def _portal_url(appointment):
    """Link assinado do portal 'meus-agendamentos' para o cliente do agendamento."""
    base = portal_base_url()
    if not appointment.client_id:
        return f"{base}/agendar"
    token = make_portal_token(appointment.client_id)
    return f"{base}/meus-agendamentos?token={token}"


def _render_template(template, appointment, review_url=''):
    local_dt = timezone.localtime(appointment.date_time)
    service_names = ", ".join(s.name for s in appointment.services.all()) or "seu procedimento"
    professional_name = (
        appointment.barber.first_name if appointment.barber and appointment.barber.first_name
        else 'Milly Rodrigues'
    )
    values = {
        '{nome}': (appointment.client.first_name if appointment.client else '') or 'Cliente',
        '{servico}': service_names,
        '{profissional}': professional_name,
        '{data}': local_dt.strftime('%d/%m/%Y'),
        '{hora}': local_dt.strftime('%H:%M'),
        '{link_agendamento}': f"{portal_base_url()}/agendar",
        '{link_gerenciamento}': _portal_url(appointment),
        '{link_avaliacao}': review_url,
    }
    message = template
    for placeholder, value in values.items():
        message = message.replace(placeholder, value)
    return message

# Tentativas permitidas quando a API recusa a mensagem explicitamente.
MAX_SEND_ATTEMPTS = 2


def _claim_notification(appointment, message_type, content):
    """Reserva o envio de um tipo de mensagem para o agendamento.

    Retorna None se o envio já foi feito, está em andamento, teve resultado
    incerto ou esgotou as tentativas. A trava na linha do agendamento impede
    que requisições simultâneas (ex.: clique duplo) enviem a mesma mensagem.
    """
    from .models import Appointment

    with transaction.atomic():
        Appointment.objects.select_for_update().filter(pk=appointment.pk).first()
        previous = Notification.objects.filter(appointment=appointment, type=message_type)
        if previous.filter(status__in=('sent', 'pending')).exists():
            return None
        if previous.filter(status='failed').count() >= MAX_SEND_ATTEMPTS:
            return None
        return Notification.objects.create(
            appointment=appointment,
            type=message_type,
            message=content,
            status='pending',
        )


class WhatsAppService:
    @staticmethod
    def send_message(appointment, message_type, content):
        """
        Sends a WhatsApp message using the configured provider (Evolution API).

        Retorna True se enviou, False se a API recusou e None se o envio foi
        ignorado por já existir uma mensagem desse tipo para o agendamento.
        """
        # 1. Create notification log (only once per appointment and type)
        notification = _claim_notification(appointment, message_type, content)
        if notification is None:
            return None

        # 2. Get configuration
        api_url = os.getenv('WHATSAPP_API_URL')
        api_key = os.getenv('WHATSAPP_API_KEY')
        instance = os.getenv('WHATSAPP_INSTANCE')
        
        if not all([api_url, api_key, instance]):
            logger.warning("WhatsApp credentials missing. Falling back to MOCK.")
            notification.status = 'sent' # Mark as sent for mock
            notification.sent_at = timezone.now()
            notification.save()
            return True

        # 3. Sending process via Evolution API
        try:
            phone = re.sub(r'\D', '', appointment.client.phone or '')
            if len(phone) <= 11:
                phone = f"55{phone}"
            
            # Evolution API Send Text endpoint
            endpoint = f"{api_url}/message/sendText/{instance}"
            headers = {
                "apikey": api_key,
                "Content-Type": "application/json"
            }
            payload = {
                "number": phone,
                "options": {
                    "delay": 1200,
                    "presence": "composing",
                    "linkPreview": False
                },
                "text": content,
            }
            
            response = requests.post(endpoint, json=payload, headers=headers, timeout=10)
        except requests.exceptions.ReadTimeout as e:
            # A mensagem pode ter sido entregue mesmo sem resposta: fica 'pending'
            # para nunca ser reenviada automaticamente.
            logger.error(f"WHATSAPP SEND UNCERTAIN (notification {notification.pk}): {str(e)}")
            return False
        except Exception as e:
            logger.error(f"WHATSAPP SEND FAILED: {str(e)}")
            notification.status = 'failed'
            notification.save()
            return False

        if response.status_code in [200, 201]:
            notification.status = 'sent'
            notification.sent_at = timezone.now()
            notification.save()
            return True

        logger.error(f"WHATSAPP API ERROR: {response.status_code} - {response.text}")
        notification.status = 'failed'
        notification.save()
        return False

    @classmethod
    def send_confirmation(cls, appointment):
        from .models import MessageAutomationConfig
        config = MessageAutomationConfig.load()
        if not config.confirmation_enabled:
            return True
        return cls.send_message(
            appointment, 'confirmation',
            _render_template(config.confirmation_template, appointment),
        )

    @classmethod
    def send_reminder(cls, appointment):
        from .models import MessageAutomationConfig
        config = MessageAutomationConfig.load()
        if not config.appointment_reminder_enabled:
            return True
        return cls.send_message(
            appointment, 'reminder',
            _render_template(config.appointment_reminder_template, appointment),
        )

    @classmethod
    def send_cancellation(cls, appointment):
        from .models import MessageAutomationConfig
        config = MessageAutomationConfig.load()
        if not config.cancellation_enabled:
            return True
        return cls.send_message(
            appointment, 'cancellation',
            _render_template(config.cancellation_template, appointment),
        )

    @classmethod
    def send_post_visit(cls, appointment):
        from .models import MessageAutomationConfig
        config = MessageAutomationConfig.load()
        if not config.thank_you_enabled:
            return True
        return cls.send_message(
            appointment, 'thank_you',
            _render_template(config.thank_you_template, appointment),
        )

    @classmethod
    def send_follow_up(cls, appointment, template):
        return cls.send_message(
            appointment,
            'follow_up',
            _render_template(template, appointment),
        )

    @classmethod
    def send_review_request(cls, appointment, template, review_url):
        return cls.send_message(
            appointment,
            'review_request',
            _render_template(template, appointment, review_url=review_url),
        )

    @classmethod
    def send_return_reminder(cls, appointment, template):
        return cls.send_message(
            appointment,
            'return_reminder',
            _render_template(template, appointment),
        )
