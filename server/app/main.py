from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import settings
from app.routers import auth, bots, models, chat, approvals, upload, settings as settings_router, connectors, audit, computers, tools, files, gate, commands, secrets
from app.services.auth_service import auth_service
from app.services.llm_service import current_llm_config

app = FastAPI(
    title="Cubicle API",
    description="FastAPI backend for Cubicle. Talks to any OpenAI-compatible LLM server (OpenRouter, Ollama, LM Studio, llama.cpp, vLLM) or the legacy MUAPI API.",
    version="1.0.0"
)

# Configure CORS for Next.js frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

PUBLIC_API_PATHS = {
    "/api/v1/health",
    "/api/v1/auth/status",
    "/api/v1/auth/session",
    "/api/v1/auth/login",
    "/api/v1/auth/logout",
}


@app.middleware("http")
async def require_authentication(request: Request, call_next):
    path = request.url.path
    if (
        request.method == "OPTIONS"
        or not path.startswith("/api/v1")
        or path in PUBLIC_API_PATHS
    ):
        return await call_next(request)

    user = auth_service.authenticate_request(request)
    if not user:
        return JSONResponse(
            {"detail": "Authentication is required."},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )
    request.state.user = user
    return await call_next(request)

app.include_router(auth.router)
app.include_router(bots.router)
app.include_router(models.router)
app.include_router(chat.router)
app.include_router(upload.router)
app.include_router(approvals.router)
app.include_router(settings_router.router)
app.include_router(connectors.router)
app.include_router(audit.router)
app.include_router(computers.router)
app.include_router(tools.router)
app.include_router(files.router)
app.include_router(gate.router)
app.include_router(commands.router)
app.include_router(secrets.router)


@app.get("/api/v1/health")
async def health_check():
    llm_config = current_llm_config()
    return {
        "status": "online",
        "service": "Cubicle FastAPI Backend",
        "provider": llm_config.provider,
        "provider_base_url": llm_config.base_url,
        "computer_provider": settings.COMPUTER_PROVIDER,
        "default_model": llm_config.default_model,
    }
