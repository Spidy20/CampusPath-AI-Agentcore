"""FastAPI gateway for the CampusPath AI placement assistant."""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from strands import Agent
from strands.models.bedrock import BedrockModel
from .prompt_templates import (
    BASE_SYSTEM_PROMPT,
    ROUTE_PROMPTS,
    build_tagged_student_request,
)


FRONTEND_DIR = Path(__file__).resolve().parents[1]
DEFAULT_MODEL_ID = os.getenv(
    "BEDROCK_MODEL_ID",
    "global.amazon.nova-2-lite-v1:0",
)
DEFAULT_TEMPERATURE = float(os.getenv("BEDROCK_TEMPERATURE", "0.4"))
DEFAULT_TOP_P = float(os.getenv("BEDROCK_TOP_P", "0.9"))
DEFAULT_MAX_TOKENS = int(os.getenv("BEDROCK_MAX_TOKENS", "2048"))

MODEL_PRICING_USD_PER_1M = {
    "amazon.nova-micro": {"input": 0.035, "output": 0.14},
    "amazon.nova-lite": {"input": 0.06, "output": 0.24},
    "amazon.nova-2-lite": {"input": 0.06, "output": 0.24},
    "amazon.nova-pro": {"input": 0.80, "output": 3.20},
    "anthropic.claude-haiku": {"input": 1.00, "output": 5.00},
    "anthropic.claude-3-haiku": {"input": 0.25, "output": 1.25},
    "anthropic.claude": {"input": 3.00, "output": 15.00},
}

class ModelParamsMixin(BaseModel):
    model_id: str | None = Field(default=None, max_length=200)
    temperature: float | None = Field(default=None, ge=0.0, le=1.0)
    top_p: float | None = Field(default=None, ge=0.0, le=1.0)
    max_tokens: int | None = Field(default=None, ge=1, le=8192)


class AskRequest(ModelParamsMixin):
    question: str = Field(min_length=5, max_length=4000)
    student_profile: str | None = Field(default=None, max_length=1500)


class CareerRequest(ModelParamsMixin):
    goal: str = Field(min_length=2, max_length=300)
    degree: str = Field(min_length=2, max_length=120)
    year: str = Field(min_length=1, max_length=60)
    skills: str = Field(default="Not specified", max_length=1500)
    hours_per_week: int = Field(default=8, ge=1, le=80)


class ResumeRequest(ModelParamsMixin):
    resume_text: str = Field(min_length=50, max_length=20000)
    target_role: str = Field(min_length=2, max_length=200)


class InterviewRequest(ModelParamsMixin):
    role: str = Field(min_length=2, max_length=200)
    interview_type: Literal["Technical", "HR", "Mixed"] = "Mixed"
    experience_level: Literal["Beginner", "Intermediate", "Advanced"] = "Beginner"
    focus_topics: str = Field(default="General placement preparation", max_length=1000)


class ModelParams(BaseModel):
    model_id: str
    temperature: float
    top_p: float
    max_tokens: int


class UsageInfo(BaseModel):
    input_tokens: int
    output_tokens: int
    total_tokens: int


class CostInfo(BaseModel):
    currency: str
    input_cost: float
    output_cost: float
    total_cost: float
    pricing_note: str
    rates_per_1m_tokens: dict[str, float]


class AgentResponse(BaseModel):
    ok: bool = True
    answer: str
    mode: Literal["bedrock", "demo"]
    request_id: str
    route: str
    model_params: ModelParams
    usage: UsageInfo
    cost: CostInfo
    latency_ms: float


