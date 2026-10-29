import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import settings
from app.services import user_questions as _user_questions  # noqa: F401 - registers user.ask
from app.routers import admin, questions, attachments, auth, bots, models, chat, approvals, upload, settings as settings_router, connectors, audit, computers, tools, files, gate, commands, secrets, schedules, mcp, search, conversations, projects
from app.routers import sharing as sharing_router
from app.routers import help as help_router
from app.services.mcp_service import mcp_manager
from app.services.scheduler import scheduler
from app.services.computer_provider import computer_provider
from app.services.auth_service import auth_service
from app.services import sharing
from app.services.identity import reset_current_actor, reset_current_owner, set_current_actor, set_current_owner
from app.services.llm_service import current_llm_config


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Computers a previous API process started are still running; take
    # them over rather than report them stopped and replace them later.
    adopt = getattr(computer_provider, "adopt_running", None)
    if adopt is not None:
        try:
            await adopt()
        except Exception as exc:  # Startup must not fail over Docker.
            logging.getLogger(__name__).warning("Could not adopt running computers: %s", exc)
    # Due schedules post their prompt into the bot's chat and start a turn
    # through the chat router, exactly as a user message would.
    if settings.SCHEDULER_ENABLED:
        scheduler.start(chat.start_scheduled_turn)
    # MCP servers connect in the background; the first turn that needs
    # them finds their tools listed.
    mcp_manager.start_all()
    yield
    await mcp_manager.stop_all()
    await scheduler.stop()


app = FastAPI(
    title="Cubicle API",
    description="FastAPI backend for Cubicle. Talks to any OpenAI-compatible LLM server (OpenRouter, Ollama, LM Studio, llama.cpp, vLLM) or the legacy MUAPI API.",
    version="1.0.0",
    lifespan=lifespan,
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
    "/api/v1/auth/setup",
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
    # Everything this request reads or writes belongs to this account, unless
    # it is about a bot shared with them: then it acts in the bot owner's data,
    # still as this person.
    token = set_current_owner(user["id"])
    actor_token = set_current_actor(user["id"])
    try:
        refusal = sharing.route(request.method, path, dict(request.query_params), user["id"])
        if refusal is not None:
            return JSONResponse({"detail": refusal.detail}, status_code=refusal.status)
        return await call_next(request)
    finally:
        reset_current_actor(actor_token)
        reset_current_owner(token)

app.include_router(auth.router)
app.include_router(help_router.router)
app.include_router(bots.router)
app.include_router(conversations.router)
app.include_router(projects.router)
app.include_router(sharing_router.router)
app.include_router(models.router)
app.include_router(search.router)
app.include_router(chat.router)
app.include_router(upload.router)
app.include_router(attachments.router)
app.include_router(approvals.router)
app.include_router(questions.router)
app.include_router(admin.router)
app.include_router(settings_router.router)
app.include_router(connectors.router)
app.include_router(audit.router)
app.include_router(computers.router)
app.include_router(tools.router)
app.include_router(files.router)
app.include_router(gate.router)
app.include_router(commands.router)
app.include_router(secrets.router)
app.include_router(schedules.router)
app.include_router(mcp.router)


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
