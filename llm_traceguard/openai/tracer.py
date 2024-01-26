import logging
import os
import json
import types
from importlib.metadata import distribution  # type: ignore
from typing import Collection
from enum import Enum
from wrapt import wrap_function_wrapper  # type: ignore
import openai

from opentelemetry import context as context_api
from opentelemetry.trace import get_tracer, SpanKind
from opentelemetry.trace.status import Status, StatusCode

from opentelemetry.instrumentation.instrumentor import BaseInstrumentor  # type: ignore
from opentelemetry.instrumentation.utils import (
    _SUPPRESS_INSTRUMENTATION_KEY,
    unwrap,
)


# from opentelemetry.semconv.ai import SpanAttributes, LLMRequestTypeValues
# from opentelemetry.instrumentation.openai.version import __version__
class LLMRequestTypeValues(Enum):
    COMPLETION = "completion"
    CHAT = "chat"
    RERANK = "rerank"
    UNKNOWN = "unknown"


class SpanAttributes:
    # LLM
    LLM_VENDOR = "llm.vendor"
    LLM_REQUEST_TYPE = "llm.request.type"
    LLM_REQUEST_MODEL = "llm.request.model"
    LLM_RESPONSE_MODEL = "llm.response.model"
    LLM_REQUEST_MAX_TOKENS = "llm.request.max_tokens"
    LLM_USAGE_TOTAL_TOKENS = "llm.usage.total_tokens"
    LLM_USAGE_COMPLETION_TOKENS = "llm.usage.completion_tokens"
    LLM_USAGE_PROMPT_TOKENS = "llm.usage.prompt_tokens"
    LLM_TEMPERATURE = "llm.temperature"
    LLM_USER = "llm.user"
    LLM_HEADERS = "llm.headers"
    LLM_TOP_P = "llm.top_p"
    LLM_FREQUENCY_PENALTY = "llm.frequency_penalty"
    LLM_PRESENCE_PENALTY = "llm.presence_penalty"
    LLM_PROMPTS = "llm.prompts"
    LLM_COMPLETIONS = "llm.completions"
    LLM_CHAT_STOP_SEQUENCES = "llm.chat.stop_sequences"
    LLM_REQUEST_FUNCTIONS = "llm.request.functions"

    # Vector DB
    VECTOR_DB_VENDOR = "vector_db.vendor"
    VECTOR_DB_QUERY_TOP_K = "vector_db.query.top_k"


logger = logging.getLogger(__name__)

_instruments = ("openai >= 0.27.0",)
__version__ = "0.1.0"
_is_openai_v1 = None


def _check_openai_v1():
    global _is_openai_v1
    if _is_openai_v1 is None:
        logger.info("Check for OpenAI version")
        res = distribution("openai")
        logger.info(f"OpenAI version: {res.version}")
        _is_openai_v1 = (res.version >= "1.0.0")
    return _is_openai_v1


WRAPPED_METHODS_VERSION_0 = [
    {
        "module": "openai",
        "object": "ChatCompletion",
        "method": "create",
        "span_name": "openai.chat",
    },
    {
        "module": "openai",
        "object": "Completion",
        "method": "create",
        "span_name": "openai.completion",
    },
]

WRAPPED_METHODS_VERSION_1 = [
    {
        "module": "openai.resources.chat.completions",
        "object": "Completions",
        "method": "create",
        "span_name": "openai.chat",
    },
    {
        "module": "openai.resources.completions",
        "object": "Completions",
        "method": "create",
        "span_name": "openai.completion",
    },
]


def should_send_prompts():
    return (
                   os.getenv("TRACELOOP_TRACE_CONTENT") or "true"
           ).lower() == "true" or context_api.get_value("override_enable_content_tracing")


def _set_span_attribute(span, name, value):
    if value is not None:
        if value != "":
            span.set_attribute(name, value)
    return


def _set_api_attributes(span):
    _set_span_attribute(
            span,
            OpenAISpanAttributes.OPENAI_API_BASE,
            openai.base_url if hasattr(openai, "base_url") else openai.api_base,
    )
    _set_span_attribute(span, OpenAISpanAttributes.OPENAI_API_TYPE, openai.api_type)
    _set_span_attribute(
            span, OpenAISpanAttributes.OPENAI_API_VERSION, openai.api_version
    )

    return


def _set_span_prompts(span, messages):
    if messages is None:
        return

    for i, msg in enumerate(messages):
        prefix = f"{SpanAttributes.LLM_PROMPTS}.{i}"
        _set_span_attribute(span, f"{prefix}.role", msg.get("role"))
        _set_span_attribute(span, f"{prefix}.content", msg.get("content"))


