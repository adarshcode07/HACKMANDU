"""
SentinelAI — Playbook Generator
Uses Claude to generate dynamic, context-aware prevention playbooks
based on the specific incident details.
"""
import os
import json
import httpx
from typing import Optional

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"

PLAYBOOK_SYSTEM = """You are SentinelAI's incident response engine.
Given a threat alert, generate a structured, actionable prevention playbook.

Respond ONLY with valid JSON in this exact structure:
{
  "steps": [
    {
      "phase": "CONTAIN",
      "icon": "🛡",
      "actions": ["action 1", "action 2"]
    },
    {
      "phase": "INVESTIGATE",
      "icon": "🔍",
      "actions": ["action 1", "action 2"]
    },
    {
      "phase": "ERADICATE",
      "icon": "🗑",
      "actions": ["action 1", "action 2"]
    },
    {
      "phase": "PREVENT",
      "icon": "✅",
      "actions": ["action 1", "action 2"]
    }
  ],
  "escalate_to": "SOC Tier 2 / CISO",
  "estimated_ttc_min": 45,
  "tools_needed": ["tool1", "tool2"]
}

Keep actions specific to the actual IPs, processes, and ports from the alert.
Each action should be one concrete sentence. No markdown, no preamble."""


# ─── Fallback playbooks (used when API unavailable) ──────────────────────────

FALLBACK_PLAYBOOKS = {
    "brute_force": {
        "steps": [
            {"phase": "CONTAIN", "icon": "🛡", "actions": [
                "Block all source IPs at the perimeter firewall immediately",
                "Temporarily disable the /api/login endpoint or add CAPTCHA",
                "Force-expire all active sessions for targeted accounts"
            ]},
            {"phase": "INVESTIGATE", "icon": "🔍", "actions": [
                "Pull auth logs for the last 2 hours and identify all attempted usernames",
                "Check if any attempts succeeded — look for 200 OK responses",
                "Determine if an account was compromised using EDR session data"
            ]},
            {"phase": "ERADICATE", "icon": "🗑", "actions": [
                "Reset credentials for any compromised accounts",
                "Revoke and reissue all API tokens for affected users",
                "Remove any unauthorized sessions or OAuth grants"
            ]},
            {"phase": "PREVENT", "icon": "✅", "actions": [
                "Implement account lockout after 10 failed attempts",
                "Deploy geo-blocking for auth endpoint to restrict to known regions",
                "Enable MFA on all accounts that lack it"
            ]}
        ],
        "escalate_to": "SOC Tier 2 / Identity Team",
        "estimated_ttc_min": 30,
        "tools_needed": ["SIEM", "EDR", "WAF", "Identity Provider"]
    },
    "c2_beacon": {
        "steps": [
            {"phase": "CONTAIN", "icon": "🛡", "actions": [
                "Immediately isolate the infected host from all network segments",
                "Block the C2 IP at perimeter firewall and DNS sinkhle the domain",
                "Disable all outbound connections from the host's MAC address"
            ]},
            {"phase": "INVESTIGATE", "icon": "🔍", "actions": [
                "Capture full process tree from endpoint EDR to find the malware parent process",
                "Run memory dump and hash all running processes against VirusTotal",
                "Check DNS query history for the infected host to find additional C2 domains"
            ]},
            {"phase": "ERADICATE", "icon": "🗑", "actions": [
                "Kill the malicious process and delete associated binary and scheduled tasks",
                "Remove any registry run keys added for persistence",
                "Reimage the host if malware cannot be fully removed"
            ]},
            {"phase": "PREVENT", "icon": "✅", "actions": [
                "Add C2 IP/domain block list to NGFW and DNS resolver",
                "Deploy EDR behavioral rules to detect beacon-like traffic patterns",
                "Segment high-value hosts to prevent lateral movement if re-infected"
            ]}
        ],
        "escalate_to": "SOC Tier 2 / Incident Response Team",
        "estimated_ttc_min": 60,
        "tools_needed": ["EDR", "Firewall", "DNS Sinkhole", "Memory Forensics"]
    },
    "lateral_movement": {
        "steps": [
            {"phase": "CONTAIN", "icon": "🛡", "actions": [
                "Isolate the pivot host from the internal network immediately",
                "Block SMB (445), RPC (135), and RDP (3389) between workstation VLANs",
                "Disable the SYSTEM account's network access on the affected host"
            ]},
            {"phase": "INVESTIGATE", "icon": "🔍", "actions": [
                "Map all hosts the pivot connected to — check if any showed successful auth",
                "Review endpoint logs on all scanned hosts for matching process activity",
                "Determine initial compromise vector: phishing, exploit, or credential theft"
            ]},
            {"phase": "ERADICATE", "icon": "🗑", "actions": [
                "Remove all tools dropped by attacker (psexec, nc, mimikatz)",
                "Revoke Kerberos tickets (krbtgt) if credential harvesting is suspected",
                "Restore registry keys modified during the attack"
            ]},
            {"phase": "PREVENT", "icon": "✅", "actions": [
                "Implement network micro-segmentation to restrict workstation-to-workstation traffic",
                "Enable Windows Credential Guard to prevent credential harvesting",
                "Deploy deception technology (honeypots) to detect future scanning"
            ]}
        ],
        "escalate_to": "SOC Tier 2 / Active Directory Team",
        "estimated_ttc_min": 90,
        "tools_needed": ["EDR", "SIEM", "Network Firewall", "AD Tools"]
    },
    "data_exfil": {
        "steps": [
            {"phase": "CONTAIN", "icon": "🛡", "actions": [
                "Isolate endpoint 10.0.1.47 from all network access immediately",
                "Block outbound traffic to 185.220.x.x/24 at perimeter firewall",
                "Enable DLP blocking on all endpoints for transfers >100MB to unknown IPs"
            ]},
            {"phase": "INVESTIGATE", "icon": "🔍", "actions": [
                "Pull full process tree for the svchost.exe process and identify its origin",
                "Determine what data was accessed and transferred — check file audit logs",
                "Assess volume of data exfiltrated and classify sensitivity"
            ]},
            {"phase": "ERADICATE", "icon": "🗑", "actions": [
                "Kill malicious process and quarantine the binary for forensic analysis",
                "Remove persistence mechanisms (registry run keys, scheduled tasks)",
                "Rotate any credentials that may have been accessed by the malware"
            ]},
            {"phase": "PREVENT", "icon": "✅", "actions": [
                "Add TOR exit node block list (from Abuse.ch) to NGFW policy",
                "Implement DLP rule: alert + block if >500MB outbound to unknown external IPs in 10 min",
                "Enable file access auditing on all shares containing sensitive data"
            ]}
        ],
        "escalate_to": "CISO / Legal / SOC Tier 2",
        "estimated_ttc_min": 45,
        "tools_needed": ["EDR", "DLP", "SIEM", "NGFW", "File Audit"]
    }
}


