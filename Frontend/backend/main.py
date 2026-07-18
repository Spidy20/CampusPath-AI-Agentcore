"""FastAPI gateway for the CampusPath AI placement assistant."""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
import urllib.error
import urllib.request
import uuid
from datetime import date
from pathlib import Path
from typing import Literal

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from strands import Agent
from strands.models.bedrock import BedrockModel
from .prompt_templates import (
    BASE_SYSTEM_PROMPT,
    ROUTE_PROMPTS,
    build_datetime_context,
    build_tagged_student_request,
)


FRONTEND_DIR = Path(__file__).resolve().parents[1]
DEFAULT_MODEL_ID = os.getenv(
    "BEDROCK_MODEL_ID",
    "apac.amazon.nova-pro-v1:0",
)
DEFAULT_TEMPERATURE = float(os.getenv("BEDROCK_TEMPERATURE", "0.4"))
DEFAULT_TOP_P = float(os.getenv("BEDROCK_TOP_P", "0.9"))
DEFAULT_MAX_TOKENS = int(os.getenv("BEDROCK_MAX_TOKENS", "4096"))

# Resume-driven workflows get a stronger default model for nuanced feedback.
RESUME_WORKFLOWS_MODEL_ID = os.getenv(
    "BEDROCK_RESUME_MODEL_ID",
    "apac.anthropic.claude-3-7-sonnet-20250219-v1:0",
)
ROUTE_MODEL_DEFAULTS = {
    "resume-review": RESUME_WORKFLOWS_MODEL_ID,
    "interview-prep": RESUME_WORKFLOWS_MODEL_ID,
}

# Where each UI environment sends workflow requests.
# dev  -> local AgentCore runtime (`agentcore dev`), falling back to direct
#         Bedrock if the local runtime is not running.
# prod -> the deployed AgentCore gateway.
AGENTCORE_DEV_URL = os.getenv("AGENTCORE_DEV_URL", "http://localhost:8080/invocations")
AGENTCORE_PROD_URL = os.getenv(
    "AGENTCORE_PROD_URL",
    "https://gateway-quick-start-5f89e2-uebqiqlxy2.gateway.bedrock-agentcore."
    "ap-south-1.amazonaws.com/target-quick-start-1505a4/invocations",
)
AGENTCORE_GATEWAY_TOKEN = os.getenv("AGENTCORE_GATEWAY_TOKEN", "")
AGENTCORE_TIMEOUT_SECONDS = float(os.getenv("AGENTCORE_TIMEOUT_SECONDS", "180"))

AWS_REGION = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "ap-south-1"
RESUME_BUCKET_NAME = os.getenv("RESUME_BUCKET_NAME", "sagemaker-tutorials-mlhub")
RESUME_KEY_PREFIX = os.getenv("RESUME_KEY_PREFIX", "resume-uploads/")
RESUME_EXTRACTOR_FUNCTION_NAME = os.getenv(
    "RESUME_EXTRACTOR_FUNCTION_NAME",
    "campuspath-resume-pdf-extractor",
)
MAX_RESUME_UPLOAD_BYTES = int(os.getenv("MAX_RESUME_UPLOAD_BYTES", str(5 * 1024 * 1024)))
MAX_RESUME_TEXT_CHARS = 20000
SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")

ROLE_PATTERN = re.compile(
    r"(?i)\b"
    r"(?:(?:senior|junior|lead|principal|staff|associate|graduate|trainee)\s+)?"
    r"(?:(?:software|backend|back[- ]end|frontend|front[- ]end|full[- ]?stack|data|ml|"
    r"machine\s+learning|ai|devops|cloud|mobile|android|ios|web|qa|test|security|"
    r"platform|site\s+reliability|embedded|systems?|network|database|business|financial|"
    r"product|research)\s+)?"
    r"(?:engineer|developer|scientist|analyst|architect|designer|consultant|"
    r"administrator|programmer|manager|specialist|researcher|intern)\b"
)
EXPERIENCE_HEADER_PATTERN = re.compile(
    r"(?i)^\s*(?:work|professional|employment|industry)?\s*"
    r"(?:experience|history|internships?)\s*:?\s*$"
)
SKILLS_SECTION_PATTERN = re.compile(
    r"(?i)^\s*(?:technical\s+)?(?:skills?|projects?|education|certifications?|"
    r"achievements?|awards?)\b"
)
ROLE_CASING_FIXES = {
    "Devops": "DevOps", "Ml": "ML", "Ai": "AI", "Ios": "iOS", "Qa": "QA",
    "Sql": "SQL", "Api": "API",
}


