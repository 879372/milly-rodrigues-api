import logging
import requests
import os
import re
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
    service_names = ", ".join(s.name for s in appointment.services.all()) or "seu procedimento"
    professional_name = (
        appointment.barber.first_name if appointment.barber and appointment.barber.first_name
        else 'Milly Rodrigues'
    )
    values = {
        '{nome}': appointment.client.first_name or 'Cliente',
        '{servico}': service_names,
        '{profissional}': professional_name,
        '{data}': timezone.localtime(appointment.date_time).strftime('%d/%m/%Y'),
        '{link_agendamento}': f"{portal_base_url()}/agendar",
        '{link_avaliacao}': review_url,
    }
    message = template
    for placeholder, value in values.items():
        message = message.replace(placeholder, value)
    return message

class WhatsAppService:
    @staticmethod
    def send_message(appointment, message_type, content):
        """
        Sends a WhatsApp message using the configured provider (Evolution API).
        """
        # 1. Create notification log
        notification = Notification.objects.create(
            appointment=appointment,
            type=message_type,
            message=content,
            status='pending'
        )

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
            import re
            phone = re.sub(r'\D', '', appointment.client.phone)
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
            
            if response.status_code in [200, 201]:
                notification.status = 'sent'
                notification.sent_at = timezone.now()
                notification.save()
                return True
            else:
                logger.error(f"WHATSAPP API ERROR: {response.status_code} - {response.text}")
                notification.status = 'failed'
                notification.save()
                return False

        except Exception as e:
            logger.error(f"WHATSAPP SEND FAILED: {str(e)}")
            notification.status = 'failed'
            notification.save()
            return False

    @classmethod
    def send_confirmation(cls, appointment):
        from django.utils import timezone
        local_dt = timezone.localtime(appointment.date_time)
        service_names = ", ".join(s.name for s in appointment.services.all()) if getattr(appointment, "services", None) and appointment.services.exists() else "Serviços"
        barber_name = appointment.barber.first_name if appointment.barber else 'Milly Rodrigues'
        service_names = ", ".join(s.name for s in appointment.services.all()) if getattr(appointment, "services", None) and appointment.services.exists() else "Serviços"
        manage_url = _portal_url(appointment)
        
        msg = (
            f"✅ *Agendamento Confirmado*\n\n"
            f"Olá {appointment.client.first_name if appointment.client else 'Cliente'},\n"
            f"Seu agendamento para *{service_names}* foi confirmado!\n\n"
            f"📅 *Data:* {local_dt.strftime('%d/%m')}\n"
            f"⏰ *Hora:* {local_dt.strftime('%H:%M')}\n"
            f"✨ *Profissional:* {barber_name}\n\n"
            f"📍 *Local:* Milly Rodrigues — Depilação & Estética\n"
            f"⚠️ *Importante:* Pedimos a gentileza de chegar com *10 minutos de antecedência*.\n\n"
            f"🔗 *Veja ou cancele em:* {manage_url}\n\n"
            f"Esperamos por você! ✨"
        )
        return cls.send_message(appointment, 'confirmation', msg)

    @classmethod
    def send_reminder(cls, appointment):
        from django.utils import timezone
        import re
        local_dt = timezone.localtime(appointment.date_time)
        service_names = ", ".join(s.name for s in appointment.services.all()) if getattr(appointment, "services", None) and appointment.services.exists() else "Serviços"
        barber_name = appointment.barber.first_name if appointment.barber else 'Milly Rodrigues'
        service_names = ", ".join(s.name for s in appointment.services.all()) if getattr(appointment, "services", None) and appointment.services.exists() else "Serviços"
        manage_url = _portal_url(appointment)
        
        msg = (
            f"⏰ *Lembrete de Agendamento*\n\n"
            f"Olá! Passando para lembrar do seu horário hoje:\n\n"
            f"✨ *{service_names}*\n"
            f"🕒 às *{local_dt.strftime('%H:%M')}*\n"
            f"com *{barber_name}*\n\n"
            f"📍 *Lembrete:* Chegue com 10 minutos de antecedência.\n\n"
            f"🔗 *Gerenciar agendamento:* {manage_url}\n\n"
            f"Até logo! ✨"
        )
        return cls.send_message(appointment, 'reminder', msg)

    @classmethod
    def send_cancellation(cls, appointment):
        from django.utils import timezone
        local_dt = timezone.localtime(appointment.date_time)
        service_names = ", ".join(s.name for s in appointment.services.all()) if getattr(appointment, "services", None) and appointment.services.exists() else "Serviços"
        msg = (
            f"❌ *Agendamento Cancelado*\n\n"
            f"Olá, o seu agendamento para *{service_names}* no dia {local_dt.strftime('%d/%m às %H:%M')} foi cancelado.\n\n"
            f"Caso queira agendar um novo horário, acesse: {portal_base_url()}/agendar"
        )
        return cls.send_message(appointment, 'cancellation', msg)

    @classmethod
    def send_post_visit(cls, appointment):
        import re
        service_names = ", ".join(s.name for s in appointment.services.all()) if getattr(appointment, "services", None) and appointment.services.exists() else "Serviços"
        manage_url = _portal_url(appointment)
        
        msg = (
            f"⭐ *Obrigado pela visita!*\n\n"
            f"Olá {appointment.client.first_name if appointment.client else 'Cliente'},\n"
            f"Obrigado por escolher a Milly Rodrigues! Esperamos que tenha gostado do serviço *{service_names}*.\n\n"
            f"🔗 *Acompanhe seu histórico:* {manage_url}\n\n"
            f"Até a próxima! ✨"
        )
        return cls.send_message(appointment, 'confirmation', msg)

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
