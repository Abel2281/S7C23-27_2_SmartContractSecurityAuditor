"""
Pydantic schemas for the courtroom pipeline (Prosecutor -> Defender -> Judge).
Single-pass design (Option A): each agent runs exactly once per finding.
"""

from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field


class Severity(str, Enum):
    HIGH = "High"
    MEDIUM = "Medium"
    LOW = "Low"


class Verdict(str, Enum):
    CONFIRMED = "CONFIRMED"
    FALSE_POSITIVE = "FALSE_POSITIVE"
    INCONCLUSIVE = "INCONCLUSIVE"


# ---------- Prosecutor ----------

class ProsecutorCharge(BaseModel):
    """Output of the Prosecutor agent for a single Slither finding."""
    finding_id: str
    charge_summary: str = Field(..., description="One-line statement of the alleged vulnerability")
    reasoning: str = Field(..., description="Why this is exploitable, referencing the sliced code")
    severity_assessment: Severity
    attack_scenario: Optional[str] = Field(
        None, description="Concrete step-by-step exploit path, if applicable"
    )


# ---------- Defender ----------

class DefenderResponse(BaseModel):
    """Output of the Defender agent, responding to a ProsecutorCharge."""
    finding_id: str
    rebuttal_summary: str = Field(..., description="One-line counter-argument")
    reasoning: str = Field(..., description="Why the charge is/isn't valid, citing guards/modifiers/state")
    concedes: bool = Field(..., description="True if Defender agrees the vulnerability is real")
    mitigating_factors: Optional[str] = Field(
        None, description="Existing guard conditions, modifiers, or invariants that reduce risk"
    )


# ---------- Judge ----------

class JudgeVerdict(BaseModel):
    """Final output of the Judge agent. This is what gets cached and reported."""
    finding_id: str
    verdict: Verdict
    final_severity: Optional[Severity] = Field(
        None, description="Judge's own severity call; None if INCONCLUSIVE"
    )
    reasoning: str = Field(..., description="Synthesis of Prosecutor charge vs Defender rebuttal")
    patch_recommendation: Optional[str] = Field(
        None, description="Suggested fix, if verdict is CONFIRMED"
    )


# ---------- Pipeline record ----------

class DebateRecord(BaseModel):
    """Full record of one finding's trip through the courtroom. Written to cache + report."""
    finding_id: str
    contract_hash: str
    prosecutor: ProsecutorCharge
    defender: DefenderResponse
    judge: JudgeVerdict
    degraded_mode: bool = Field(
        default=False, description="True if any agent call fell back to a lower provider tier"
    )