def _titleize_role(raw: str) -> str:
    words = raw.split()
    return " ".join(ROLE_CASING_FIXES.get(w.title(), w.title()) for w in words)


def _suggest_role(resume_text: str) -> str | None:
    """Best-effort role title: most recent job in the experience section first,
    then the resume headline, then anywhere in the document."""
    lines = [line.strip() for line in resume_text.splitlines() if line.strip()]

    # Resumes list experience newest-first, so the first title after the
    # experience header is the candidate's latest role.
    for index, line in enumerate(lines):
        if not EXPERIENCE_HEADER_PATTERN.match(line):
            continue
        for candidate in lines[index + 1 : index + 16]:
            if SKILLS_SECTION_PATTERN.match(candidate):
                break
            match = ROLE_PATTERN.search(candidate)
            if match:
                return _titleize_role(match.group(0))
        break

    for chunk in lines[:20] + [resume_text]:
        match = ROLE_PATTERN.search(chunk)
        if match:
            return _titleize_role(match.group(0))
    return None

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
    environment: Literal["dev", "prod"] = "dev"


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
    interview_date: date | None = None
    focus_topics: str = Field(default="General placement preparation", max_length=1000)
    resume_text: str | None = Field(default=None, max_length=20000)


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
    mode: Literal["bedrock", "demo", "agentcore-dev", "agentcore-prod"]
    request_id: str
    route: str
    model_params: ModelParams
    usage: UsageInfo
    cost: CostInfo
    latency_ms: float


class ResumeExtractResponse(BaseModel):
    ok: bool = True
    filename: str
    content_type: str
    page_count: int
    character_count: int
    resume_text: str
    suggested_role: str | None = None
    warnings: list[str] = Field(default_factory=list)
    request_id: str
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


def _resolve_params(route: str, body: ModelParamsMixin) -> ModelParams:
    return ModelParams(
        model_id=body.model_id or ROUTE_MODEL_DEFAULTS.get(route, DEFAULT_MODEL_ID),
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
        system_prompt=(
            f"{BASE_SYSTEM_PROMPT}\n\n"
            f"{build_datetime_context()}\n\n"
            f"{ROUTE_PROMPTS[route]}"
        ),
        tools=[],
    )
    result = agent(build_tagged_student_request(route, user_prompt))
    answer = _extract_text(result)
    if not answer:
        raise RuntimeError("The model returned an empty response.")
    return answer, _extract_usage(result)


def _invoke_agentcore(route: str, user_prompt: str, params: ModelParams, environment: str) -> dict:
    """Send the workflow request to an AgentCore runtime (local dev or gateway)."""
    url = AGENTCORE_PROD_URL if environment == "prod" else AGENTCORE_DEV_URL
    payload = {
        "route": route,
        "prompt": user_prompt,
        "model_id": params.model_id,
        "temperature": params.temperature,
        "top_p": params.top_p,
        "max_tokens": params.max_tokens,
    }
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": f"campuspath-ui-{uuid.uuid4().hex}",
    }
    if environment == "prod" and AGENTCORE_GATEWAY_TOKEN:
        headers["Authorization"] = f"Bearer {AGENTCORE_GATEWAY_TOKEN}"

    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=AGENTCORE_TIMEOUT_SECONDS) as response:
        body = json.loads(response.read().decode("utf-8") or "{}")

    if not isinstance(body, dict) or not body.get("ok", True) or not body.get("answer"):
        error = body.get("error") if isinstance(body, dict) else None
        message = error.get("message") if isinstance(error, dict) else str(error or "AgentCore returned no answer.")
        raise RuntimeError(message)
    return body


