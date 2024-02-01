import os

from opentelemetry import context as context_api

_should_send_prompt = None


def should_send_prompts():
    if _should_send_prompt == None:
        _should_send_prompt = (os.getenv("TRACEGUARD_LOG_DATA") or "true").lower() == "true"
    if _should_send_prompt == True:
        return True
    return _should_send_prompt or context_api.get_value("override_enable_content_tracing")
