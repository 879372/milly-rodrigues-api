from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response


class OptionalPageNumberPagination(PageNumberPagination):
    """Paginação opt-in: só pagina quando o parâmetro ``page`` é enviado.

    Mantém compatibilidade com os consumidores que ainda esperam a lista
    completa (selects, dropdowns, telas antigas) e habilita scroll infinito
    onde o front-end pedir explicitamente ``?page=N``.
    """

    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 200

    def paginate_queryset(self, queryset, request, view=None):
        if 'page' not in request.query_params:
            return None
        return super().paginate_queryset(queryset, request, view)


def paginate_plain_list(request, data, default_page_size=20):
    """Paginação manual para APIViews que montam listas em memória.

    Sem ``page`` -> retorna a lista crua (compatível com o comportamento antigo).
    Com ``page`` -> retorna ``{count, next, previous, results}``.
    """
    page_param = request.query_params.get('page')
    if page_param is None:
        return Response(data)

    try:
        page = max(1, int(page_param))
    except (TypeError, ValueError):
        page = 1

    try:
        page_size = min(200, max(1, int(request.query_params.get('page_size', default_page_size))))
    except (TypeError, ValueError):
        page_size = default_page_size

    total = len(data)
    start = (page - 1) * page_size
    end = start + page_size

    return Response({
        'count': total,
        'next': page + 1 if end < total else None,
        'previous': page - 1 if page > 1 else None,
        'results': data[start:end],
    })
