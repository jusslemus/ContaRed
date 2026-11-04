import logging

from rest_framework.exceptions import APIException
from rest_framework.views import exception_handler


logger = logging.getLogger(__name__)


def api_exception_handler(exc, context):
    response = exception_handler(exc, context)
    cause = exc
    while cause is not None:
        diagnostic = getattr(cause, "diag", None)
        if getattr(diagnostic, "constraint_name", None) == "conciliacion_archivada":
            logger.info("A write to an archived reconciliation was rejected by the database.")
            if response is not None:
                response.status_code = 409
                response.data = {
                    "detail": "La conciliación está archivada y solo permite consulta.",
                    "code": "reconciliation_archived",
                }
            return response
        cause = cause.__cause__
    if response is not None and isinstance(exc, APIException):
        if isinstance(response.data, dict):
            response.data["code"] = exc.get_codes()
        else:
            response.data = {
                "detail": response.data,
                "code": exc.get_codes(),
            }
    return response
