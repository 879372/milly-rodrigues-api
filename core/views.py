from django.http import JsonResponse
from django.db import connection

def health_check(request):
    """
    Endpoint para verificação de integridade do sistema (Health Check).
    Verifica se a conexão com o banco de dados está ativa.
    """
    try:
        connection.ensure_connection()
        return JsonResponse({
            "status": "ok", 
            "database": "connected",
            "message": "Sistema Milly Rodrigues está online."
        })
    except Exception as e:
        return JsonResponse({
            "status": "error", 
            "database": "disconnected",
            "error": str(e)
        }, status=503)
