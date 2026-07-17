"""CampusPath AI — AgentCore runtime with routed placement workflows."""

from __future__ import annotations

import os
import time
import uuid
from typing import Any

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands import Agent
from strands.agent.conversation_manager.null_conversation_manager import (
    NullConversationManager,
)
from strands.models.bedrock import BedrockModel
from prompt_templates import (
    BASE_SYSTEM_PROMPT,
    ROUTE_PROMPTS,
    build_tagged_student_request,
)

app = BedrockAgentCoreApp()
log = app.logger

DEFAULT_MODEL_ID = os.getenv(
    "BEDROCK_MODEL_ID",
    "global.amazon.nova-2-lite-v1:0",
)
DEFAULT_TEMPERATURE = float(os.getenv("BEDROCK_TEMPERATURE", "0.4"))
DEFAULT_TOP_P = float(os.getenv("BEDROCK_TOP_P", "0.9"))
DEFAULT_MAX_TOKENS = int(os.getenv("BEDROCK_MAX_TOKENS", "2048"))

# Approximate on-demand prices (USD per 1M tokens). Demo estimates only.
MODEL_PRICING_USD_PER_1M = {
    "amazon.nova-micro": {"input": 0.035, "output": 0.14},
    "amazon.nova-lite": {"input": 0.06, "output": 0.24},
    "amazon.nova-2-lite": {"input": 0.06, "output": 0.24},
    "amazon.nova-pro": {"input": 0.80, "output": 3.20},
    "anthropic.claude-haiku": {"input": 1.00, "output": 5.00},
    "anthropic.claude-3-haiku": {"input": 0.25, "output": 1.25},
    "anthropic.claude": {"input": 3.00, "output": 15.00},
}

ROUTES: dict[str, dict[str, str]] = {
    "placement-doubt": {"title": "Placement Coach"},
    "career-roadmap": {"title": "Career Roadmap"},
    "resume-review": {"title": "Resume Review"},
    "interview-prep": {"title": "Interview Prep"},
}

# Accept either "route" or "action" as the selector key.
ROUTE_ALIASES = {
    "placement": "placement-doubt",
    "placement_doubt": "placement-doubt",
    "career": "career-roadmap",
    "career_roadmap": "career-roadmap",
    "resume": "resume-review",
    "resume_review": "resume-review",
    "interview": "interview-prep",
    "interview_prep": "interview-prep",
}


class RouteValidationError(ValueError):
    """Raised when the payload route/action is missing or invalid."""


def _normalize_route(payload: dict[str, Any]) -> str:
    raw = payload.get("route", payload.get("action"))
    if raw is None or str(raw).strip() == "":
        allowed = ", ".join(sorted(ROUTES))
        raise RouteValidationError(
            f"Missing required field 'route' (or 'action'). "
            f"Allowed values: {allowed}"
        )

    key = str(raw).strip().lower().replace(" ", "-")
    key = ROUTE_ALIASES.get(key, key)
    if key not in ROUTES:
        allowed = ", ".join(sorted(ROUTES))
        raise RouteValidationError(
            f"Invalid route '{raw}'. Allowed values: {allowed}"
        )
    return key


def _resolve_model_params(payload: dict[str, Any]) -> dict[str, Any]:
    temperature = payload.get("temperature", payload.get("temp", DEFAULT_TEMPERATURE))
    top_p = payload.get("top_p", payload.get("topP", DEFAULT_TOP_P))
    max_tokens = payload.get(
        "max_tokens",
        payload.get("maxTokens", payload.get("max_token", DEFAULT_MAX_TOKENS)),
    )
    model_id = payload.get(
        "model_id",
        payload.get("model-id", payload.get("modelId", DEFAULT_MODEL_ID)),
    )

    try:
        temperature = float(temperature)
        top_p = float(top_p)
        max_tokens = int(max_tokens)
    except (TypeError, ValueError) as exc:
        raise RouteValidationError(
            "temperature, top_p, and max_tokens must be numeric."
        ) from exc

    if not 0.0 <= temperature <= 1.0:
        raise RouteValidationError("temperature must be between 0.0 and 1.0.")
    if not 0.0 <= top_p <= 1.0:
        raise RouteValidationError("top_p must be between 0.0 and 1.0.")
    if not 1 <= max_tokens <= 8192:
        raise RouteValidationError("max_tokens must be between 1 and 8192.")
    if not isinstance(model_id, str) or not model_id.strip():
        raise RouteValidationError("model_id must be a non-empty string.")

    return {
        "model_id": model_id.strip(),
        "temperature": temperature,
        "top_p": top_p,
        "max_tokens": max_tokens,
    }