async def generate_playbook(alert_dict: dict, use_llm: bool = True) -> dict:
    """
    Generate a context-aware playbook for the given alert.
    Falls back to template if LLM unavailable.
    """
    threat_type = alert_dict.get("threat_type", "data_exfil")

    if not use_llm:
        return FALLBACK_PLAYBOOKS.get(threat_type, FALLBACK_PLAYBOOKS["data_exfil"])

    prompt = f"""Generate a playbook for this security alert:

Alert ID: {alert_dict.get('alert_id')}
Threat Type: {alert_dict.get('threat_type')}
Severity: {alert_dict.get('severity')}
Confidence: {alert_dict.get('confidence', 0)*100:.0f}%
Source IP: {alert_dict.get('src_ip')}
Destination IP: {alert_dict.get('dst_ip')}
Description: {alert_dict.get('description')}
Why Flagged: {alert_dict.get('why_flagged')}
MITRE: {alert_dict.get('mitre_id')} — {alert_dict.get('mitre_name')}
False Positive Score: {alert_dict.get('false_positive_score', 0)*100:.0f}%
Top Features: {json.dumps([f["feature"] for f in alert_dict.get("shap_features", [])[:3]])}

Generate a specific, actionable 4-phase playbook for this exact incident."""

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                ANTHROPIC_API_URL,
                headers={"Content-Type": "application/json"},
                json={
                    "model": "claude-sonnet-4-20250514",
                    "max_tokens": 1000,
                    "system": PLAYBOOK_SYSTEM,
                    "messages": [{"role": "user", "content": prompt}]
                }
            )
            if resp.status_code == 200:
                data = resp.json()
                text = data["content"][0]["text"]
                # Strip markdown code fences if present
                text = text.strip()
                if text.startswith("```"):
                    text = text.split("```")[1]
                    if text.startswith("json"):
                        text = text[4:]
                return json.loads(text.strip())
    except Exception as e:
        print(f"[Playbook LLM] Falling back to template: {e}")

    return FALLBACK_PLAYBOOKS.get(threat_type, FALLBACK_PLAYBOOKS["data_exfil"])
