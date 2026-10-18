"""Authenticated computer lifecycle and provider action endpoints."""

from __future__ import annotations

from typing import Any, Dict, Literal, Optional

import asyncio

from fastapi import APIRouter, HTTPException, WebSocket
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
import websockets

from app.services.auth_service import auth_service

from app.services.action_gateway import (
    ActionGatewayError,
    ActionInvocation,
    ActionPolicyError,
    action_gateway,
)
from app.services.computer_provider import ComputerProviderError, check_package_names, computer_provider
from app.services.secrets_service import redactor_for
from app.services.storage_service import storage_service
from app.services import computer_actions as _computer_actions  # noqa: F401 - registers actions


router = APIRouter(prefix="/api/v1/computers", tags=["computers"])


class ComputerActionRequest(BaseModel):
    action: Literal[
        "browser_navigate",
        "terminal_execute",
        "files_list",
        "files_read",
        "files_write",
        "files_edit",
        "send_input",
        "cleanup",
        "request_takeover",
        "install_packages",
    ]
    arguments: Dict[str, Any] = Field(default_factory=dict)


class ControlRequest(BaseModel):
    owner: Literal["bot", "user"]


def _ensure_bot(bot_id: str) -> None:
    if not any(bot.get("id") == bot_id for bot in storage_service.get_bots()):
        raise HTTPException(status_code=404, detail="Bot not found")


def _preview(action: str, bot_id: str, arguments: Dict[str, Any]) -> str:
    if action == "browser_navigate":
        return f"Navigate the computer for {bot_id} to {arguments.get('url') or '[missing URL]'}"
    if action == "terminal_execute":
        limit = arguments.get("timeout_seconds")
        return f"Run a terminal command on the computer for {bot_id}" + (f" (up to {limit} s)" if limit else "")
    if action == "files_list":
        return f"List computer files for {bot_id} at {arguments.get('path') or '/workspace'}"
    if action == "files_read":
        return f"Read {arguments.get('path') or '[missing path]'} from the computer for {bot_id}"
    if action == "files_write":
        content = arguments.get("content")
        size = len(content) if isinstance(content, str) else 0
        return f"Write {size} characters to {arguments.get('path') or '[missing path]'} in the computer for {bot_id}"
    if action == "files_edit":
        return f"Edit {arguments.get('path') or '[missing path]'} in the computer for {bot_id}"
    if action == "send_input":
        return f"Send input to the computer for {bot_id}"
    if action == "cleanup":
        return f"Remove the computer assigned to {bot_id}"
    if action == "request_takeover":
        return f"Ask the user to take over the computer for {bot_id}"
    if action == "install_packages":
        packages = arguments.get("packages")
        listed = ", ".join(packages) if isinstance(packages, list) else str(packages or "")
        return f"Install packages in the computer for {bot_id}: {listed[:160]}"
    if action == "set_control":
        return f"Give control of the computer for {bot_id} to the {arguments.get('owner') or 'bot'}"
    return f"Run computer action {action} for {bot_id}"


async def _run_action(
    bot_id: str,
    action: str,
    arguments: Optional[Dict[str, Any]] = None,
):
    _ensure_bot(bot_id)
    status = computer_provider.describe(bot_id)
    action_arguments = dict(arguments or {})
    # The URL path is the ownership boundary; callers cannot redirect an
    # action to another bot by smuggling ids inside its argument object.
    action_arguments.pop("bot_id", None)
    action_arguments.pop("computer_id", None)
    action_arguments.update({"bot_id": bot_id, "computer_id": status.computer_id})
    if action == "install_packages":
        # Checked here, as the model's path does, so a bad name is refused
        # before anyone is asked to approve it and the preview shows the
        # cleaned list that would actually be installed.
        try:
            action_arguments["packages"] = check_package_names(action_arguments.get("packages"))
        except ComputerProviderError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    call = ActionInvocation(
        name=f"computer.{action}",
        arguments=action_arguments,
        target={"bot_id": bot_id, "computer_id": status.computer_id},
        preview=_preview(action, bot_id, action_arguments),
    )
    try:
        request, approval = action_gateway.open(
            thread_id=f"computer:{bot_id}",
            bot_id=bot_id,
            call=call,
        )
    except ActionPolicyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if approval:
        return JSONResponse(
            status_code=202,
            content={
                "status": "pending_approval",
                "request": request.model_dump(),
                "approval": approval,
            },
        )

    try:
        result = await action_gateway.execute(request)
    except ActionGatewayError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if result.status != "completed":
        raise HTTPException(status_code=409, detail=result.error or "Computer action failed.")
    return {
        "status": "completed",
        "request": request.model_dump(),
        "result": redactor_for(bot_id).apply(result.result or {}),
    }

