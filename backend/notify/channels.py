"""AETHER notification channels (spec §14).

Channel definitions shared by the server payload builder and the Android
client (which must create these NotificationChannels on first run).
The safety channel is high-priority: sound + vibration + bypass-Do-Not-Disturb
(on Android this still requires the user to grant DND access — see
docs/NOTIFICATIONS.md background-restriction caveats).
"""
from __future__ import annotations

from typing import Any, Dict, List

# importance: high | default | low  (mirrors Android NotificationManager)
CHANNELS: Dict[str, Dict[str, Any]] = {
    "aether_safety": {
        "id": "aether_safety",
        "name": "Safety alerts",
        "description": ("Life-safety warnings: official tsunami warnings, "
                        "PAGER red/orange earthquake alerts."),
        "importance": "high",
        "sound": True,
        "vibration": True,
        "bypass_dnd": True,   # requested; user must still grant DND access on device
        "badge": True,
    },
    "aether_hazard": {
        "id": "aether_hazard",
        "name": "Hazard updates",
        "description": "Advisories, watches and significant detections.",
        "importance": "default",
        "sound": True,
        "vibration": True,
        "bypass_dnd": False,
        "badge": True,
    },
    "aether_info": {
        "id": "aether_info",
        "name": "Information",
        "description": "Daily summary and informational notices.",
        "importance": "low",
        "sound": False,
        "vibration": False,
        "bypass_dnd": False,
        "badge": False,
    },
}


def channel_for(severity: str, kind: str) -> str:
    """Pick a channel from the v2 severity + kind. Official hierarchy always
    outranks AETHER analysis (spec §90): official warnings go to the safety
    channel even at the same severity level."""
    from backend.alerting.severity import to_new_severity
    sev = to_new_severity(severity)
    if sev == "LIFE_SAFETY":
        return "aether_safety"
    if sev == "SEVERE" or (kind == "official" and sev in ("HIGH", "SEVERE")):
        return "aether_safety"
    if sev in ("HIGH", "MODERATE"):
        return "aether_hazard"
    return "aether_info"


def as_list() -> List[Dict[str, Any]]:
    return [dict(CHANNELS[k]) for k in CHANNELS]


def android_channel_payloads() -> List[Dict[str, Any]]:
    """JSON the Android client uses to create NotificationChannels verbatim."""
    return [
        {"channel_id": c["id"], "name": c["name"],
         "description": c["description"], "importance": c["importance"],
         "sound": c["sound"], "vibration": c["vibration"]}
        for c in CHANNELS.values()
    ]