def _set_input_attributes(span, llm_request_type, kwargs):
    _set_span_attribute(span, SpanAttributes.LLM_REQUEST_MODEL, kwargs.get("model"))
    _set_span_attribute(
            span, SpanAttributes.LLM_REQUEST_MAX_TOKENS, kwargs.get("max_tokens")
    )
    _set_span_attribute(span, SpanAttributes.LLM_TEMPERATURE, kwargs.get("temperature"))
    _set_span_attribute(span, SpanAttributes.LLM_TOP_P, kwargs.get("top_p"))
    _set_span_attribute(
            span, SpanAttributes.LLM_FREQUENCY_PENALTY, kwargs.get("frequency_penalty")
    )
    _set_span_attribute(
            span, SpanAttributes.LLM_PRESENCE_PENALTY, kwargs.get("presence_penalty")
    )
    _set_span_attribute(span, SpanAttributes.LLM_USER, kwargs.get("user"))
    _set_span_attribute(span, SpanAttributes.LLM_HEADERS, str(kwargs.get("headers")))

    if should_send_prompts():
        if llm_request_type == LLMRequestTypeValues.CHAT:
            _set_span_prompts(span, kwargs.get("messages"))
        elif llm_request_type == LLMRequestTypeValues.COMPLETION:
            prompt = kwargs.get("prompt")
            _set_span_attribute(
                    span,
                    f"{SpanAttributes.LLM_PROMPTS}.0.user",
                    prompt[0] if isinstance(prompt, list) else prompt,
            )

        functions = kwargs.get("functions")
        if functions:
            for i, function in enumerate(functions):
                prefix = f"{SpanAttributes.LLM_REQUEST_FUNCTIONS}.{i}"
                _set_span_attribute(span, f"{prefix}.name", function.get("name"))
                _set_span_attribute(
                        span, f"{prefix}.description", function.get("description")
                )
                _set_span_attribute(
                        span, f"{prefix}.parameters", json.dumps(function.get("parameters"))
                )

    return


def _set_span_completions(span, llm_request_type, choices):
    if choices is None:
        return

    for choice in choices:
        if _check_openai_v1() and not isinstance(choice, dict):
            choice = choice.__dict__

        index = choice.get("index")
        prefix = f"{SpanAttributes.LLM_COMPLETIONS}.{index}"
        _set_span_attribute(
                span, f"{prefix}.finish_reason", choice.get("finish_reason")
        )

        if llm_request_type == LLMRequestTypeValues.CHAT:
            message = choice.get("message")
            if message is not None:
                if _check_openai_v1() and not isinstance(message, dict):
                    message = message.__dict__

                _set_span_attribute(span, f"{prefix}.role", message.get("role"))
                _set_span_attribute(span, f"{prefix}.content", message.get("content"))
                function_call = message.get("function_call")
                if function_call:
                    if _check_openai_v1() and not isinstance(function_call, dict):
                        function_call = function_call.__dict__

                    _set_span_attribute(
                            span, f"{prefix}.function_call.name", function_call.get("name")
                    )
                    _set_span_attribute(
                            span,
                            f"{prefix}.function_call.arguments",
                            function_call.get("arguments"),
                    )
        elif llm_request_type == LLMRequestTypeValues.COMPLETION:
            _set_span_attribute(span, f"{prefix}.content", choice.get("text"))


def _set_response_attributes(span, llm_request_type, response):
    logger.info(f"Type: {llm_request_type}. Response: {response}")
    _set_span_attribute(span, SpanAttributes.LLM_RESPONSE_MODEL, response.get("model"))
    if should_send_prompts():
        _set_span_completions(span, llm_request_type, response.get("choices"))

    usage = response.get("usage")
    if usage is not None:
        if _check_openai_v1() and not isinstance(usage, dict):
            usage = usage.__dict__

        _set_span_attribute(
                span, SpanAttributes.LLM_USAGE_TOTAL_TOKENS, usage.get("total_tokens")
        )
        _set_span_attribute(
                span,
                SpanAttributes.LLM_USAGE_COMPLETION_TOKENS,
                usage.get("completion_tokens"),
        )
        _set_span_attribute(
                span, SpanAttributes.LLM_USAGE_PROMPT_TOKENS, usage.get("prompt_tokens")
        )

    return


def _build_from_streaming_response(span, llm_request_type, response):
    print("Streaming messages")
    complete_response = {"choices": [], "model": ""}
    for item in response:
        if _check_openai_v1():
            item = item.__dict__

        for choice in item.get("choices"):
            if _check_openai_v1():
                choice = choice.__dict__

            index = choice.get("index")
            if len(complete_response.get("choices")) <= index:
                complete_response["choices"].append(
                        {"index": index, "message": {"content": "", "role": ""}}
                        if llm_request_type == LLMRequestTypeValues.CHAT
                        else {"index": index, "text": ""}
                )
            complete_choice = complete_response.get("choices")[index]
            if choice.get("finish_reason"):
                complete_choice["finish_reason"] = choice.get("finish_reason")
            if llm_request_type == LLMRequestTypeValues.CHAT:
                delta = choice.get("delta")
                if _check_openai_v1():
                    delta = delta.__dict__

                if delta.get("content"):
                    complete_choice["message"]["content"] += delta.get("content")
                if delta.get("role"):
                    complete_choice["message"]["role"] = delta.get("role")
            else:
                complete_choice["text"] += choice.get("text")

        yield item

    _set_response_attributes(
            span,
            llm_request_type,
            complete_response,
    )
    span.set_status(Status(StatusCode.OK))
    span.end()


