"""Explicit resource accounting and bounded retries, without SDK hidden retries."""
from copy import deepcopy


class BudgetExceeded(Exception):
    pass


def client_config(config):
    result = deepcopy(config)
    timeout = result.get("harness", {}).get("request_timeout", 60)
    if not isinstance(timeout, (int, float)) or not 1 <= timeout <= 300:
        raise ValueError("harness.request_timeout must be between 1 and 300 seconds")
    for provider in result.get("llm", {}).get("providers", {}).values():
        provider.update(timeout=timeout, max_retries=0)
    return result


def transient(exc):
    # Network/timeouts are safe to retry only for the allowlisted, non-publishing tools.
    from openai import APIConnectionError, APIStatusError
    return (isinstance(exc, (TimeoutError, ConnectionError, APIConnectionError))
            or isinstance(exc, APIStatusError) and (exc.status_code == 429 or exc.status_code >= 500))


def reserve(state, model_calls=0, evaluations=0):
    if state.model_calls_used + model_calls > state.max_model_calls:
        raise BudgetExceeded("model_call_budget_exhausted")
    if state.evaluations_used + evaluations > state.max_evaluations:
        raise BudgetExceeded("evaluation_budget_exhausted")
    state.model_calls_used += model_calls
    state.evaluations_used += evaluations
