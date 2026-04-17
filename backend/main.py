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
from prevention_engine import PreventionEngine

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
        self.prevention_engine = PreventionEngine()
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

# ─── Prevention API endpoints ─────────────────────────────────────────────────

@app.get("/api/prevention/overview")
def get_prevention_overview():
    """Get data breach prevention overview."""
    return state.prevention_engine.get_protection_overview()

@app.get("/api/prevention/strategies")
def get_protection_strategies():
    """Get all protection strategies."""
    return {
        "strategies": state.prevention_engine.strategies,
        "total": len(state.prevention_engine.strategies),
        "active": sum(1 for s in state.prevention_engine.strategies if s["status"] == "active")
    }

@app.get("/api/prevention/strategies/{strategy_id}")
def get_strategy(strategy_id: int):
    """Get details of a specific strategy."""
    strategy = next((s for s in state.prevention_engine.strategies if s["id"] == strategy_id), None)
    if not strategy:
        raise HTTPException(status_code=404, detail="Strategy not found")
    return strategy

@app.get("/api/prevention/vulnerabilities")
def get_vulnerabilities():
    """Get current vulnerabilities."""
    if not state.prevention_engine.vulnerabilities:
        state.prevention_engine.scan_vulnerabilities()
    return {
        "vulnerabilities": state.prevention_engine.vulnerabilities,
        "total": len(state.prevention_engine.vulnerabilities),
        "critical": sum(1 for v in state.prevention_engine.vulnerabilities if v["severity"] == "critical"),
        "last_scan": state.prevention_engine.last_scan
    }

@app.post("/api/prevention/scan")
def trigger_vulnerability_scan():
    """Trigger a vulnerability scan."""
    vulnerabilities = state.prevention_engine.scan_vulnerabilities()
    return {
        "scan_status": "completed",
        "vulnerabilities_found": len(vulnerabilities),
        "scan_time": state.prevention_engine.last_scan
    }

@app.get("/api/prevention/dlp")
def get_dlp_status():
    """Get Data Loss Prevention status."""
    return state.prevention_engine.get_dlp_status()

@app.post("/api/prevention/check-data")
def check_data_exposure(data: dict):
    """Check if data contains sensitive information."""
    content = data.get("content", "")
    result = state.prevention_engine.check_data_exposure(content)
    return result

@app.get("/api/prevention/encryption")
def get_encryption_status():
    """Get encryption status and algorithms."""
    return state.prevention_engine.encryption_status

@app.post("/api/prevention/validate-encryption")
def validate_encryption(config: dict):
    """Validate encryption algorithm and key size."""
    algorithm = config.get("algorithm")
    key_size = config.get("key_size")
    
    if not algorithm or not key_size:
        raise HTTPException(status_code=400, detail="algorithm and key_size required")
    
    is_valid = state.prevention_engine.validate_encryption(algorithm, key_size)
    return {
        "algorithm": algorithm,
        "key_size": key_size,
        "valid": is_valid,
        "recommendation": f"{algorithm}-{key_size} is {'secure' if is_valid else 'not recommended'}"
    }

@app.get("/api/prevention/compliance")
def get_compliance_status():
    """Get compliance status (GDPR, HIPAA, PCI-DSS, SOC2)."""
    return state.prevention_engine.get_compliance_status()

@app.get("/api/prevention/audit-logs")
def get_audit_logs(limit: int = 50):
    """Get audit logs."""
    logs = state.prevention_engine.get_audit_logs(limit)
    return {"logs": logs, "total": len(logs)}

@app.post("/api/prevention/remediate/{vuln_id}")
async def remediate_vulnerability(vuln_id: int, action: dict):
    """Apply remediation action to a vulnerability."""
    action_type = action.get("action")
    if not action_type:
        raise HTTPException(status_code=400, detail="action parameter required")
    
    result = state.prevention_engine.remediate_vulnerability(vuln_id, action_type)
    if result["success"]:
        await broadcast({
            "type": "remediation_applied",
            "vulnerability_id": vuln_id,
            "action": action_type
        })
    return result

@app.get("/api/prevention/report")
def export_prevention_report(report_type: str = "full"):
    """Export prevention report."""
    return state.prevention_engine.export_report(report_type)

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
