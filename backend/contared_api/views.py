from django.db import DatabaseError, connection, transaction
from rest_framework.exceptions import APIException
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from contared_api.session_access import DatabaseUnavailable, load_session_access


class ApiStatusView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SET TRANSACTION READ ONLY")
                    cursor.execute("SELECT 1")
        except DatabaseError as exc:
            raise DatabaseUnavailable() from exc
        return Response({"status": "ok"})


class SessionView(APIView):
    def get(self, request):
        return Response(
            load_session_access(
                request.user.auth_user_id,
                allow_password_change=True,
            )
        )