def _build_user_prompt(route: str, payload: dict[str, Any]) -> str:
    """Build the user prompt from a free-form prompt or route-specific fields."""
    if payload.get("prompt"):
        return build_tagged_student_request(route, str(payload["prompt"]).strip())

    if "messages" in payload and isinstance(payload["messages"], list):
        texts: list[str] = []
        for message in payload["messages"]:
            if not isinstance(message, dict):
                continue
            content = message.get("content", [])
            if isinstance(content, str):
                texts.append(content)
                continue
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and isinstance(block.get("text"), str):
                        texts.append(block["text"])
        if texts:
            return build_tagged_student_request(route, "\n".join(texts).strip())

    if route == "placement-doubt":
        question = payload.get("question") or payload.get("doubt")
        if not question:
            raise RouteValidationError(
                "placement-doubt requires 'prompt' or 'question'."
            )
        profile = payload.get("student_profile") or payload.get("profile") or "Not provided"
        content = f"Student profile: {profile}\n\nQuestion: {question}"
        return build_tagged_student_request(route, content)

    if route == "career-roadmap":
        goal = payload.get("goal")
        if not goal:
            raise RouteValidationError("career-roadmap requires 'prompt' or 'goal'.")
        content = (
            f"Goal: {goal}\n"
            f"Degree: {payload.get('degree', 'Not specified')}\n"
            f"Current year: {payload.get('year', 'Not specified')}\n"
            f"Current skills: {payload.get('skills', 'Not specified')}\n"
            f"Available time: {payload.get('hours_per_week', 8)} hours/week"
        )
        return build_tagged_student_request(route, content)

    if route == "resume-review":
        resume_text = payload.get("resume_text") or payload.get("resume")
        if not resume_text:
            raise RouteValidationError(
                "resume-review requires 'prompt' or 'resume_text'."
            )
        target_role = payload.get("target_role", "Software Development Engineer")
        content = f"Target role: {target_role}\n\nResume:\n{resume_text}"
        return build_tagged_student_request(route, content)

    if route == "interview-prep":
        role = payload.get("role") or payload.get("target_role")
        if not role:
            raise RouteValidationError("interview-prep requires 'prompt' or 'role'.")
        content = (
            f"Target role: {role}\n"
            f"Interview type: {payload.get('interview_type', 'Mixed')}\n"
            f"Level: {payload.get('experience_level', 'Beginner')}\n"
            f"Focus topics: {payload.get('focus_topics', 'General placement preparation')}"
        )
        return build_tagged_student_request(route, content)

    raise RouteValidationError(f"Unable to build prompt for route '{route}'.")


def _extract_text(result: object) -> str:
    message = getattr(result, "message", None)
    if isinstance(message, dict):
        blocks = message.get("content", [])
        text = "\n".join(
            block["text"]
            for block in blocks
            if isinstance(block, dict) and isinstance(block.get("text"), str)
        ).strip()
        if text:
            return text
    return str(result).strip()


def _extract_usage(result: object) -> dict[str, int]:
    metrics = getattr(result, "metrics", None)
    usage = getattr(metrics, "accumulated_usage", None) if metrics else None
    if isinstance(usage, dict):
        input_tokens = int(usage.get("inputTokens") or 0)
        output_tokens = int(usage.get("outputTokens") or 0)
        total_tokens = int(usage.get("totalTokens") or (input_tokens + output_tokens))
        return {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
        }
    return {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}


