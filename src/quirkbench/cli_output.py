"""One response convention; presentation never changes service authorization."""
import json
import sys
from .operations import operation_response
from .state_reader import safe_text


def emit(args, data):
    if args.json:
        print(json.dumps(operation_response(data=data), sort_keys=True))
    else:
        from .cli_product import render
        print(render(data))


def error(args, message, *, code='INVALID_INPUT', retryable=False):
    message=safe_text(str(message))[:512]
    if args.json:
        print(json.dumps(operation_response(error={'code':code,'message':message,'retryable':retryable}),sort_keys=True))
    else:
        print(code+': '+message,file=sys.stderr)
