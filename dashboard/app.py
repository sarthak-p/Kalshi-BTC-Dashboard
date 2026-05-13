"""
FastAPI dashboard backend.

Routes:
  GET  /           → index.html
  GET  /api/state  → current state snapshot (JSON)
  WS   /ws         → real-time state broadcast
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI(title="Kalshi BTC Dashboard")

STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/state")
async def api_state() -> JSONResponse:
    return JSONResponse(app.state.state_manager.to_dict())


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket) -> None:
    sm = app.state.state_manager
    await websocket.accept()
    sm.register_ws(websocket)
    import json
    await websocket.send_text(json.dumps(sm.to_dict()))
    try:
        while True:
            await websocket.receive_text()
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        sm.unregister_ws(websocket)
