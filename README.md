# CampusPath AI

CampusPath AI is an agentic placement copilot for engineering students. It
combines Amazon Bedrock, Strands Agents, Amazon Bedrock AgentCore, FastAPI, and
a responsive web interface to turn placement questions into concrete next
steps.

Built for the demo **“AI Engineering with AWS: Agents, Prompting, and
Real-World Deployment.”**

## Student experiences

- **Placement Coach** — answers preparation, eligibility, strategy, and
  placement-process questions.
- **Career Roadmap** — builds a skill and project roadmap around a target role.
- **Resume Review** — provides ATS-aware feedback without inventing
  achievements.
- **Interview Prep** — generates role-specific questions, revision topics, and
  mock exercises.

## Architecture

```text
Student browser
      │
      ▼
FastAPI gateway ──────► Strands Agent ──────► Amazon Bedrock
      │
      └── serves the responsive frontend

AgentCore runtime ────► CampusPath Strands agent (deployable to AWS)
```

The FastAPI app is ideal for the live web demo. The generated AgentCore runtime
remains independently deployable and uses the same CampusPath persona.

## Project structure

```text
CampusPath-AI/
├── README.md
├── Commands
├── campuspath-venv/          # Shared local Python environment
├── Frontend/
│   ├── index.html
│   ├── styles.css
│   ├── app.js
│   └── backend/
│       ├── main.py
│       └── requirements.txt
└── CampusAgent/              # AgentCore application only
    ├── app/CampusAgent/       # Strands + AgentCore runtime
    └── agentcore/             # AgentCore deployment configuration
```

## Run the web application

### 1. Configure AWS

Use an AWS account with access to the configured Bedrock model:

```bash
aws configure
export AWS_REGION=ap-south-1
```

Optionally choose a different Bedrock model:

```bash
export BEDROCK_MODEL_ID="global.amazon.nova-2-lite-v1:0"
```

CampusPath applies custom prompt-level guardrails for prompt injection,
fabricated claims, cheating, sensitive information, abusive language, and
off-topic requests. It does not require or configure an Amazon Bedrock
Guardrail resource.

### 2. Install and start

```bash
source campuspath-venv/bin/activate
pip install -r Frontend/backend/requirements.txt
cd Frontend
uvicorn backend.main:app --reload --port 8000
```

Open [http://localhost:8000](http://localhost:8000). Interactive API
documentation is available at
[http://localhost:8000/docs](http://localhost:8000/docs).

### Offline demo safety net

To run the full UI without making Bedrock calls:

```bash
export CAMPUSPATH_DEMO_MODE=true
uvicorn backend.main:app --port 8000
```

To try Bedrock first and automatically use a clearly labelled sample response
if AWS is unavailable:

```bash
export ALLOW_DEMO_FALLBACK=true
```

## API routes

### FastAPI web app (`Frontend/backend`)

| Method | Route | Purpose |
| --- | --- | --- |
| `GET` | `/api/health` | Service readiness |
| `POST` | `/api/placement-doubt` | Placement coaching |
| `POST` | `/api/career-roadmap` | Personalized career plan |
| `POST` | `/api/resume-review` | Resume and ATS feedback |
| `POST` | `/api/interview-prep` | Interview preparation pack |

Optional body fields on every FastAPI route: `model_id`, `temperature`,
`top_p`, `max_tokens`. Responses include the resolved model params, Bedrock
token usage, approximate USD cost, and latency.

### AgentCore runtime (`CampusAgent`)

AgentCore exposes one endpoint: `POST /invocations`. Choose a workflow with
the required body field `route` (alias: `action`):

| `route` value | Purpose |
| --- | --- |
| `placement-doubt` | Placement coaching |
| `career-roadmap` | Personalized career plan |
| `resume-review` | Resume and ATS feedback |
| `interview-prep` | Interview preparation pack |

Missing or unknown routes return a validation error. Import
`CampusPath-AI-AgentCore.postman_collection.json` for ready-made examples.

Example:

```bash
curl -X POST http://localhost:8000/api/placement-doubt \
  -H "Content-Type: application/json" \
  -d '{
    "question": "How should I prepare for placements in the next 30 days?",
    "student_profile": "Third-year CSE student, Python and basic DSA"
  }'
```

## Run the AgentCore runtime

The AgentCore project has already been generated. From the repository root:

```bash
source campuspath-venv/bin/activate
cd CampusAgent
agentcore dev
```

In another terminal:

```bash
cd CampusAgent
agentcore invoke --dev "Create a 30-day placement plan for a third-year CSE student"
```

Validate and deploy when AWS credentials and the deployment target are ready:

```bash
agentcore validate
agentcore deploy
```

## Demo flow

1. Show the four focused experiences instead of a generic chatbot.
2. Generate a career roadmap to demonstrate structured prompting.
3. Review a sample resume to explain task-specific agent instructions.
4. Open `/docs` to show production-friendly API contracts.
5. Explain how the same Strands agent can run on Bedrock AgentCore.

## Responsible-use notes

CampusPath provides guidance, not placement guarantees. Students should verify
company-specific eligibility and hiring details with their placement cell. Do
not paste phone numbers, addresses, government IDs, or other sensitive data into
the resume reviewer during a public demo.