@router.get("/{bot_id}")
async def computer_status(bot_id: str):
    _ensure_bot(bot_id)
    status = computer_provider.describe(bot_id)
    return {
        "status": status.to_dict(),
        "created": status.generation > 0,
    }


@router.post("/{bot_id}/create")
async def create_computer(bot_id: str):
    return await _run_action(bot_id, "create")


@router.post("/{bot_id}/start")
async def start_computer(bot_id: str):
    return await _run_action(bot_id, "start")


@router.post("/{bot_id}/pause")
async def pause_computer(bot_id: str):
    return await _run_action(bot_id, "pause")


@router.post("/{bot_id}/stop")
async def stop_computer(bot_id: str):
    return await _run_action(bot_id, "stop")


@router.post("/{bot_id}/reset")
async def reset_computer(bot_id: str):
    return await _run_action(bot_id, "reset")


@router.get("/{bot_id}/health")
async def health_computer(bot_id: str):
    return await _run_action(bot_id, "health")


@router.get("/{bot_id}/screenshot")
async def screenshot_computer(bot_id: str):
    return await _run_action(bot_id, "screenshot")


@router.post("/{bot_id}/control")
async def set_computer_control(bot_id: str, request: ControlRequest):
    """Hand the desktop to the user (view + input) or back to the bot."""
    return await _run_action(bot_id, "set_control", {"owner": request.owner})


def websocket_is_authenticated(websocket: WebSocket) -> bool:
    """The HTTP auth middleware does not see WebSocket upgrades, so check here."""
    return auth_service.authenticate_websocket(websocket.headers, websocket.cookies) is not None


@router.websocket("/{bot_id}/vnc")
async def computer_vnc(websocket: WebSocket, bot_id: str):
    """Bridge the browser's noVNC client to the sandbox's VNC server.

    The sandbox publishes no ports; its driver exposes VNC over a WebSocket
    that requires the per-computer token, which only this process knows. The
    user's session is checked first, so the desktop is reachable only by a
    signed-in user through this route.
    """
    if not websocket_is_authenticated(websocket):
        await websocket.close(code=4401)
        return
    if not any(bot.get("id") == bot_id for bot in storage_service.get_bots()):
        await websocket.close(code=4404)
        return
    status = computer_provider.describe(bot_id)
    target = computer_provider.vnc_target(status.computer_id) if hasattr(computer_provider, "vnc_target") else None
    if not target:
        await websocket.close(code=4409)
        return

    requested = websocket.headers.get("sec-websocket-protocol")
    subprotocol = requested.split(",")[0].strip() if requested else None
    await websocket.accept(subprotocol=subprotocol)

    upstream_url = f"ws://{target['host']}:{target['port']}/vnc"
    try:
        async with websockets.connect(
            upstream_url,
            additional_headers={"x-computer-token": target["token"]},
            max_size=None,
            ping_interval=None,
            open_timeout=10,
        ) as upstream:

            async def client_to_sandbox():
                while True:
                    message = await websocket.receive()
                    if message.get("type") == "websocket.disconnect":
                        return
                    if message.get("bytes") is not None:
                        await upstream.send(message["bytes"])
                    elif message.get("text"):
                        await upstream.send(message["text"].encode("utf-8"))

            async def sandbox_to_client():
                async for frame in upstream:
                    if isinstance(frame, (bytes, bytearray)):
                        await websocket.send_bytes(bytes(frame))
                    else:
                        await websocket.send_text(str(frame))

            tasks = [asyncio.create_task(client_to_sandbox()), asyncio.create_task(sandbox_to_client())]
            _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
    except Exception:
        pass
    finally:
        try:
            await websocket.close()
        except Exception:
            pass


@router.post("/{bot_id}/actions")
async def run_computer_action(bot_id: str, action: ComputerActionRequest):
    """Open a provider action; writes return 202 until approval is resolved."""

    return await _run_action(bot_id, action.action, action.arguments)


@router.post("/{bot_id}/actions/{request_id}/execute")
async def execute_approved_computer_action(bot_id: str, request_id: str):
    _ensure_bot(bot_id)
    request = action_gateway.get_pending_request(request_id)
    if request is None or request.bot_id != bot_id or request.tool != "computer":
        raise HTTPException(status_code=404, detail="Computer action request not found")

    decision = await action_gateway.wait_for_decision(request)
    if decision != "allow":
        return {"status": decision, "request": request.model_dump()}
    result = await action_gateway.execute(request)
    if result.status != "completed":
        raise HTTPException(status_code=409, detail=result.error or "Computer action failed.")
    return {
        "status": "completed",
        "request": request.model_dump(),
        "result": redactor_for(bot_id).apply(result.result or {}),
    }
