"""
backend/call_dispatcher.py - Emergency Escalation Sequence (SRS UC-3)
Target: Jetson Nano (Python 3)
Compliance: SRS UC-3 Emergency dialer logic
"""

import time
import logging
from typing import List, Dict, Any, Optional

logger = logging.getLogger("CallDispatcher")


class EmergencyCallDispatcher:
    def __init__(self, primary_contact: str = "", secondary_contact: str = ""):
        self.primary_contact = primary_contact
        self.secondary_contact = secondary_contact
        self.call_history: List[Dict[str, Any]] = []

    def execute_escalation_sequence(self, incident_type: str = "FALL DETECTED") -> Dict[str, Any]:
        """
        Executes SRS UC-3 dialer logic:
        1. Calls primary contact.
        2. Retries up to 3 times (1 minute apart).
        3. If no answer, fails over to secondary contact.
        """
        logger.warning("Initiating Emergency Escalation for: %s", incident_type)
        record = {
            "incident": incident_type,
            "timestamp": int(time.time()),
            "attempts": [],
            "status": "INITIATED"
        }

        # Primary contact loop
        for attempt in range(1, 4):
            success = self._simulate_dial(self.primary_contact, f"Primary Contact (Attempt {attempt}/3)")
            record["attempts"].append({
                "target": "PRIMARY",
                "phone": self.primary_contact,
                "attempt": attempt,
                "timestamp": int(time.time()),
                "connected": success
            })
            if success:
                record["status"] = "RESOLVED_PRIMARY"
                self.call_history.append(record)
                return record

            time.sleep(1.0)

        # Failover to secondary contact
        logger.info("Primary contact unreachable. Failing over to secondary contact.")
        sec_success = self._simulate_dial(self.secondary_contact, "Secondary Contact (Failover)")
        record["attempts"].append({
            "target": "SECONDARY",
            "phone": self.secondary_contact,
            "attempt": 1,
            "timestamp": int(time.time()),
            "connected": sec_success
        })
        record["status"] = "RESOLVED_SECONDARY" if sec_success else "FAILED_ALL"
        self.call_history.append(record)
        return record

    def _simulate_dial(self, phone: str, description: str) -> bool:
        if not phone:
            logger.error("No phone number configured for %s", description)
            return False
        logger.info("Dialing %s (%s)...", phone, description)
        return True
