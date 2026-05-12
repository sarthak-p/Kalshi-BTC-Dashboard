"""
FastAPI dashboard backend.

Routes:
  GET  /           → index.html
  GET  /api/state  → current state snapshot (JSON)
  POST /api/kill   → activate kill switch
  WS   /ws         → real-time state broadcast
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# StateManager is injected at startup via app.state
app = FastAPI(title="Kalshi BTC Bot")

STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/state")
async def api_state() -> JSONResponse:
    sm = app.state.state_manager
    return JSONResponse(sm.to_dict())


@app.post("/api/kill")
async def api_kill() -> JSONResponse:
    sm = app.state.state_manager
    await sm.activate_kill_switch()
    return JSONResponse({"ok": True, "kill_switch": True})


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket) -> None:
    sm = app.state.state_manager
    await websocket.accept()
    sm.register_ws(websocket)
    # Push current state immediately on connect
    import json
    await websocket.send_text(json.dumps(sm.to_dict()))
    try:
        while True:
            # Keep the connection alive; broadcasts come from StateManager
            await websocket.receive_text()
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        sm.unregister_ws(websocket)
