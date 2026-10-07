"""Classes de permissão centralizadas.

O projeto não tem multi-tenant: o isolamento é por PAPEL (admin/barber/receptionist)
e por POSSE de objeto. Estas classes substituem o uso solto de ``IsAuthenticated``.
"""
from rest_framework import permissions

STAFF_ROLES = ("admin", "barber", "receptionist")


def _role(request):
    user = getattr(request, "user", None)
    return getattr(user, "role", None) if user and user.is_authenticated else None


class IsAdmin(permissions.BasePermission):
    """Somente usuários autenticados com papel 'admin' (ou superuser)."""

    message = "Requer privilégio de administrador."

    def has_permission(self, request, view):
        user = request.user
        return bool(user and user.is_authenticated and (user.is_superuser or getattr(user, "role", None) == "admin"))


class IsStaff(permissions.BasePermission):
    """Usuários autenticados que fazem parte da operação (admin/barber/receptionist)."""

    message = "Requer conta da equipe."

    def has_permission(self, request, view):
        user = request.user
        return bool(
            user
            and user.is_authenticated
            and (user.is_superuser or getattr(user, "role", None) in STAFF_ROLES)
        )


class IsFrontDesk(permissions.BasePermission):
    """Admin ou recepção (não barbeiro). Para caixa/dívidas/relatórios operacionais."""

    message = "Requer conta de administração ou recepção."

    def has_permission(self, request, view):
        user = request.user
        return bool(
            user
            and user.is_authenticated
            and (user.is_superuser or getattr(user, "role", None) in ("admin", "receptionist"))
        )


class IsAdminOrReadOnly(permissions.BasePermission):
    """Leitura liberada (inclusive anônima); escrita só para admin."""

    def has_permission(self, request, view):
        if request.method in permissions.SAFE_METHODS:
            return True
        return IsAdmin().has_permission(request, view)


class IsStaffOrReadOnly(permissions.BasePermission):
    """Leitura liberada; escrita para qualquer conta da equipe."""

    def has_permission(self, request, view):
        if request.method in permissions.SAFE_METHODS:
            return True
        return IsStaff().has_permission(request, view)


class IsAdminOrOwnerBarber(permissions.BasePermission):
    """Admin faz tudo; barbeiro só mexe nos próprios objetos (``obj.barber``)."""

    message = "Você só pode alterar os seus próprios registros."

    def has_permission(self, request, view):
        return IsStaff().has_permission(request, view)

    def has_object_permission(self, request, view, obj):
        if IsAdmin().has_permission(request, view):
            return True
        if request.method in permissions.SAFE_METHODS:
            return True
        barber = getattr(obj, "barber", None)
        return barber is not None and barber == request.user