def _agentcore_response(
    body: dict, route: str, params: ModelParams, environment: str, request_id: str, started: float
) -> AgentResponse:
    usage_raw = body.get("usage") or {}
    usage = UsageInfo(
        input_tokens=int(usage_raw.get("input_tokens") or 0),
        output_tokens=int(usage_raw.get("output_tokens") or 0),
        total_tokens=int(usage_raw.get("total_tokens") or 0),
    )
    params_raw = body.get("model_params") or {}
    resolved = ModelParams(
        model_id=str(params_raw.get("model_id") or params.model_id),
        temperature=float(params_raw.get("temperature", params.temperature)),
        top_p=float(params_raw.get("top_p", params.top_p)),
        max_tokens=int(params_raw.get("max_tokens", params.max_tokens)),
    )
    return AgentResponse(
        answer=str(body.get("answer") or ""),
        mode=f"agentcore-{environment}",
        request_id=str(body.get("request_id") or request_id),
        route=route,
        model_params=resolved,
        usage=usage,
        cost=_estimate_cost_usd(resolved.model_id, usage),
        latency_ms=round((time.perf_counter() - started) * 1000, 2),
    )


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
    params = _resolve_params(route, body)
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

    environment = getattr(body, "environment", "dev")

    if environment == "prod":
        try:
            result = await asyncio.to_thread(
                _invoke_agentcore, route, user_prompt, params, "prod"
            )
            return _agentcore_response(result, route, params, "prod", request_id, started)
        except (urllib.error.URLError, TimeoutError) as exc:
            raise HTTPException(
                status_code=503,
                detail=(
                    "Could not reach the deployed AgentCore gateway. Check AGENTCORE_PROD_URL, "
                    "network access, and gateway credentials, or switch back to Dev."
                ),
            ) from exc
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=502,
                detail=f"AgentCore gateway request failed: {str(exc)[:300]}",
            ) from exc

    # Dev: prefer the local AgentCore runtime; fall back to direct Bedrock so
    # the app still works when `agentcore dev` is not running.
    try:
        result = await asyncio.to_thread(_invoke_agentcore, route, user_prompt, params, "dev")
        return _agentcore_response(result, route, params, "dev", request_id, started)
    except (urllib.error.URLError, TimeoutError):
        pass

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


def _s3_client():
    return boto3.client("s3", region_name=AWS_REGION)


def _lambda_client():
    return boto3.client("lambda", region_name=AWS_REGION)


def _safe_filename(filename: str | None) -> str:
    raw = Path(filename or "resume.pdf").name
    cleaned = SAFE_FILENAME_RE.sub("_", raw).strip("._") or "resume.pdf"
    if not cleaned.lower().endswith(".pdf"):
        cleaned = f"{cleaned}.pdf"
    return cleaned[:120]


def _validate_pdf_upload(filename: str, content_type: str | None, pdf_bytes: bytes) -> None:
    if not pdf_bytes:
        raise HTTPException(status_code=400, detail="Uploaded resume PDF is empty.")
    if len(pdf_bytes) > MAX_RESUME_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Resume PDFs must be {MAX_RESUME_UPLOAD_BYTES // (1024 * 1024)} MB or smaller.",
        )
    lowered_name = filename.lower()
    lowered_type = (content_type or "").lower()
    allowed_types = {"application/pdf", "application/x-pdf", "binary/octet-stream", "application/octet-stream", ""}
    if not lowered_name.endswith(".pdf") and lowered_type not in allowed_types:
        raise HTTPException(status_code=400, detail="Only PDF resumes are supported.")
    if lowered_type and lowered_type not in allowed_types and "pdf" not in lowered_type:
        raise HTTPException(status_code=400, detail="Only PDF resumes are supported.")
    if not pdf_bytes.startswith(b"%PDF"):
        raise HTTPException(status_code=400, detail="Uploaded file is not a valid PDF.")


def _upload_resume_pdf(pdf_bytes: bytes, filename: str) -> str:
    key = f"{RESUME_KEY_PREFIX}{uuid.uuid4().hex}/{filename}"
    _s3_client().put_object(
        Bucket=RESUME_BUCKET_NAME,
        Key=key,
        Body=pdf_bytes,
        ContentType="application/pdf",
        Metadata={"source": "campuspath-resume-review"},
    )
    return key


def _delete_resume_object(key: str) -> None:
    try:
        _s3_client().delete_object(Bucket=RESUME_BUCKET_NAME, Key=key)
    except (BotoCoreError, ClientError):
        # Cleanup is best-effort; extraction already completed or failed.
        return


