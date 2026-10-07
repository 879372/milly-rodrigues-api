"""Cliente da API de checkout da InfinitePay.

Documentação: https://api.checkout.infinitepay.io
- POST /links          -> gera um link de pagamento
- POST /payment_check  -> consulta o status de um pagamento

Todos os valores monetários são em centavos.
"""
import logging
import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://api.checkout.infinitepay.io"
TIMEOUT = 12


class InfinitePayError(Exception):
    """Falha ao falar com a InfinitePay (rede, credencial ou resposta inválida)."""


def _post(path, payload):
    url = f"{BASE_URL}{path}"
    try:
        resp = requests.post(url, json=payload, timeout=TIMEOUT, headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
        })
    except requests.RequestException as exc:
        logger.error("InfinitePay %s falhou: %s", path, exc)
        raise InfinitePayError(str(exc)) from exc

    if resp.status_code not in (200, 201):
        logger.error("InfinitePay %s -> %s: %s", path, resp.status_code, resp.text[:500])
        raise InfinitePayError(f"HTTP {resp.status_code}")

    try:
        return resp.json()
    except ValueError as exc:
        logger.error("InfinitePay %s: resposta não-JSON: %s", path, resp.text[:500])
        raise InfinitePayError("resposta inválida") from exc


def create_link(*, handle, items, order_nsu, redirect_url, webhook_url=None, customer=None):
    """Gera um link de checkout. Devolve ``(checkout_url, raw_response)``."""
    payload = {
        "handle": handle,
        "redirect_url": redirect_url,
        "order_nsu": order_nsu,
        "items": items,
    }
    if webhook_url:
        payload["webhook_url"] = webhook_url
    if customer:
        payload["customer"] = customer

    data = _post("/links", payload)
    checkout_url = (
        data.get("url")
        or data.get("link")
        or data.get("checkout_url")
        or (data.get("data") or {}).get("url")
    )
    if not checkout_url:
        logger.error("InfinitePay /links sem URL na resposta: %s", data)
        raise InfinitePayError("link ausente na resposta")
    return checkout_url, data


def check_payment(*, handle, order_nsu, transaction_nsu=None, slug=None):
    """Consulta o status. Devolve o dict cru: {success, paid, amount, paid_amount, ...}."""
    payload = {"handle": handle, "order_nsu": order_nsu}
    if transaction_nsu:
        payload["transaction_nsu"] = transaction_nsu
    if slug:
        payload["slug"] = slug
    return _post("/payment_check", payload)