def _estimate_cost_usd(model_id: str, input_tokens: int, output_tokens: int) -> dict[str, Any]:
    rates = {"input": 0.10, "output": 0.40}  # conservative fallback
    lowered = model_id.lower()
    for prefix, pricing in MODEL_PRICING_USD_PER_1M.items():
        if prefix in lowered:
            rates = pricing
            break

    input_cost = (input_tokens / 1_000_000) * rates["input"]
    output_cost = (output_tokens / 1_000_000) * rates["output"]
    total = input_cost + output_cost
    return {
        "currency": "USD",
        "input_cost": round(input_cost, 8),
        "output_cost": round(output_cost, 8),
        "total_cost": round(total, 8),
        "pricing_note": (
            "Approximate on-demand estimate for demo purposes; "
            "verify against current Bedrock pricing for your region."
        ),
        "rates_per_1m_tokens": rates,
    }


def _create_agent(route: str, params: dict[str, Any]) -> Agent:
    model = BedrockModel(
        model_id=params["model_id"],
        temperature=params["temperature"],
        top_p=params["top_p"],
        max_tokens=params["max_tokens"],
    )
    system_prompt = (
        f"{BASE_SYSTEM_PROMPT}\n\n"
        f"{ROUTE_PROMPTS[route]}"
    )
    return Agent(
        model=model,
        system_prompt=system_prompt,
        tools=[],
        conversation_manager=NullConversationManager(),
    )


def _error_response(
    *,
    code: str,
    message: str,
    request_id: str,
    route: str | None = None,
    params: dict[str, Any] | None = None,
    latency_ms: float | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "ok": False,
        "error": {
            "code": code,
            "message": message,
            "allowed_routes": sorted(ROUTES.keys()),
        },
        "request_id": request_id,
    }
    if route is not None:
        body["route"] = route
    if params is not None:
        body["model_params"] = params
    if latency_ms is not None:
        body["latency_ms"] = round(latency_ms, 2)
    return body


@app.entrypoint
async def invoke(payload, context):
    """Single AgentCore /invocations entrypoint with body-based routing."""
    request_id = str(uuid.uuid4())
    session_id = getattr(context, "session_id", "default-session")
    started = time.perf_counter()

    if not isinstance(payload, dict):
        return _error_response(
            code="invalid_payload",
            message="Request body must be a JSON object.",
            request_id=request_id,
            latency_ms=(time.perf_counter() - started) * 1000,
        )

    try:
        route = _normalize_route(payload)
        params = _resolve_model_params(payload)
        user_prompt = _build_user_prompt(route, payload)
    except RouteValidationError as exc:
        return _error_response(
            code="validation_error",
            message=str(exc),
            request_id=request_id,
            latency_ms=(time.perf_counter() - started) * 1000,
        )

    log.info(
        "Invoking CampusPath route=%s session=%s model=%s",
        route,
        session_id,
        params["model_id"],
    )

    try:
        agent = _create_agent(route, params)
        result = await agent.invoke_async(user_prompt)
        answer = _extract_text(result)
        usage = _extract_usage(result)
        latency_ms = (time.perf_counter() - started) * 1000
        cost = _estimate_cost_usd(
            params["model_id"],
            usage["input_tokens"],
            usage["output_tokens"],
        )

        return {
            "ok": True,
            "request_id": request_id,
            "session_id": session_id,
            "route": route,
            "route_title": ROUTES[route]["title"],
            "answer": answer,
            "model_params": {
                "model_id": params["model_id"],
                "temperature": params["temperature"],
                "top_p": params["top_p"],
                "max_tokens": params["max_tokens"],
            },
            "usage": usage,
            "cost": cost,
            "latency_ms": round(latency_ms, 2),
            "stop_reason": getattr(result, "stop_reason", None),
        }
    except Exception as exc:
        log.exception("CampusPath invocation failed for route=%s", route)
        return _error_response(
            code="invocation_error",
            message=str(exc),
            request_id=request_id,
            route=route,
            params=params,
            latency_ms=(time.perf_counter() - started) * 1000,
        )


if __name__ == "__main__":
    app.run()
