from rest_framework.exceptions import APIException
from rest_framework.views import exception_handler


def api_exception_handler(exc, context):
    response = exception_handler(exc, context)
    if response is not None and isinstance(exc, APIException):
        response.data["code"] = exc.get_codes()
    return response