def _invoke_resume_extractor(key: str, filename: str) -> dict:
    payload = {
        "bucket": RESUME_BUCKET_NAME,
        "key": key,
        "filename": filename,
    }
    response = _lambda_client().invoke(
        FunctionName=RESUME_EXTRACTOR_FUNCTION_NAME,
        InvocationType="RequestResponse",
        Payload=json.dumps(payload).encode("utf-8"),
    )
    raw = response["Payload"].read()
    try:
        envelope = json.loads(raw.decode("utf-8") or "{}")
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=502,
            detail="Resume extractor returned an invalid response.",
        ) from exc

    if response.get("FunctionError"):
        error_message = envelope.get("errorMessage") or "Resume extractor failed."
        raise HTTPException(status_code=502, detail=str(error_message)[:300])

    # Direct Lambda invoke returns either the handler dict or an API-style envelope.
    if isinstance(envelope, dict) and "statusCode" in envelope and "body" in envelope:
        status_code = int(envelope.get("statusCode") or 500)
        body_raw = envelope.get("body")
        try:
            body = json.loads(body_raw) if isinstance(body_raw, str) else (body_raw or {})
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=502,
                detail="Resume extractor returned an invalid response body.",
            ) from exc
        if status_code >= 400 or not body.get("ok"):
            raise HTTPException(
                status_code=min(max(status_code, 400), 499) if 400 <= status_code < 500 else 502,
                detail=str(body.get("error") or "Resume text extraction failed."),
            )
        return body

    if not isinstance(envelope, dict) or not envelope.get("ok"):
        raise HTTPException(
            status_code=502,
            detail=str(envelope.get("error") if isinstance(envelope, dict) else "Resume text extraction failed."),
        )
    return envelope


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
        "route_model_defaults": ROUTE_MODEL_DEFAULTS,
        "environments": {
            "dev": AGENTCORE_DEV_URL,
            "prod": AGENTCORE_PROD_URL,
        },
        "resume_extraction": {
            "bucket": RESUME_BUCKET_NAME,
            "function_name": RESUME_EXTRACTOR_FUNCTION_NAME,
            "region": AWS_REGION,
            "max_upload_mb": MAX_RESUME_UPLOAD_BYTES // (1024 * 1024),
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


@app.post("/api/resume-extract", response_model=ResumeExtractResponse)
async def resume_extract(file: UploadFile = File(...)) -> ResumeExtractResponse:
    """Upload a resume PDF to S3, extract text via Lambda, then delete the object."""
    request_id = str(uuid.uuid4())
    started = time.perf_counter()
    filename = _safe_filename(file.filename)
    pdf_bytes = await file.read()
    _validate_pdf_upload(filename, file.content_type, pdf_bytes)

    object_key: str | None = None
    try:
        object_key = await asyncio.to_thread(_upload_resume_pdf, pdf_bytes, filename)
        result = await asyncio.to_thread(_invoke_resume_extractor, object_key, filename)
    except HTTPException:
        raise
    except (BotoCoreError, ClientError) as exc:
        raise HTTPException(
            status_code=503,
            detail=(
                "CampusPath could not reach S3 or the resume extractor Lambda. "
                "Check AWS credentials, region, bucket access, and that the SAM stack is deployed."
            ),
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=502,
            detail="Resume extraction failed unexpectedly.",
        ) from exc
    finally:
        if object_key:
            await asyncio.to_thread(_delete_resume_object, object_key)

    resume_text = str(result.get("resume_text") or "").strip()
    if len(resume_text) < 50:
        raise HTTPException(
            status_code=422,
            detail=(
                "Could not extract enough selectable text from the PDF. "
                "Paste the resume text manually, or upload a text-based PDF."
            ),
        )
    if len(resume_text) > MAX_RESUME_TEXT_CHARS:
        resume_text = resume_text[:MAX_RESUME_TEXT_CHARS].rstrip()
        warnings = list(result.get("warnings") or [])
        warnings.append(
            f"Extracted text was truncated to {MAX_RESUME_TEXT_CHARS} characters for resume review."
        )
    else:
        warnings = list(result.get("warnings") or [])

    return ResumeExtractResponse(
        filename=str(result.get("filename") or filename),
        content_type="application/pdf",
        page_count=int(result.get("page_count") or 0),
        character_count=len(resume_text),
        resume_text=resume_text,
        suggested_role=_suggest_role(resume_text),
        warnings=warnings,
        request_id=request_id,
        latency_ms=round((time.perf_counter() - started) * 1000, 2),
    )


@app.post("/api/resume-review", response_model=AgentResponse)
async def resume_review(body: ResumeRequest) -> AgentResponse:
    prompt = f"Target role: {body.target_role}\n\nResume:\n{body.resume_text}"
    return await _answer("resume-review", prompt, body)


@app.post("/api/interview-prep", response_model=AgentResponse)
async def interview_prep(body: InterviewRequest) -> AgentResponse:
    prompt = (
        f"Target role: {body.role}\nInterview type: {body.interview_type}\n"
        f"Level: {body.experience_level}\n"
        f"Interview date: {body.interview_date.isoformat() if body.interview_date else 'Not provided'}\n"
        f"Focus topics: {body.focus_topics}"
    )
    if body.resume_text and body.resume_text.strip():
        prompt += (
            "\n\nCandidate resume (tailor questions to this experience):\n"
            f"{body.resume_text.strip()}"
        )
    return await _answer("interview-prep", prompt, body)