app = FastAPI(
    title="CampusPath AI API",
    description="Placement, career, resume and interview guidance powered by Amazon Bedrock.",
    version="1.0.0",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ORIGINS", "*").split(","),
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


def _resolve_params(body: ModelParamsMixin) -> ModelParams:
    return ModelParams(
        model_id=body.model_id or DEFAULT_MODEL_ID,
        temperature=DEFAULT_TEMPERATURE if body.temperature is None else body.temperature,
        top_p=DEFAULT_TOP_P if body.top_p is None else body.top_p,
        max_tokens=DEFAULT_MAX_TOKENS if body.max_tokens is None else body.max_tokens,
    )


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


def _extract_usage(result: object) -> UsageInfo:
    metrics = getattr(result, "metrics", None)
    usage = getattr(metrics, "accumulated_usage", None) if metrics else None
    if isinstance(usage, dict):
        input_tokens = int(usage.get("inputTokens") or 0)
        output_tokens = int(usage.get("outputTokens") or 0)
        total_tokens = int(usage.get("totalTokens") or (input_tokens + output_tokens))
        return UsageInfo(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
        )
    return UsageInfo(input_tokens=0, output_tokens=0, total_tokens=0)


def _estimate_cost_usd(model_id: str, usage: UsageInfo) -> CostInfo:
    rates = {"input": 0.10, "output": 0.40}
    lowered = model_id.lower()
    for prefix, pricing in MODEL_PRICING_USD_PER_1M.items():
        if prefix in lowered:
            rates = pricing
            break

    input_cost = (usage.input_tokens / 1_000_000) * rates["input"]
    output_cost = (usage.output_tokens / 1_000_000) * rates["output"]
    return CostInfo(
        currency="USD",
        input_cost=round(input_cost, 8),
        output_cost=round(output_cost, 8),
        total_cost=round(input_cost + output_cost, 8),
        pricing_note=(
            "Approximate on-demand estimate for demo purposes; "
            "verify against current Bedrock pricing for your region."
        ),
        rates_per_1m_tokens=rates,
    )


def _invoke_bedrock(route: str, user_prompt: str, params: ModelParams) -> tuple[str, UsageInfo]:
    model = BedrockModel(
        model_id=params.model_id,
        temperature=params.temperature,
        top_p=params.top_p,
        max_tokens=params.max_tokens,
    )
    agent = Agent(
        model=model,
        system_prompt=f"{BASE_SYSTEM_PROMPT}\n\n{ROUTE_PROMPTS[route]}",
        tools=[],
    )
    result = agent(build_tagged_student_request(route, user_prompt))
    answer = _extract_text(result)
    if not answer:
        raise RuntimeError("The model returned an empty response.")
    return answer, _extract_usage(result)


def _demo_answer(route: str) -> str:
    samples = {
        "placement-doubt": "## Recommended approach\n\n1. Identify the exact role and its top five skills.\n2. Practise aptitude and one coding topic daily.\n3. Prepare two project stories using Situation, Task, Action, Result.\n\n## Next 7 days\n\nComplete one mock test, revise your resume, and attempt two mock interviews.",
        "career-roadmap": "## 12-week roadmap\n\n- **Weeks 1–4:** Strengthen one programming language, DSA fundamentals, Git and SQL.\n- **Weeks 5–8:** Build one deployable project and document measurable outcomes.\n- **Weeks 9–12:** Practise interviews, aptitude and targeted applications.\n\nTrack solved problems, project releases and mock-interview scores each week.",
        "resume-review": "## Quick verdict\n\nYour resume needs role-focused impact statements.\n\n## High-priority fixes\n\n- Start bullets with strong action verbs.\n- Add measurable outcomes only where they are true.\n- Move the most relevant project above less relevant coursework.\n- Keep formatting consistent and ATS-friendly.\n\n**Illustrative score: 72/100.**",
        "interview-prep": "## Focus areas\n\n- Explain one project end to end: problem, architecture, trade-offs and results.\n- Revise core CS fundamentals relevant to the role.\n- Practise concise STAR stories for teamwork and failure.\n\n## Mock prompt\n\n“Walk me through a difficult technical decision in your project and what you learned.”",
    }
    return f"{samples[route]}\n\n> Demo fallback is active; configure AWS credentials for a personalized Bedrock response."


async def _answer(route: str, user_prompt: str, body: ModelParamsMixin) -> AgentResponse:
    request_id = str(uuid.uuid4())
    params = _resolve_params(body)
    started = time.perf_counter()

    if os.getenv("CAMPUSPATH_DEMO_MODE", "").lower() in {"1", "true", "yes"}:
        usage = UsageInfo(input_tokens=0, output_tokens=0, total_tokens=0)
        return AgentResponse(
            answer=_demo_answer(route),
            mode="demo",
            request_id=request_id,
            route=route,
            model_params=params,
            usage=usage,
            cost=_estimate_cost_usd(params.model_id, usage),
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
        )

    try:
        answer, usage = await asyncio.to_thread(_invoke_bedrock, route, user_prompt, params)
        return AgentResponse(
            answer=answer,
            mode="bedrock",
            request_id=request_id,
            route=route,
            model_params=params,
            usage=usage,
            cost=_estimate_cost_usd(params.model_id, usage),
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
        )
    except Exception as exc:
        if os.getenv("ALLOW_DEMO_FALLBACK", "").lower() in {"1", "true", "yes"}:
            usage = UsageInfo(input_tokens=0, output_tokens=0, total_tokens=0)
            return AgentResponse(
                answer=_demo_answer(route),
                mode="demo",
                request_id=request_id,
                route=route,
                model_params=params,
                usage=usage,
                cost=_estimate_cost_usd(params.model_id, usage),
                latency_ms=round((time.perf_counter() - started) * 1000, 2),
            )
        reason = str(exc)
        if "INVALID_PAYMENT_INSTRUMENT" in reason:
            detail = (
                f"Model '{params.model_id}' is blocked by AWS Marketplace: this account needs a valid "
                "payment method before Anthropic models can be subscribed. Use an Amazon Nova model, "
                "or add a payment instrument in the AWS Billing console."
            )
        elif "AccessDeniedException" in reason:
            detail = (
                f"Access denied for model '{params.model_id}'. Request model access in the Amazon "
                "Bedrock console for this region, then try again."
            )
        elif "ValidationException" in reason and "on-demand throughput" in reason:
            detail = (
                f"Model '{params.model_id}' needs an inference profile ID. Use the profile-prefixed "
                "ID (for example 'global.' or 'apac.') instead of the bare model ID."
            )
        else:
            detail = (
                "CampusPath could not reach Amazon Bedrock. Check AWS credentials, region, and model "
                f"access. ({reason[:300]})"
            )
        raise HTTPException(status_code=503, detail=detail) from exc


@app.get("/", include_in_schema=False)
async def frontend() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/api/health")
async def health() -> dict[str, object]:
    return {
        "status": "healthy",
        "service": "CampusPath AI",
        "defaults": {
            "model_id": DEFAULT_MODEL_ID,
            "temperature": DEFAULT_TEMPERATURE,
            "top_p": DEFAULT_TOP_P,
            "max_tokens": DEFAULT_MAX_TOKENS,
        },
    }


@app.post("/api/placement-doubt", response_model=AgentResponse)
async def placement_doubt(body: AskRequest) -> AgentResponse:
    prompt = f"Student profile: {body.student_profile or 'Not provided'}\n\nQuestion: {body.question}"
    return await _answer("placement-doubt", prompt, body)


@app.post("/api/career-roadmap", response_model=AgentResponse)
async def career_roadmap(body: CareerRequest) -> AgentResponse:
    prompt = (
        f"Goal: {body.goal}\nDegree: {body.degree}\nCurrent year: {body.year}\n"
        f"Current skills: {body.skills}\nAvailable time: {body.hours_per_week} hours/week"
    )
    return await _answer("career-roadmap", prompt, body)


@app.post("/api/resume-review", response_model=AgentResponse)
async def resume_review(body: ResumeRequest) -> AgentResponse:
    prompt = f"Target role: {body.target_role}\n\nResume:\n{body.resume_text}"
    return await _answer("resume-review", prompt, body)


@app.post("/api/interview-prep", response_model=AgentResponse)
async def interview_prep(body: InterviewRequest) -> AgentResponse:
    prompt = (
        f"Target role: {body.role}\nInterview type: {body.interview_type}\n"
        f"Level: {body.experience_level}\nFocus topics: {body.focus_topics}"
    )
    return await _answer("interview-prep", prompt, body)