def _with_tracer_wrapper(func):
    """Helper for providing tracer for wrapper functions."""

    def _with_tracer(tracer, to_wrap):
        def wrapper(wrapped, instance, args, kwargs):
            return func(tracer, to_wrap, wrapped, instance, args, kwargs)

        return wrapper

    return _with_tracer


def _llm_request_type_by_module_object(module_name, object_name):
    if _check_openai_v1():
        if module_name == "openai.resources.chat.completions":
            return LLMRequestTypeValues.CHAT
        elif module_name == "openai.resources.completions":
            return LLMRequestTypeValues.COMPLETION
        else:
            return LLMRequestTypeValues.UNKNOWN
    else:
        if object_name == "Completion":
            return LLMRequestTypeValues.COMPLETION
        elif object_name == "ChatCompletion":
            return LLMRequestTypeValues.CHAT
        else:
            return LLMRequestTypeValues.UNKNOWN


def is_streaming_response(response):
    return isinstance(response, types.GeneratorType) or (
            _check_openai_v1() and isinstance(response, openai.Stream)
    )


@_with_tracer_wrapper
def _wrap(tracer, to_wrap, wrapped, instance, args, kwargs):
    """Instruments and calls every function defined in TO_WRAP."""
    if context_api.get_value(_SUPPRESS_INSTRUMENTATION_KEY):
        return wrapped(*args, **kwargs)

    name = to_wrap.get("span_name")
    print(f"Request: {to_wrap.get('module')}.{to_wrap.get('object')}")
    print(f"kwargs: {kwargs}")
    llm_request_type = _llm_request_type_by_module_object(
            to_wrap.get("module"), to_wrap.get("object")
    )

    span = tracer.start_span(
            name,
            kind=SpanKind.CLIENT,
            attributes={
                SpanAttributes.LLM_VENDOR: "OpenAI",
                SpanAttributes.LLM_REQUEST_TYPE: llm_request_type.value,
            },
    )
    print(llm_request_type)

    if span.is_recording():
        _set_api_attributes(span)
    try:
        if span.is_recording():
            _set_input_attributes(span, llm_request_type, kwargs)

    except Exception as ex:  # pylint: disable=broad-except
        logger.warning(
                "Failed to set input attributes for openai span, error: %s", str(ex)
        )

    from openai.openai_object import OpenAIObject
    OpenAIObject()
    response = wrapped(*args, **kwargs)
    t = type(response)

    fully_qualified_name = f"{t.__module__}.{t.__name__}"
    print(f"Response type: {fully_qualified_name}")

    if response:
        try:
            if span.is_recording():
                if is_streaming_response(response):
                    return _build_from_streaming_response(
                            span, llm_request_type, response
                    )
                else:
                    _set_response_attributes(
                            span,
                            llm_request_type,
                            response.__dict__ if _check_openai_v1() else response,
                    )

        except Exception as ex:  # pylint: disable=broad-except
            logger.warning(
                    "Failed to set response attributes for openai span, error: %s",
                    str(ex),
            )
        if span.is_recording():
            span.set_status(Status(StatusCode.OK))

    span.end()
    return response


class OpenAISpanAttributes:
    OPENAI_API_VERSION = "openai.api_version"
    OPENAI_API_BASE = "openai.api_base"
    OPENAI_API_TYPE = "openai.api_type"


class OpenAIInstrumentor(BaseInstrumentor):
    """An instrumentor for OpenAI's client library."""

    def instrumentation_dependencies(self) -> Collection[str]:
        return _instruments

    def _instrument(self, **kwargs):
        tracer_provider = kwargs.get("tracer_provider")
        print(f"Try to instruments. {tracer_provider}")

        tracer = get_tracer(__name__, __version__, tracer_provider)

        wrapped_methods = (
            WRAPPED_METHODS_VERSION_1 if _check_openai_v1() else WRAPPED_METHODS_VERSION_0
        )
        for wrapped_method in wrapped_methods:
            wrap_module = wrapped_method.get("module")
            wrap_object = wrapped_method.get("object")
            wrap_method = wrapped_method.get("method")
            wrap_function_wrapper(
                    wrap_module,
                    f"{wrap_object}.{wrap_method}",
                    _wrap(tracer, wrapped_method),
            )

    def _uninstrument(self, **kwargs):
        wrapped_methods = (
            WRAPPED_METHODS_VERSION_1 if _check_openai_v1() else WRAPPED_METHODS_VERSION_0
        )
        for wrapped_method in wrapped_methods:
            wrap_object = wrapped_method.get("object")
            unwrap(f"openai.{wrap_object}", wrapped_method.get("method"))