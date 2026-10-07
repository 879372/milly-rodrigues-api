"""Tokens assinados para o portal público do cliente (meus-agendamentos).

Substitui a identificação por telefone cru (IDOR) por um token HMAC emitido
na confirmação do agendamento e válido por um período limitado.
"""
import os
from django.core import signing

_SALT = "millyrodrigues.portal.appointments.v1"
# Vida útil do link enviado por WhatsApp.
MAX_AGE_SECONDS = 60 * 60 * 24 * 120  # 120 dias

_DEFAULT_PORTAL_BASE = "http://localhost:5173"


def portal_base_url() -> str:
    """URL base do frontend público (portal de agendamento)."""
    return os.getenv("PUBLIC_PORTAL_BASE_URL", _DEFAULT_PORTAL_BASE).rstrip("/")


def make_portal_token(client_id: int) -> str:
    return signing.dumps(int(client_id), salt=_SALT)


def read_portal_token(token: str):
    """Devolve o ``client_id`` (int) ou ``None`` se o token for inválido/expirado."""
    if not token:
        return None
    try:
        return int(signing.loads(token, salt=_SALT, max_age=MAX_AGE_SECONDS))
    except (signing.BadSignature, signing.SignatureExpired, ValueError, TypeError):
        return None
