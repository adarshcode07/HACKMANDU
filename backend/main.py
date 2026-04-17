"""
SentinelAI — FastAPI Backend
Real-time threat detection server with WebSocket streaming to the SOC dashboard.
"""
import asyncio
import json
import threading
import time
from collections import deque
from typing import Set
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware


# import log_generator, detection_engine, playbook_engine
from log_generator import LogGenerator
from detection_engine import DetectionEngine
from playbook_engine import generate_playbook

app = FastAPI(title="SentinelAI API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── Shared state ─────────────────────────────────────────────────────────────

class SentinelState:
    def __init__(self):
        self.alerts: deque = deque(maxlen=500)
        self.events_processed: int = 0
        self.alerts_total: int = 0
        self.start_time: float = time.time()
        self.ws_clients: Set[WebSocket] = set()
        self.lock = asyncio.Lock()
        self.engine = DetectionEngine()
        self.generator = LogGenerator(events_per_second=15)
        self._running = False

    def get_stats(self) -> dict:
        uptime_s = time.time() - self.start_time
        return {
            "events_processed": self.events_processed,
            "alerts_total": self.alerts_total,
            "alerts_active": len([a for a in self.alerts if a.get("severity") in ("Critical","High")]),
            "uptime_s": round(uptime_s),
            "eps": round(self.events_processed / max(uptime_s, 1), 1),
        }

state = SentinelState()


# ─── WebSocket manager ────────────────────────────────────────────────────────

async def broadcast(msg: dict):
    """Send message to all connected WebSocket clients."""
    if not state.ws_clients:
        return
    dead = set()
    payload = json.dumps(msg)
    for ws in state.ws_clients:
        try:
            await ws.send_text(payload)
        except Exception:
            dead.add(ws)
    state.ws_clients -= dead


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    state.ws_clients.add(ws)
    try:
        # Send current state on connect
        await ws.send_text(json.dumps({
            "type": "init",
            "alerts": [a for a in state.alerts],
            "stats": state.get_stats()
        }))
        while True:
            # Keep connection alive
            await asyncio.sleep(30)
            await ws.send_text(json.dumps({"type": "ping"}))
    except WebSocketDisconnect:
        state.ws_clients.discard(ws)
    except Exception:
        state.ws_clients.discard(ws)


# ─── REST endpoints ───────────────────────────────────────────────────────────

@app.get("/api/health")
def health():
    return {"status": "ok", "service": "SentinelAI"}

@app.get("/api/stats")
def get_stats():
    return state.get_stats()

@app.get("/api/alerts")
def get_alerts(limit: int = 50, threat_type: str = None, severity: str = None):
    alerts = list(state.alerts)
    if threat_type:
        alerts = [a for a in alerts if a.get("threat_type") == threat_type]
    if severity:
        alerts = [a for a in alerts if a.get("severity") == severity]
    return {"alerts": alerts[-limit:], "total": len(alerts)}

@app.get("/api/alerts/{alert_id}")
def get_alert(alert_id: str):
    for a in state.alerts:
        if a.get("alert_id") == alert_id:
            return a
    raise HTTPException(status_code=404, detail="Alert not found")

@app.post("/api/alerts/{alert_id}/playbook")
async def get_playbook(alert_id: str):
    """Generate or retrieve playbook for an alert."""
    for a in state.alerts:
        if a.get("alert_id") == alert_id:
            if not a.get("playbook"):
                a["playbook"] = await generate_playbook(a, use_llm=True)
            return {"playbook": a["playbook"]}
    raise HTTPException(status_code=404, detail="Alert not found")

@app.post("/api/simulate/{scenario}")
async def trigger_scenario(scenario: str):
    """
    Manually trigger an attack scenario for demo purposes.
    scenario: brute_force | c2_beacon | lateral_movement | data_exfil | false_positive
    """
    valid = ["brute_force", "c2_beacon", "lateral_movement", "data_exfil", "false_positive"]
    if scenario not in valid:
        raise HTTPException(400, f"Invalid scenario. Choose from: {valid}")
    await broadcast({"type": "simulation_triggered", "scenario": scenario})
    # The generator will inject this scenario; we just flag it
    return {"status": "triggered", "scenario": scenario}

@app.get("/api/mitre")
def get_mitre_coverage():
    """Return MITRE ATT&CK techniques covered."""
    return {
        "techniques": [
            {"id": "T1110", "name": "Brute Force", "tactic": "Credential Access", "covered": True},
            {"id": "T1021", "name": "Remote Services", "tactic": "Lateral Movement", "covered": True},
            {"id": "T1048", "name": "Exfiltration Over Alternative Protocol", "tactic": "Exfiltration", "covered": True},
            {"id": "T1071", "name": "Application Layer Protocol", "tactic": "Command and Control", "covered": True},
        ]
    }


# ─── Background detection loop ────────────────────────────────────────────────

async def detection_loop():
    """Runs log generation + detection in the background."""
    loop = asyncio.get_event_loop()
    gen = state.generator

    def run_generator():
        """Runs in a thread — pushes events to async queue."""
        for batch in gen.generate():
            asyncio.run_coroutine_threadsafe(
                process_batch(batch), loop
            )

    # Start generator thread
    t = threading.Thread(target=run_generator, daemon=True)
    t.start()


async def process_batch(batch: list):
    """Process a batch of events through the detection engine."""
    for event in batch:
        state.events_processed += 1
        alert = state.engine.process(event)
        if alert:
            state.alerts_total += 1
            # Generate playbook (use fallback for speed, LLM async)
            from playbook_engine import FALLBACK_PLAYBOOKS
            alert_dict = alert.to_dict()
            alert_dict["playbook"] = FALLBACK_PLAYBOOKS.get(
                alert.threat_type,
                FALLBACK_PLAYBOOKS["data_exfil"]
            )
            state.alerts.append(alert_dict)

            # Broadcast to all WS clients
            await broadcast({
                "type": "alert",
                "alert": alert_dict,
                "stats": state.get_stats()
            })

    # Broadcast stats heartbeat every 50 events
    if state.events_processed % 50 == 0:
        await broadcast({
            "type": "stats",
            "stats": state.get_stats()
        })


# ─── Startup ──────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    asyncio.create_task(detection_loop())
    print("🛡  SentinelAI engine started")


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
