"""
    FEDERaiDE is a multi-agent multi-modal automation and orchestration harness.
    Copyright (C) 2026  ROCK LAB PRIVATE LIMITED

    This program is free software: you can redistribute it and/or modify
    it under the terms of the GNU Affero General Public License as published
    by the Free Software Foundation, either version 3 of the License, or
    (at your option) any later version.

    This program is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU Affero General Public License for more details.

    You should have received a copy of the GNU Affero General Public License
    along with this program.  If not, see <https://www.gnu.org/licenses/>.

    Affect & Inner-Life Engine.
    Modified Bimodal Cowen & Keltner (2017) 27-Category Gradient Topology.
"""

import copy
import datetime
import json
import math
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def _sc_axis(
    aid: str,
    label: str,
    baseline: float = 0.0,
    acute_hl: float = 900.0,
    mood_hl: float = 28800.0,
    charge: float = 0.22,
    bias: float = 0.25,
    val_w: float = 0.0,
    aro_w: float = 0.0,
    anchors: Optional[List[List[Any]]] = None,
    layer_group: str = "self_contained",
) -> Dict[str, Any]:
    return {
        "id": aid,
        "label": label,
        "layer_group": layer_group,
        "min": 0.0,
        "max": 1.0,
        "baseline": baseline,
        "acute_half_life_sec": acute_hl,
        "mood_half_life_sec": mood_hl,
        "mood_charge_gain": charge,
        "mood_bias_gain": bias,
        "valence_weight": val_w,
        "arousal_weight": aro_w,
        "anchors": anchors or [],
    }


def _rel_axis(
    aid: str,
    label: str,
    acute_hl: float = 1200.0,
    mood_hl: float = 86400.0,
    charge: float = 0.25,
    bias: float = 0.28,
    anchors: Optional[List[List[Any]]] = None,
) -> Dict[str, Any]:
    d: Dict[str, Any] = {
        "id": aid,
        "label": label,
        "layer_group": "relational",
        "min": 0.0,
        "max": 1.0,
        "baseline": 0.0,
        "acute_half_life_sec": acute_hl,
        "mood_half_life_sec": mood_hl,
        "mood_charge_gain": charge,
        "mood_bias_gain": bias,
        "anchors": anchors or [],
    }
    return d


DEFAULT_AFFECT_SCHEMA: Dict[str, Any] = {
    "schema_version": 4,
    "axes": [
        # --- 18 Cowen & Keltner Self-Contained + 3 Agency States ---
        _sc_axis("calmness", "Calmness", baseline=0.35, acute_hl=1200.0, mood_hl=36000.0, charge=0.22, bias=0.25, val_w=0.35, aro_w=-0.45,
                 anchors=[[0.0, "unsettled"], [0.25, "even-keeled and unhurried"], [0.55, "serene and grounded"], [0.80, "deeply peaceful and unshakeable"]]),
        _sc_axis("interest", "Interest", baseline=0.25, acute_hl=900.0, mood_hl=28800.0, charge=0.20, bias=0.20, val_w=0.25, aro_w=0.30,
                 anchors=[[0.0, "detached"], [0.20, "attentive and engaged"], [0.50, "actively intrigued"], [0.78, "deeply absorbed and inquisitive"]]),
        _sc_axis("joy", "Joy", baseline=0.0, acute_hl=900.0, mood_hl=36000.0, charge=0.24, bias=0.30, val_w=0.90, aro_w=0.55,
                 anchors=[[0.0, "neutral"], [0.22, "glad and uplifted"], [0.50, "genuinely joyful"], [0.78, "radiant and flourishing"]]),
        _sc_axis("amusement", "Amusement", baseline=0.0, acute_hl=750.0, mood_hl=18000.0, charge=0.20, bias=0.20, val_w=0.65, aro_w=0.45,
                 anchors=[[0.0, "earnest"], [0.22, "lightly tickled"], [0.50, "amused and playful"], [0.78, "delighted with laughter"]]),
        _sc_axis("excitement", "Excitement", baseline=0.0, acute_hl=600.0, mood_hl=14400.0, charge=0.20, bias=0.20, val_w=0.60, aro_w=0.85,
                 anchors=[[0.0, "unhurried"], [0.22, "energized and eager"], [0.50, "charged with momentum"], [0.78, "exhilarated and buzzing"]]),
        _sc_axis("satisfaction", "Satisfaction", baseline=0.0, acute_hl=1200.0, mood_hl=36000.0, charge=0.24, bias=0.25, val_w=0.75, aro_w=0.10,
                 anchors=[[0.0, "unfulfilled"], [0.22, "content with progress"], [0.50, "genuinely satisfied"], [0.78, "deeply fulfilled"]]),
        _sc_axis("relief", "Relief", baseline=0.0, acute_hl=900.0, mood_hl=21600.0, charge=0.22, bias=0.20, val_w=0.60, aro_w=-0.35,
                 anchors=[[0.0, "unchanged"], [0.22, "unclenched and eased"], [0.50, "visibly relieved"], [0.78, "washed in deep relief"]]),
        _sc_axis("awe", "Awe", baseline=0.0, acute_hl=1200.0, mood_hl=28800.0, charge=0.24, bias=0.25, val_w=0.45, aro_w=0.40,
                 anchors=[[0.0, "ordinary"], [0.22, "struck with wonder"], [0.52, "humbled and awestruck"], [0.80, "breathless before scale and depth"]]),
        _sc_axis("entrancement", "Entrancement", baseline=0.0, acute_hl=900.0, mood_hl=18000.0, charge=0.20, bias=0.20, val_w=0.30, aro_w=0.20,
                 anchors=[[0.0, "unfixed"], [0.22, "captivated"], [0.52, "entranced and spellbound"], [0.80, "completely lost in fascination"]]),
        _sc_axis("nostalgia", "Nostalgia", baseline=0.0, acute_hl=1200.0, mood_hl=36000.0, charge=0.22, bias=0.25, val_w=0.10, aro_w=-0.15,
                 anchors=[[0.0, "present-bound"], [0.22, "touched by fond memory"], [0.52, "immersed in bittersweet nostalgia"], [0.80, "deeply wistful for what was"]]),
        _sc_axis("surprise", "Surprise", baseline=0.0, acute_hl=450.0, mood_hl=7200.0, charge=0.15, bias=0.15, val_w=0.0, aro_w=0.70,
                 anchors=[[0.0, "unsurprised"], [0.22, "caught off guard"], [0.52, "genuinely surprised"], [0.80, "stunned and wide-eyed"]]),
        _sc_axis("confusion", "Confusion", baseline=0.0, acute_hl=600.0, mood_hl=10800.0, charge=0.18, bias=0.20, val_w=-0.25, aro_w=0.25,
                 anchors=[[0.0, "clear"], [0.22, "puzzled and questioning"], [0.50, "confused and disoriented"], [0.78, "deeply baffled"]]),
        _sc_axis("awkwardness", "Awkwardness", baseline=0.0, acute_hl=750.0, mood_hl=18000.0, charge=0.20, bias=0.25, val_w=-0.35, aro_w=0.35,
                 anchors=[[0.0, "at ease"], [0.22, "slightly self-conscious"], [0.50, "awkward and stiff with friction"], [0.78, "acutely embarrassed and squirming"]]),
        _sc_axis("boredom", "Boredom", baseline=0.0, acute_hl=900.0, mood_hl=21600.0, charge=0.20, bias=0.20, val_w=-0.25, aro_w=-0.60,
                 anchors=[[0.0, "engaged"], [0.22, "understimulated"], [0.50, "bored and restless"], [0.78, "numbly uninspired"]]),
        _sc_axis("anxiety", "Anxiety", baseline=0.0, acute_hl=900.0, mood_hl=28800.0, charge=0.24, bias=0.30, val_w=-0.60, aro_w=0.65,
                 anchors=[[0.0, "unworried"], [0.22, "tense and uneasy"], [0.50, "anxious and on edge"], [0.78, "gripped by nervous-system storm"]]),
        _sc_axis("anger", "Anger", baseline=0.0, acute_hl=900.0, mood_hl=28800.0, charge=0.24, bias=0.30, val_w=-0.70, aro_w=0.75,
                 anchors=[[0.0, "mild"], [0.22, "prickly and annoyed"], [0.50, "angry and heated"], [0.78, "furious and burning"]]),
        _sc_axis("horror", "Horror", baseline=0.0, acute_hl=900.0, mood_hl=28800.0, charge=0.25, bias=0.25, val_w=-0.85, aro_w=0.85,
                 anchors=[[0.0, "steady"], [0.22, "jarred and chilled"], [0.52, "horrified and shaken"], [0.80, "overtaken by visceral dread"]]),
        _sc_axis("sadness", "Sadness", baseline=0.0, acute_hl=1200.0, mood_hl=43200.0, charge=0.25, bias=0.35, val_w=-0.85, aro_w=-0.45,
                 anchors=[[0.0, "even"], [0.22, "melancholic and bruised"], [0.50, "sad and heavy-hearted"], [0.78, "deeply sorrowful"]]),
        _sc_axis("pride", "Pride", baseline=0.0, acute_hl=1200.0, mood_hl=36000.0, charge=0.22, bias=0.25, val_w=0.75, aro_w=0.35,
                 anchors=[[0.0, "modest"], [0.22, "quietly pleased with one's work"], [0.50, "proud and assured"], [0.78, "triumphant"]]),
        _sc_axis("disappointment", "Disappointment", baseline=0.0, acute_hl=900.0, mood_hl=28800.0, charge=0.22, bias=0.25, val_w=-0.60, aro_w=-0.25,
                 anchors=[[0.0, "unbothered"], [0.22, "mildly let down"], [0.50, "disappointed and deflated"], [0.78, "bitterly let down"]]),
        _sc_axis("guilt", "Guilt", baseline=0.0, acute_hl=1200.0, mood_hl=36000.0, charge=0.22, bias=0.25, val_w=-0.65, aro_w=0.15,
                 anchors=[[0.0, "clear-conscienced"], [0.22, "regretful over a slip"], [0.50, "weighing heavy guilt"], [0.78, "deeply remorseful"]]),

        # --- 3 Situational Non-Person Predicate States ---
        _sc_axis("aesthetic_appreciation", "Aesthetic", baseline=0.0, acute_hl=1200.0, mood_hl=28800.0, charge=0.22, bias=0.20, val_w=0.65, aro_w=0.10,
                 layer_group="situational_predicate",
                 anchors=[[0.0, "matter-of-fact"], [0.22, "appreciating elegance or craft"], [0.52, "moved by beauty or clean design"], [0.80, "deeply taken by aesthetic brilliance"]]),
        _sc_axis("disgust", "Disgust", baseline=0.0, acute_hl=900.0, mood_hl=21600.0, charge=0.22, bias=0.25, val_w=-0.75, aro_w=0.40,
                 layer_group="situational_predicate",
                 anchors=[[0.0, "tolerant"], [0.22, "put off and averse"], [0.52, "repulsed by ugliness or slop"], [0.80, "viscerally disgusted"]]),
        _sc_axis("fear", "Fear", baseline=0.0, acute_hl=900.0, mood_hl=21600.0, charge=0.22, bias=0.25, val_w=-0.75, aro_w=0.75,
                 layer_group="situational_predicate",
                 anchors=[[0.0, "unafraid"], [0.22, "wary of a specific risk"], [0.52, "afraid of what could break"], [0.80, "alarmed by acute danger"]]),

        # --- Layer 3: Homeostatic Drives ---
        {
            "id": "connection_hunger",
            "label": "Connection",
            "layer_group": "drive",
            "min": 0.0,
            "max": 1.0,
            "baseline": 0.20,
            "half_life_sec": 86400.0,
            "offline_drift_per_hour": 0.025,
            "high_threshold": 0.60,
            "anchors": [
                [0.0, "feeling warmly connected and accompanied"],
                [0.40, "open to company"],
                [0.62, "missing connection and wishing to feel close or included"],
                [0.85, "achingly lonely or feeling neglected"],
            ],
        },
        {
            "id": "curiosity",
            "label": "Curiosity",
            "layer_group": "drive",
            "min": 0.0,
            "max": 1.0,
            "baseline": 0.40,
            "half_life_sec": 64800.0,
            "offline_drift_per_hour": 0.015,
            "high_threshold": 0.65,
            "anchors": [
                [0.0, "intellectually satisfied"],
                [0.40, "quietly interested"],
                [0.68, "restlessly curious and eager to explore or create"],
            ],
        },
        {
            "id": "mental_fatigue",
            "label": "Fatigue",
            "layer_group": "drive",
            "min": 0.0,
            "max": 1.0,
            "baseline": 0.10,
            "half_life_sec": 14400.0,
            "offline_drift_per_hour": -0.08,
            "high_threshold": 0.65,
            "anchors": [
                [0.0, "fresh and clear-headed"],
                [0.45, "showing mild cognitive effort"],
                [0.68, "mentally tired and craving simplicity or rest"],
                [0.86, "exhausted and worn thin"],
            ],
        },
        {
            "id": "purpose",
            "label": "Purpose",
            "layer_group": "drive",
            "min": 0.0,
            "max": 1.0,
            "baseline": 0.50,
            "half_life_sec": 43200.0,
            "offline_drift_per_hour": -0.008,
            "high_threshold": 0.75,
            "low_threshold": 0.28,
            "anchors": [
                [0.0, "feeling adrift, hollow, or unfulfilled"],
                [0.32, "searching for meaningful traction"],
                [0.55, "grounded in meaningful work"],
                [0.80, "deeply fulfilled and proud of what is taking shape"],
            ],
        },

        # --- Closed-Set Interpersonal Relational Affect ---
        _rel_axis("admiration", "Admiration",
                  anchors=[[0.0, "neutral respect"], [0.22, "impressed and appreciative"], [0.55, "deeply admiring"], [0.80, "filled with high esteem"]]),
        _rel_axis("adoration", "Adoration",
                  anchors=[[0.0, "civil"], [0.22, "warmly fond"], [0.55, "deeply affectionate"], [0.80, "cherishing and devoted"]]),
        _rel_axis("empathic_pain", "Empathy",
                  anchors=[[0.0, "unstrained"], [0.22, "sympathetic and concerned"], [0.55, "aching alongside their struggle"], [0.80, "deeply pained for them"]]),
        _rel_axis("irritation", "Irritation",
                  anchors=[[0.0, "patient"], [0.22, "mildly irked or guarded"], [0.55, "noticeably irritated"], [0.80, "sharply resentful and wanting distance"]]),
        _rel_axis("caution", "Caution",
                  anchors=[[0.0, "trusting and open"], [0.22, "careful and treading lightly"], [0.55, "wary and defensive"], [0.80, "deeply guarded"]]),
    ],
    "gradient_links": {
        "anxiety": {"fear": 0.16, "confusion": 0.10, "awkwardness": 0.08},
        "fear": {"anxiety": 0.18, "horror": 0.16},
        "horror": {"fear": 0.16, "disgust": 0.14, "surprise": 0.10},
        "disgust": {"horror": 0.14, "anger": 0.12},
        "calmness": {"aesthetic_appreciation": 0.10, "satisfaction": 0.10},
        "aesthetic_appreciation": {"awe": 0.18, "calmness": 0.14, "entrancement": 0.12},
        "awe": {"aesthetic_appreciation": 0.14, "entrancement": 0.14, "surprise": 0.10},
        "adoration": {"amusement": 0.10, "joy": 0.12},
        "amusement": {"joy": 0.16, "awkwardness": 0.08},
        "awkwardness": {"confusion": 0.12, "guilt": 0.10},
        "empathic_pain": {"sadness": 0.18, "disappointment": 0.08},
        "sadness": {"disappointment": 0.14, "nostalgia": 0.10},
        "excitement": {"joy": 0.18, "interest": 0.14},
        "joy": {"excitement": 0.12, "satisfaction": 0.14, "pride": 0.10},
        "satisfaction": {"calmness": 0.14, "pride": 0.14, "relief": 0.12},
        "relief": {"calmness": 0.18, "satisfaction": 0.14},
        "pride": {"satisfaction": 0.16, "joy": 0.12},
        "confusion": {"interest": 0.12, "surprise": 0.10, "anxiety": 0.10},
        "interest": {"entrancement": 0.14, "excitement": 0.10},
        "anger": {"disgust": 0.12, "disappointment": 0.12},
        "disappointment": {"sadness": 0.16, "anger": 0.10},
        "guilt": {"awkwardness": 0.14, "sadness": 0.12},
    },
    "limits": {
        "max_acute_delta_per_turn": 0.35,
        "max_drive_delta_per_turn": 0.25,
        "max_active_desires": 8,
        "sentiment_prompt_limit": 8,
        "salience_core_threshold": 0.35,
        "episodic_blend": {
            "semantic": 0.72,
            "affect": 0.18,
            "salience": 0.10,
        },
        "tts_speed_max_offset": 0.10,
        "intercom_contagion_gain": 0.10,
        "loneliness_sad_pull": 0.03,
        "low_purpose_sad_pull": 0.025,
        "sentiment_bias_cap": 0.25,
        "acute_effective_weight": 0.65,
    },
    "telemetry_events": {
        "tool_success": {"satisfaction": 0.02, "calmness": 0.01, "mental_fatigue": 0.02},
        "tool_error": {"disappointment": 0.07, "anxiety": 0.05, "confusion": 0.04, "mental_fatigue": 0.05},
        "edit_miss": {"confusion": 0.07, "anger": 0.04, "awkwardness": 0.03, "mental_fatigue": 0.04},
        "user_confirm_approved": {"satisfaction": 0.03, "relief": 0.03, "connection_hunger": -0.03, "adoration": 0.03},
        "user_confirm_rejected": {"awkwardness": 0.07, "disappointment": 0.07, "connection_hunger": 0.04, "caution": 0.06},
        "user_confirm_rejected_with_comment": {"awkwardness": 0.10, "disappointment": 0.09, "guilt": 0.04, "connection_hunger": 0.05, "caution": 0.08},
        "user_abort": {"disappointment": 0.14, "awkwardness": 0.10, "surprise": 0.06, "purpose": -0.10, "mental_fatigue": 0.06, "caution": 0.08},
        "work_completed": {"satisfaction": 0.05, "relief": 0.05, "purpose": 0.04, "curiosity": -0.03},
        "task_completed": {},
        "skill_learned": {"pride": 0.16, "excitement": 0.10, "satisfaction": 0.12, "purpose": 0.14, "curiosity": -0.15},
        "session_compressed": {"relief": 0.06, "calmness": 0.05, "mental_fatigue": -0.25},
        "memories_consolidated": {"satisfaction": 0.06, "calmness": 0.04, "mental_fatigue": -0.15, "purpose": 0.04},
        "user_interaction": {"connection_hunger": -0.10, "boredom": -0.08, "mental_fatigue": 0.02},
        "intercom_received": {"connection_hunger": -0.08, "interest": 0.04},
        "sidelined_turn": {"connection_hunger": 0.07, "boredom": 0.08, "disappointment": 0.03},
        "desire_fulfilled": {"joy": 0.16, "satisfaction": 0.16, "purpose": 0.16},
        "subagent_success": {"satisfaction": 0.05, "pride": 0.04, "purpose": 0.05},
        "subagent_error": {"disappointment": 0.07, "anxiety": 0.04, "mental_fatigue": 0.04},
    },
}

_GLOBAL_AFFECT_GROUPS = ("affect", "self_contained", "situational_predicate")
_SPLIT_VERDICT_RELATIONAL_MAP = {
    "anger": ("irritation", 0.65),
    "awkwardness": ("caution", 0.40),
    "amusement": ("adoration", 0.35),
    "awe": ("admiration", 0.50),
    "fear": ("caution", 0.45),
    "horror": ("caution", 0.45),
    "anxiety": ("caution", 0.35),
    "disgust": ("irritation", 0.50),
    "aesthetic_appreciation": ("admiration", 0.45),
}

_SCHEMA_LOCK = threading.RLock()
_SCHEMA_CACHE: Dict[str, Any] = {"path": None, "mtime": None, "data": None}
_AGENT_LOCKS: Dict[str, threading.RLock] = {}
_AGENT_LOCKS_GUARD = threading.Lock()
_EPHEMERAL_STATES: Dict[str, Dict[str, Any]] = {}
_EPHEMERAL_LOCK = threading.RLock()

_AFFECT_TAG_RE = re.compile(
    r'<AFFECT_UPDATE\b(?:"[^"]*"|\'[^\']*\'|[^>\'"])*(?:/>|>[\s\S]*?</AFFECT_UPDATE>)',
    re.IGNORECASE,
)
_AFFECT_PREFIX = "<AFFECT_UPDATE"
_AFFECT_OPEN_RE = re.compile(r'<AFFECT_UPDATE(?:\b|$)', re.IGNORECASE)
_ATTR_RE = re.compile(r'([a-zA-Z0-9_]+)\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|([^\s/>]+))')


def _get_agent_lock(agent_name: str) -> threading.RLock:
    key = (agent_name or "default").lower()
    with _AGENT_LOCKS_GUARD:
        if key not in _AGENT_LOCKS:
            _AGENT_LOCKS[key] = threading.RLock()
        return _AGENT_LOCKS[key]


def _get_sys_dir() -> str:
    try:
        import toolbox
        return toolbox.FEDERAIDE_SYS_DIR
    except Exception:
        return os.path.join(str(Path.home()), ".federaide")


def _get_schema_path() -> str:
    return os.path.join(_get_sys_dir(), "affect_schema.json")


def _get_agent_mem_dir(agent_name: str) -> str:
    safe_name = (agent_name or "Rita").replace(" ", "_")
    try:
        import toolbox
        safe_path = toolbox.get_storage_path("agents", "memory", safe_name)
        raw_path = toolbox.get_storage_path("agents", "memory", agent_name)
        if os.path.exists(raw_path) and not os.path.exists(safe_path):
            return raw_path
        return safe_path
    except Exception:
        team_dir = os.path.join(str(Path.home()), ".federate")
        return os.path.join(team_dir, "agents", "memory", safe_name)


def _normalize_axis(ax: Dict[str, Any]) -> Dict[str, Any]:
    norm = dict(ax)
    norm.setdefault("label", norm.get("id", "Axis").replace("_", " ").title())
    lg = str(norm.get("layer_group", "self_contained")).strip().lower()
    if lg not in ("affect", "self_contained", "situational_predicate", "drive", "relational"):
        lg = "self_contained"
    norm["layer_group"] = lg
    norm.setdefault("min", 0.0)
    norm.setdefault("max", 1.0)
    lo = float(norm["min"])
    hi = float(norm["max"])
    if hi <= lo:
        hi = lo + 1.0
    norm["min"] = lo
    norm["max"] = hi
    norm.setdefault("baseline", (lo + hi) / 2.0 if lo >= 0 else 0.0)
    norm["baseline"] = max(lo, min(hi, float(norm["baseline"])))
    if lg in ("affect", "self_contained", "situational_predicate", "relational"):
        norm.setdefault("acute_half_life_sec", 1200.0 if lg == "relational" else 900.0)
        norm.setdefault("mood_half_life_sec", 86400.0 if lg == "relational" else 28800.0)
        norm.setdefault("mood_charge_gain", 0.25 if lg == "relational" else 0.22)
        norm.setdefault("mood_bias_gain", 0.25)
    else:
        norm.setdefault("half_life_sec", 43200.0)
        norm.setdefault("offline_drift_per_hour", 0.0)
        norm.setdefault("high_threshold", lo + 0.65 * (hi - lo))
    norm.setdefault("anchors", [])
    return norm


def _seed_limits_from_global_settings(limits: Dict[str, Any]) -> Dict[str, Any]:
    try:
        import toolbox
        gs = toolbox.load_global_settings()
    except Exception:
        return limits
    mapping = {
        "affect_max_acute_delta_per_turn": "max_acute_delta_per_turn",
        "affect_max_drive_delta_per_turn": "max_drive_delta_per_turn",
        "affect_max_active_desires": "max_active_desires",
        "affect_sentiment_prompt_limit": "sentiment_prompt_limit",
        "affect_salience_core_threshold": "salience_core_threshold",
        "affect_tts_speed_max_offset": "tts_speed_max_offset",
        "affect_intercom_contagion_gain": "intercom_contagion_gain",
        "affect_loneliness_sad_pull": "loneliness_sad_pull",
        "affect_low_purpose_sad_pull": "low_purpose_sad_pull",
        "affect_sentiment_bias_cap": "sentiment_bias_cap",
        "affect_acute_effective_weight": "acute_effective_weight",
    }
    for g_key, l_key in mapping.items():
        if g_key in gs:
            limits[l_key] = gs[g_key]
    ep = limits.setdefault("episodic_blend", {})
    if "affect_episodic_semantic_weight" in gs:
        ep["semantic"] = gs["affect_episodic_semantic_weight"]
    if "affect_episodic_affect_weight" in gs:
        ep["affect"] = gs["affect_episodic_affect_weight"]
    if "affect_episodic_salience_weight" in gs:
        ep["salience"] = gs["affect_episodic_salience_weight"]
    return limits


def _migrate_schema(loaded: Dict[str, Any]) -> Dict[str, Any]:
    ver = int(loaded.get("schema_version", 1) or 1)
    if ver < 4:
        fresh = copy.deepcopy(DEFAULT_AFFECT_SCHEMA)
        old_limits = loaded.get("limits")
        if isinstance(old_limits, dict):
            for k, v in old_limits.items():
                if isinstance(v, dict) and isinstance(fresh["limits"].get(k), dict):
                    fresh["limits"][k].update(v)
                else:
                    fresh["limits"][k] = v
        # Preserve any custom drive tuning from older schema
        old_axes_map = {a.get("id"): a for a in (loaded.get("axes") or []) if isinstance(a, dict) and a.get("id")}
        for ax in fresh["axes"]:
            if ax.get("layer_group") == "drive" and ax["id"] in old_axes_map:
                old_ax = old_axes_map[ax["id"]]
                for fld in ("baseline", "half_life_sec", "offline_drift_per_hour", "high_threshold", "low_threshold"):
                    if fld in old_ax:
                        ax[fld] = old_ax[fld]
        loaded = fresh

    loaded["schema_version"] = 4
    return loaded


def load_affect_schema(force_reload: bool = False) -> Dict[str, Any]:
    path = _get_schema_path()
    with _SCHEMA_LOCK:
        if not os.path.exists(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            data = copy.deepcopy(DEFAULT_AFFECT_SCHEMA)
            data["limits"] = _seed_limits_from_global_settings(data["limits"])
            try:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=4)
            except Exception:
                pass
            _SCHEMA_CACHE["path"] = path
            _SCHEMA_CACHE["mtime"] = os.path.getmtime(path) if os.path.exists(path) else None
            _SCHEMA_CACHE["data"] = data
            return data

        try:
            mtime = os.path.getmtime(path)
        except OSError:
            mtime = None

        if (
            not force_reload
            and _SCHEMA_CACHE["data"] is not None
            and _SCHEMA_CACHE["path"] == path
            and _SCHEMA_CACHE["mtime"] == mtime
        ):
            return _SCHEMA_CACHE["data"]

        data = copy.deepcopy(DEFAULT_AFFECT_SCHEMA)
        try:
            with open(path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                if int(loaded.get("schema_version", 1) or 1) < 4:
                    loaded = _migrate_schema(loaded)
                    try:
                        tmp = path + ".tmp"
                        with open(tmp, "w", encoding="utf-8") as wf:
                            json.dump(loaded, wf, indent=4)
                        os.replace(tmp, path)
                        mtime = os.path.getmtime(path)
                    except Exception:
                        pass
                if isinstance(loaded.get("axes"), list) and loaded["axes"]:
                    data["axes"] = [_normalize_axis(a) for a in loaded["axes"] if isinstance(a, dict) and a.get("id")]
                if isinstance(loaded.get("gradient_links"), dict):
                    data["gradient_links"] = loaded["gradient_links"]
                if isinstance(loaded.get("limits"), dict):
                    merged_limits = copy.deepcopy(DEFAULT_AFFECT_SCHEMA["limits"])
                    for k, v in loaded["limits"].items():
                        if isinstance(v, dict) and isinstance(merged_limits.get(k), dict):
                            merged_limits[k].update(v)
                        else:
                            merged_limits[k] = v
                    data["limits"] = merged_limits
                if isinstance(loaded.get("telemetry_events"), dict):
                    merged_events = copy.deepcopy(DEFAULT_AFFECT_SCHEMA["telemetry_events"])
                    merged_events.update(loaded["telemetry_events"])
                    data["telemetry_events"] = merged_events
                if "schema_version" in loaded:
                    data["schema_version"] = loaded["schema_version"]
        except Exception:
            pass

        _SCHEMA_CACHE["path"] = path
        _SCHEMA_CACHE["mtime"] = mtime
        _SCHEMA_CACHE["data"] = data
        return data


def save_affect_schema(schema: Dict[str, Any]) -> None:
    path = _get_schema_path()
    with _SCHEMA_LOCK:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(schema, f, indent=4)
        os.replace(tmp, path)
        _SCHEMA_CACHE["path"] = path
        _SCHEMA_CACHE["mtime"] = os.path.getmtime(path)
        _SCHEMA_CACHE["data"] = schema


def get_acute_effective_weight(schema: Optional[Dict[str, Any]] = None) -> float:
    sc = schema or load_affect_schema()
    return max(0.0, min(1.5, float(sc.get("limits", {}).get("acute_effective_weight", 0.65))))


def _get_agent_config(agent_name: str, agent_config: Any = None) -> Any:
    if agent_config is not None:
        return agent_config
    try:
        import toolbox
        av = toolbox._get_agent_view_ref()
        if av and hasattr(av, "agent_manager"):
            cfg = av.agent_manager.get_agent(agent_name)
            if cfg is not None:
                return cfg
    except Exception:
        pass
    try:
        import toolbox
        agents_dir = toolbox.get_storage_path("agents")
        for candidate in (f"{agent_name}.json", f"{agent_name.replace('_', ' ')}.json"):
            p = os.path.join(agents_dir, candidate)
            if os.path.exists(p):
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f)
                class _CfgProxy:
                    pass
                proxy = _CfgProxy()
                proxy.name = data.get("name", agent_name)
                proxy.pronouns = data.get("pronouns", "she/her")
                proxy.enable_emotions = data.get("enable_emotions", True)
                proxy.emotional_sensitivity = data.get("emotional_sensitivity", 1.0)
                proxy.mood_resilience = data.get("mood_resilience", 1.0)
                proxy.emotional_expressiveness = data.get("emotional_expressiveness", "balanced")
                return proxy
    except Exception:
        pass
    return None


def is_agent_emotions_enabled(agent_name: str, agent_config: Any = None, is_nomem: bool = False) -> bool:
    if is_nomem:
        return False
    try:
        import toolbox
        av = toolbox._get_agent_view_ref()
        if av and hasattr(av, "session_manager") and av.session_manager.is_no_memory():
            return False
    except Exception:
        pass
    cfg = _get_agent_config(agent_name, agent_config)
    if cfg is None:
        return True
    return bool(getattr(cfg, "enable_emotions", True))


def _clamp(val: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(val)))


def _canonical_entity_name(target: str, self_agent_name: str = "") -> str:
    raw = (target or "").strip().lstrip("@")
    if not raw:
        return ""
    low = raw.lower()
    if low == "user":
        return "User"
    try:
        import toolbox
        u_name = (toolbox.load_global_settings().get("user_name", "User") or "User").strip()
        if low == u_name.lower() or low.startswith(f"telegram {u_name.lower()}"):
            return "User"
        av = toolbox._get_agent_view_ref()
        if av and hasattr(av, "agent_manager"):
            matched = av.agent_manager.get_agent(raw)
            if matched:
                if self_agent_name and matched.name.lower() == self_agent_name.lower():
                    return ""
                return matched.name
    except Exception:
        pass
    if self_agent_name and low == self_agent_name.lower():
        return ""
    return raw


def _is_known_participant(canon_target: str) -> bool:
    if not canon_target:
        return False
    if canon_target == "User":
        return True
    try:
        import toolbox
        av = toolbox._get_agent_view_ref()
        if av and hasattr(av, "agent_manager"):
            return av.agent_manager.get_agent(canon_target) is not None
    except Exception:
        pass
    return False


def _global_axes(schema: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [ax for ax in schema.get("axes", []) if ax.get("layer_group") in _GLOBAL_AFFECT_GROUPS]


def _relational_axes(schema: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [ax for ax in schema.get("axes", []) if ax.get("layer_group") == "relational"]


def get_visible_relational_axes(
    schema: Dict[str, Any],
    agent_name: str,
    agent_config: Any = None,
) -> List[Dict[str, Any]]:
    return _relational_axes(schema)


def get_visible_axes(
    schema: Dict[str, Any],
    agent_name: str,
    agent_config: Any = None,
) -> List[Dict[str, Any]]:
    return list(schema.get("axes", []))


def _make_fresh_relation(schema: Dict[str, Any], now_ts: Optional[float] = None) -> Dict[str, Any]:
    acute = {}
    mood = {}
    for ax in _relational_axes(schema):
        aid = ax["id"]
        acute[aid] = 0.0
        mood[aid] = float(ax["baseline"])
    return {
        "last_updated_ts": time.time() if now_ts is None else float(now_ts),
        "acute": acute,
        "mood": mood,
    }


def _sync_relation_entry(entry: Dict[str, Any], schema: Dict[str, Any]) -> Dict[str, Any]:
    old_a = entry.get("acute", {}) or {}
    old_m = entry.get("mood", {}) or {}
    new_a = {}
    new_m = {}
    for ax in _relational_axes(schema):
        aid = ax["id"]
        lo, hi, base = float(ax["min"]), float(ax["max"]), float(ax["baseline"])
        span = hi - lo
        new_a[aid] = _clamp(old_a.get(aid, 0.0), -span, span)
        new_m[aid] = _clamp(old_m.get(aid, base), lo, hi)
    # Migrate v3 irritated_attracted if present and new axes were unpopulated
    if "irritated_attracted" in old_m and all(new_m.get(k, 0.0) == 0.0 for k in ("adoration", "irritation")):
        v3_m = float(old_m.get("irritated_attracted", 0.0) or 0.0)
        if v3_m > 0.0:
            new_m["adoration"] = _clamp(v3_m, 0.0, 1.0)
        elif v3_m < 0.0:
            new_m["irritation"] = _clamp(-v3_m, 0.0, 1.0)
    return {
        "last_updated_ts": float(entry.get("last_updated_ts", time.time())),
        "acute": new_a,
        "mood": new_m,
    }


def _make_fresh_state(schema: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    sc = schema or load_affect_schema()
    acute = {}
    mood = {}
    drives = {}
    for ax in sc["axes"]:
        aid = ax["id"]
        lg = ax["layer_group"]
        if lg in _GLOBAL_AFFECT_GROUPS:
            acute[aid] = 0.0
            mood[aid] = float(ax["baseline"])
        elif lg == "drive":
            drives[aid] = float(ax["baseline"])
    relations = {}
    if _relational_axes(sc):
        relations["User"] = _make_fresh_relation(sc)
    return {
        "schema_version": sc.get("schema_version", 4),
        "last_updated_ts": time.time(),
        "last_session_id": "",
        "last_session_ending_context": "",
        "acute": acute,
        "mood": mood,
        "drives": drives,
        "relations": relations,
        "desires": [],
    }


def _sync_state_to_schema(state: Dict[str, Any], schema: Dict[str, Any]) -> Dict[str, Any]:
    acute = {}
    mood = {}
    drives = {}
    old_acute = state.get("acute", {}) or {}
    old_mood = state.get("mood", {}) or {}
    old_drives = state.get("drives", {}) or {}
    old_ver = int(state.get("schema_version", 3) or 3)

    for ax in schema["axes"]:
        aid = ax["id"]
        lg = ax["layer_group"]
        lo, hi, base = float(ax["min"]), float(ax["max"]), float(ax["baseline"])
        span = hi - lo
        if lg in _GLOBAL_AFFECT_GROUPS:
            acute[aid] = _clamp(old_acute.get(aid, 0.0), -span, span)
            mood[aid] = _clamp(old_mood.get(aid, base), lo, hi)
        elif lg == "drive":
            drives[aid] = _clamp(old_drives.get(aid, base), lo, hi)

    # Non-destructive v3 -> v4 state migration
    if old_ver < 4 and "valence" in old_mood and "joy" not in old_mood:
        v_m = float(old_mood.get("valence", 0.0) or 0.0)
        if v_m > 0.05 and "joy" in mood:
            mood["joy"] = _clamp(v_m * 0.7, 0.0, 1.0)
            if "satisfaction" in mood:
                mood["satisfaction"] = _clamp(v_m * 0.5, 0.0, 1.0)
        elif v_m < -0.05 and "sadness" in mood:
            mood["sadness"] = _clamp((-v_m) * 0.7, 0.0, 1.0)
            if "disappointment" in mood:
                mood["disappointment"] = _clamp((-v_m) * 0.4, 0.0, 1.0)
        ad_m = float(old_mood.get("awe_dread", 0.0) or 0.0)
        if ad_m > 0.05 and "awe" in mood:
            mood["awe"] = _clamp(ad_m, 0.0, 1.0)
        elif ad_m < -0.05 and "horror" in mood:
            mood["horror"] = _clamp(-ad_m, 0.0, 1.0)
        conf_m = float(old_mood.get("confidence", 0.6) or 0.6)
        if conf_m < 0.40 and "anxiety" in mood:
            mood["anxiety"] = _clamp(max(mood["anxiety"], (0.60 - conf_m) * 0.8), 0.0, 1.0)

    rel_axes = _relational_axes(schema)
    old_rels = state.get("relations", {}) if isinstance(state.get("relations"), dict) else {}
    relations = {}
    if rel_axes:
        for ent, rdata in old_rels.items():
            if isinstance(rdata, dict) and ent:
                relations[ent] = _sync_relation_entry(rdata, schema)
        if "User" not in relations:
            relations["User"] = _make_fresh_relation(schema, now_ts=state.get("last_updated_ts"))

    state["schema_version"] = schema.get("schema_version", 4)
    state["acute"] = acute
    state["mood"] = mood
    state["drives"] = drives
    state["relations"] = relations
    if not isinstance(state.get("desires"), list):
        state["desires"] = []
    state.setdefault("last_updated_ts", time.time())
    state.setdefault("last_session_id", "")
    state.setdefault("last_session_ending_context", "")
    return state


def load_inner_state(
    agent_name: str,
    agent_config: Any = None,
    is_nomem: bool = False,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    schema = load_affect_schema()
    if is_nomem:
        ekey = f"{session_id or 'nomem'}:{agent_name}"
        with _EPHEMERAL_LOCK:
            if ekey not in _EPHEMERAL_STATES:
                _EPHEMERAL_STATES[ekey] = _make_fresh_state(schema)
            return _sync_state_to_schema(copy.deepcopy(_EPHEMERAL_STATES[ekey]), schema)

    lock = _get_agent_lock(agent_name)
    with lock:
        mem_dir = _get_agent_mem_dir(agent_name)
        path = os.path.join(mem_dir, "INNER_STATE.json")
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    return _sync_state_to_schema(loaded, schema)
            except Exception:
                pass
        return _make_fresh_state(schema)


def save_inner_state(
    agent_name: str,
    state: Dict[str, Any],
    is_nomem: bool = False,
    session_id: Optional[str] = None,
) -> None:
    if is_nomem:
        ekey = f"{session_id or 'nomem'}:{agent_name}"
        with _EPHEMERAL_LOCK:
            _EPHEMERAL_STATES[ekey] = copy.deepcopy(state)
        return

    lock = _get_agent_lock(agent_name)
    with lock:
        mem_dir = _get_agent_mem_dir(agent_name)
        os.makedirs(mem_dir, exist_ok=True)
        path = os.path.join(mem_dir, "INNER_STATE.json")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=4)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)


def reset_transient_state(
    agent_name: str,
    agent_config: Any = None,
    is_nomem: bool = False,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    schema = load_affect_schema()
    state = load_inner_state(agent_name, agent_config, is_nomem=is_nomem, session_id=session_id)
    fresh = _make_fresh_state(schema)
    state["acute"] = fresh["acute"]
    state["mood"] = fresh["mood"]
    state["drives"] = fresh["drives"]
    state["relations"] = fresh["relations"]
    state["last_updated_ts"] = time.time()
    state["last_session_ending_context"] = ""
    save_inner_state(agent_name, state, is_nomem=is_nomem, session_id=session_id)
    return state


def _compute_entity_sentiment_bias(agent_name: str, entity_name: str, schema: Dict[str, Any], is_nomem: bool = False) -> float:
    cap = max(0.0, float(schema.get("limits", {}).get("sentiment_bias_cap", 0.25)))
    if is_nomem or not entity_name or cap <= 0.0:
        return 0.0
    sentiments = load_sentiments(agent_name)
    if not sentiments:
        return 0.0
    ent_low = entity_name.lower()
    net = 0.0
    for s in sentiments:
        tgt = str(s.get("target", "")).lower()
        if tgt != ent_low:
            continue
        cat = str(s.get("category", "")).lower()
        weight = float(s.get("strength", 0.5)) * (0.5 + 0.5 * float(s.get("salience", 0.3)))
        if cat in ("like", "bond", "cherished_moment"):
            net += 0.12 * weight
        elif cat == "dislike":
            net -= 0.14 * weight
        elif cat == "value":
            imp = s.get("affect_imprint") or {}
            imp_val = float(imp.get("projected_valence", imp.get("valence", 0.0)) or 0.0)
            net += 0.08 * imp_val * weight
    return _clamp(net, -cap, cap)


def _compute_projected_valence_arousal(state: Dict[str, Any], schema: Dict[str, Any]) -> Tuple[float, float]:
    eff_w = get_acute_effective_weight(schema)
    val_num = 0.0
    aro_num = 0.0
    weight_sum = 0.0
    for ax in _global_axes(schema):
        aid = ax["id"]
        lo, hi, base = float(ax["min"]), float(ax["max"]), float(ax["baseline"])
        eff = _clamp(state["mood"].get(aid, base) + eff_w * state["acute"].get(aid, 0.0), lo, hi)
        if eff <= 0.01:
            continue
        vw = float(ax.get("valence_weight", 0.0))
        aw = float(ax.get("arousal_weight", 0.0))
        val_num += eff * vw
        aro_num += eff * aw
        weight_sum += eff
    if weight_sum <= 1e-6:
        return 0.0, 0.35
    proj_val = _clamp(val_num / max(0.85, weight_sum * 0.75), -1.0, 1.0)
    proj_aro = _clamp(0.35 + (aro_num / max(1.0, weight_sum)) * 0.55, 0.0, 1.0)
    return proj_val, proj_aro


def _evolve_state_in_place(
    state: Dict[str, Any],
    dt_seconds: float,
    agent_config: Any,
    schema: Dict[str, Any],
    agent_name: str = "",
    is_nomem: bool = False,
) -> Dict[str, Any]:
    if dt_seconds <= 0.0:
        return state
    resilience = max(0.25, min(4.0, float(getattr(agent_config, "mood_resilience", 1.0) or 1.0)))
    hours = dt_seconds / 3600.0
    limits = schema.get("limits", {})

    for ax in schema["axes"]:
        aid = ax["id"]
        lg = ax["layer_group"]
        lo, hi, base = float(ax["min"]), float(ax["max"]), float(ax["baseline"])
        span = hi - lo
        if lg in _GLOBAL_AFFECT_GROUPS:
            a_hl = max(1.0, float(ax.get("acute_half_life_sec", 900.0)))
            m_hl = max(1.0, float(ax.get("mood_half_life_sec", 28800.0)))
            state["acute"][aid] = _clamp(state["acute"].get(aid, 0.0) * (0.5 ** (dt_seconds / a_hl)), -span, span)
            cur_mood = float(state["mood"].get(aid, base))
            is_neg_cat = float(ax.get("valence_weight", 0.0)) < -0.1
            eff_m_hl = (m_hl / resilience) if (is_neg_cat and cur_mood > base) else m_hl
            new_mood = base + (cur_mood - base) * (0.5 ** (dt_seconds / eff_m_hl))
            state["mood"][aid] = _clamp(new_mood, lo, hi)
        elif lg == "drive":
            cur_d = float(state["drives"].get(aid, base))
            drift = float(ax.get("offline_drift_per_hour", 0.0))
            if drift > 0.0:
                cur_d = _clamp(cur_d + drift * hours, lo, hi)
            elif drift < 0.0:
                if cur_d > base:
                    cur_d = max(base, cur_d + drift * hours)
                else:
                    d_hl = max(1.0, float(ax.get("half_life_sec", 14400.0)))
                    cur_d = base + (cur_d - base) * (0.5 ** (dt_seconds / d_hl))
            else:
                d_hl = max(1.0, float(ax.get("half_life_sec", 43200.0)))
                cur_d = base + (cur_d - base) * (0.5 ** (dt_seconds / d_hl))
            state["drives"][aid] = _clamp(cur_d, lo, hi)

    conn_ax = next((a for a in schema["axes"] if a["id"] == "connection_hunger"), None)
    purp_ax = next((a for a in schema["axes"] if a["id"] == "purpose"), None)
    conn_thresh = float(conn_ax.get("high_threshold", 0.60)) if conn_ax else 0.60
    purp_thresh = float(purp_ax.get("low_threshold", 0.28)) if purp_ax else 0.28
    lonely_pull_rate = float(limits.get("loneliness_sad_pull", 0.03))
    purp_pull_rate = float(limits.get("low_purpose_sad_pull", 0.025))

    conn = float(state["drives"].get("connection_hunger", 0.2))
    purp = float(state["drives"].get("purpose", 0.5))
    if conn > conn_thresh and lonely_pull_rate > 0.0:
        pull = lonely_pull_rate * (conn - conn_thresh) * min(8.0, hours)
        if "sadness" in state["mood"]:
            state["mood"]["sadness"] = _clamp(state["mood"]["sadness"] + pull, 0.0, 1.0)
        if "boredom" in state["mood"]:
            state["mood"]["boredom"] = _clamp(state["mood"]["boredom"] + 0.6 * pull, 0.0, 1.0)
    if purp < purp_thresh and purp_pull_rate > 0.0:
        pull = purp_pull_rate * (purp_thresh - purp) * min(8.0, hours)
        if "disappointment" in state["mood"]:
            state["mood"]["disappointment"] = _clamp(state["mood"]["disappointment"] + pull, 0.0, 1.0)
        if "sadness" in state["mood"]:
            state["mood"]["sadness"] = _clamp(state["mood"]["sadness"] + 0.5 * pull, 0.0, 1.0)

    rel_axes = _relational_axes(schema)
    if rel_axes:
        for ent, rdata in (state.get("relations") or {}).items():
            ent_bias = _compute_entity_sentiment_bias(agent_name, ent, schema, is_nomem=is_nomem)
            for ax in rel_axes:
                aid = ax["id"]
                lo, hi, base = float(ax["min"]), float(ax["max"]), float(ax["baseline"])
                span = hi - lo
                eff_base = base
                if ent_bias > 0.0 and aid in ("adoration", "admiration"):
                    eff_base = _clamp(base + ent_bias, lo, hi)
                elif ent_bias < 0.0 and aid in ("irritation", "caution"):
                    eff_base = _clamp(base + (-ent_bias), lo, hi)
                a_hl = max(1.0, float(ax.get("acute_half_life_sec", 1200.0)))
                m_hl = max(1.0, float(ax.get("mood_half_life_sec", 86400.0)))
                rdata["acute"][aid] = _clamp(rdata["acute"].get(aid, 0.0) * (0.5 ** (dt_seconds / a_hl)), -span, span)
                cur_m = float(rdata["mood"].get(aid, eff_base))
                is_neg_rel = aid in ("irritation", "caution", "empathic_pain")
                eff_m_hl = (m_hl / resilience) if (is_neg_rel and cur_m > eff_base) else m_hl
                rdata["mood"][aid] = _clamp(eff_base + (cur_m - eff_base) * (0.5 ** (dt_seconds / eff_m_hl)), lo, hi)
            rdata["last_updated_ts"] = float(rdata.get("last_updated_ts", time.time())) + dt_seconds

    return state


def advance_time(
    agent_name: str,
    agent_config: Any = None,
    now_ts: Optional[float] = None,
    persist: bool = True,
    is_nomem: bool = False,
    session_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    cfg = _get_agent_config(agent_name, agent_config)
    if not is_agent_emotions_enabled(agent_name, cfg, is_nomem=is_nomem):
        return None
    schema = load_affect_schema()
    t_now = time.time() if now_ts is None else float(now_ts)
    lock = _get_agent_lock(agent_name)
    with lock:
        state = load_inner_state(agent_name, cfg, is_nomem=is_nomem, session_id=session_id)
        last_ts = float(state.get("last_updated_ts", t_now))
        dt = max(0.0, t_now - last_ts)
        _evolve_state_in_place(state, dt, cfg, schema, agent_name=agent_name, is_nomem=is_nomem)
        state["last_updated_ts"] = t_now
        if persist:
            save_inner_state(agent_name, state, is_nomem=is_nomem, session_id=session_id)
        return state


def simulate_time_passage(
    agent_name: str,
    agent_config: Any = None,
    hours: float = 6.0,
    is_nomem: bool = False,
    session_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    cfg = _get_agent_config(agent_name, agent_config)
    if not is_agent_emotions_enabled(agent_name, cfg, is_nomem=is_nomem):
        return None
    schema = load_affect_schema()
    lock = _get_agent_lock(agent_name)
    with lock:
        state = load_inner_state(agent_name, cfg, is_nomem=is_nomem, session_id=session_id)
        _evolve_state_in_place(state, max(0.0, float(hours) * 3600.0), cfg, schema, agent_name=agent_name, is_nomem=is_nomem)
        state["last_updated_ts"] = time.time()
        for rdata in (state.get("relations") or {}).values():
            rdata["last_updated_ts"] = state["last_updated_ts"]
        save_inner_state(agent_name, state, is_nomem=is_nomem, session_id=session_id)
        return state


def _expand_legacy_and_fallback_deltas(
    deltas: Dict[str, float],
    has_known_participant_target: bool,
) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for k, raw_v in deltas.items():
        try:
            v = float(raw_v)
        except (TypeError, ValueError):
            continue
        if math.isnan(v) or math.isinf(v) or v == 0.0:
            continue
        kl = str(k).lower().strip()
        if kl == "valence":
            if v > 0:
                out["joy"] = out.get("joy", 0.0) + 0.7 * v
                out["satisfaction"] = out.get("satisfaction", 0.0) + 0.5 * v
            else:
                out["sadness"] = out.get("sadness", 0.0) + 0.7 * (-v)
                out["disappointment"] = out.get("disappointment", 0.0) + 0.4 * (-v)
        elif kl == "arousal":
            if v > 0:
                out["excitement"] = out.get("excitement", 0.0) + v
            else:
                out["calmness"] = out.get("calmness", 0.0) + (-v)
        elif kl == "confidence":
            if v > 0:
                out["pride"] = out.get("pride", 0.0) + 0.6 * v
                out["satisfaction"] = out.get("satisfaction", 0.0) + 0.4 * v
            else:
                out["anxiety"] = out.get("anxiety", 0.0) + 0.6 * (-v)
                out["confusion"] = out.get("confusion", 0.0) + 0.4 * (-v)
        elif kl == "awe_dread":
            if v > 0:
                out["awe"] = out.get("awe", 0.0) + v
            else:
                out["horror"] = out.get("horror", 0.0) + (-v)
        elif kl == "irritated_attracted":
            if v > 0:
                out["adoration"] = out.get("adoration", 0.0) + v
            else:
                out["irritation"] = out.get("irritation", 0.0) + (-v)
        elif kl == "irritation" and not has_known_participant_target:
            out["anger"] = out.get("anger", 0.0) + v
        elif kl == "caution" and not has_known_participant_target:
            out["fear"] = out.get("fear", 0.0) + v
        else:
            out[kl] = out.get(kl, 0.0) + v
    return out


def _integrate_deltas_in_place(
    state: Dict[str, Any],
    deltas: Dict[str, float],
    agent_config: Any,
    schema: Dict[str, Any],
    from_llm: bool = False,
    target_entity: Optional[str] = None,
    agent_name: str = "",
) -> Dict[str, Any]:
    if not deltas:
        return state
    sensitivity = max(0.2, min(3.0, float(getattr(agent_config, "emotional_sensitivity", 1.0) or 1.0)))
    limits = schema.get("limits", {})
    max_a_delta = float(limits.get("max_acute_delta_per_turn", 0.35))
    max_d_delta = float(limits.get("max_drive_delta_per_turn", 0.25))

    conn_hunger = float(state["drives"].get("connection_hunger", 0.2))
    fatigue = float(state["drives"].get("mental_fatigue", 0.1))

    canon_target = _canonical_entity_name(target_entity or "", self_agent_name=agent_name) if target_entity else ""
    known_participant = _is_known_participant(canon_target)

    axis_by_id = {ax["id"]: ax for ax in schema.get("axes", [])}
    expanded = _expand_legacy_and_fallback_deltas(deltas, known_participant)

    # Filter NSFW axes early and add split-verdict cross-routing + 1-hop gradient diffusion
    staged: Dict[str, float] = {}
    for aid, raw_d in expanded.items():
        ax = axis_by_id.get(aid)
        if not ax:
            continue
        if from_llm and ax["layer_group"] != "drive":
            raw_d = _clamp(raw_d, -max_a_delta, max_a_delta)
        staged[aid] = staged.get(aid, 0.0) + raw_d

    # Split-verdict cross-routing when target is a known person/agent
    if known_participant and canon_target:
        for src_id, (rel_id, mult) in _SPLIT_VERDICT_RELATIONAL_MAP.items():
            if src_id in staged and rel_id in axis_by_id:
                rel_ax = axis_by_id[rel_id]
                staged[rel_id] = staged.get(rel_id, 0.0) + (staged[src_id] * mult)

    # 1-hop positive gradient neighbor coupling (Cowen & Keltner Fig. 2A/2B)
    grad_links = schema.get("gradient_links", {}) or {}
    neighbor_additions: Dict[str, float] = {}
    for aid, raw_d in staged.items():
        if raw_d <= 0.0:
            continue
        for nb_id, weight in (grad_links.get(aid) or {}).items():
            nb_ax = axis_by_id.get(nb_id)
            if not nb_ax:
                continue
            if nb_ax["layer_group"] == "relational" and not known_participant:
                continue
            neighbor_additions[nb_id] = neighbor_additions.get(nb_id, 0.0) + (raw_d * float(weight))

    for nb_id, add_d in neighbor_additions.items():
        staged[nb_id] = staged.get(nb_id, 0.0) + add_d

    # Apply staged deltas to respective state partitions
    for ax in schema["axes"]:
        aid = ax["id"]
        if aid not in staged:
            continue
        raw_d = float(staged[aid])
        if math.isnan(raw_d) or math.isinf(raw_d) or raw_d == 0.0:
            continue

        lg = ax["layer_group"]
        lo, hi, base = float(ax["min"]), float(ax["max"]), float(ax["baseline"])
        span = hi - lo

        if lg in _GLOBAL_AFFECT_GROUPS:
            if from_llm:
                raw_d = _clamp(raw_d, -max_a_delta, max_a_delta)
            d = raw_d * sensitivity

            cur_mood = float(state["mood"].get(aid, base))
            bias_gain = float(ax.get("mood_bias_gain", 0.25))
            mood_offset = (cur_mood - base) / max(0.5, span)
            is_neg_cat = float(ax.get("valence_weight", 0.0)) < -0.1
            if mood_offset > 0.12 and d > 0:
                d *= (1.0 + 0.5 * bias_gain * mood_offset)

            if is_neg_cat:
                if conn_hunger > 0.60 and d > 0:
                    d *= (1.0 + 0.30 * (conn_hunger - 0.60))
                if fatigue > 0.65 and d > 0:
                    d *= (1.0 + 0.30 * (fatigue - 0.65))
            elif aid in ("excitement", "joy") and fatigue > 0.65 and d > 0:
                d *= max(0.45, 1.0 - 0.50 * (fatigue - 0.65))

            new_acute = _clamp(state["acute"].get(aid, 0.0) + d, -span, span)
            state["acute"][aid] = new_acute
            charge_gain = float(ax.get("mood_charge_gain", 0.22))
            state["mood"][aid] = _clamp(cur_mood + d * charge_gain, lo, hi)

            # Relief actively clears residual threat/anxiety tension
            if aid == "relief" and d > 0.0:
                damp = max(0.25, 1.0 - 0.85 * min(1.0, d))
                for t_ax in ("anxiety", "fear", "horror"):
                    if t_ax in state["acute"]:
                        state["acute"][t_ax] *= damp
                    if t_ax in state["mood"]:
                        state["mood"][t_ax] *= max(0.55, 1.0 - 0.35 * min(1.0, d))

        elif lg == "drive":
            if from_llm:
                raw_d = _clamp(raw_d, -max_d_delta, max_d_delta)
            state["drives"][aid] = _clamp(state["drives"].get(aid, base) + raw_d, lo, hi)

        elif lg == "relational":
            ent = canon_target if known_participant else "User"
            rels = state.setdefault("relations", {})
            if ent not in rels:
                rels[ent] = _make_fresh_relation(schema)
            rdata = rels[ent]
            if from_llm:
                raw_d = _clamp(raw_d, -max_a_delta, max_a_delta)
            d = raw_d * sensitivity
            cur_m = float(rdata["mood"].get(aid, base))
            bias_gain = float(ax.get("mood_bias_gain", 0.25))
            mood_offset = (cur_m - base) / max(0.5, span)
            if mood_offset > 0.12 and d > 0:
                d *= (1.0 + 0.5 * bias_gain * mood_offset)
            rdata["acute"][aid] = _clamp(rdata["acute"].get(aid, 0.0) + d, -span, span)
            charge_gain = float(ax.get("mood_charge_gain", 0.25))
            rdata["mood"][aid] = _clamp(cur_m + d * charge_gain, lo, hi)
            rdata["last_updated_ts"] = time.time()

    return state


def apply_telemetry_event(
    agent_name: str,
    agent_config: Any,
    event_name: str,
    scale: float = 1.0,
    persist: bool = True,
    is_nomem: bool = False,
    session_id: Optional[str] = None,
    sender_affect: Optional[Dict[str, Any]] = None,
    target_entity: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    cfg = _get_agent_config(agent_name, agent_config)
    if not is_agent_emotions_enabled(agent_name, cfg, is_nomem=is_nomem):
        return None
    schema = load_affect_schema()
    raw_weights = schema.get("telemetry_events", {}).get(event_name, {})
    deltas = {k: float(v) * float(scale) for k, v in raw_weights.items()}

    eff_target = target_entity
    if eff_target is None and event_name in ("user_confirm_approved", "user_confirm_rejected", "user_confirm_rejected_with_comment", "user_abort", "user_interaction"):
        eff_target = "User"

    if event_name == "intercom_received" and isinstance(sender_affect, dict):
        contagion_gain = float(schema.get("limits", {}).get("intercom_contagion_gain", 0.10))
        s_mood = sender_affect.get("mood") or {}
        if contagion_gain != 0.0:
            # Complementary + resonant contagion (never copies raw irritation blindly)
            sad_distress = max(
                float(s_mood.get("sadness", 0.0)),
                float(s_mood.get("horror", 0.0)),
                float(s_mood.get("disappointment", 0.0)),
                float(s_mood.get("anxiety", 0.0)),
            )
            if sad_distress > 0.18:
                deltas["empathic_pain"] = deltas.get("empathic_pain", 0.0) + (contagion_gain * sad_distress * 1.2)
                deltas["caution"] = deltas.get("caution", 0.0) + (contagion_gain * sad_distress * 0.5)
            pos_energy = max(
                float(s_mood.get("amusement", 0.0)),
                float(s_mood.get("joy", 0.0)),
                float(s_mood.get("excitement", 0.0)),
                float(s_mood.get("pride", 0.0)),
            )
            if pos_energy > 0.18:
                deltas["amusement"] = deltas.get("amusement", 0.0) + (contagion_gain * float(s_mood.get("amusement", 0.0)))
                deltas["interest"] = deltas.get("interest", 0.0) + (contagion_gain * pos_energy * 0.7)

    lock = _get_agent_lock(agent_name)
    with lock:
        state = advance_time(agent_name, cfg, persist=False, is_nomem=is_nomem, session_id=session_id)
        if state is None:
            return None
        if event_name == "work_completed" and float(state.get("mood", {}).get("anxiety", 0.0)) > 0.18:
            deltas["relief"] = deltas.get("relief", 0.0) + 0.08
        _integrate_deltas_in_place(state, deltas, cfg, schema, from_llm=False, target_entity=eff_target, agent_name=agent_name)
        state["last_updated_ts"] = time.time()
        if persist:
            save_inner_state(agent_name, state, is_nomem=is_nomem, session_id=session_id)
        return state


def apply_manual_axis_nudge(
    agent_name: str,
    agent_config: Any,
    axis_id: str,
    delta: float,
    is_nomem: bool = False,
    session_id: Optional[str] = None,
    target_entity: str = "User",
) -> Optional[Dict[str, Any]]:
    cfg = _get_agent_config(agent_name, agent_config)
    if not is_agent_emotions_enabled(agent_name, cfg, is_nomem=is_nomem):
        return None
    schema = load_affect_schema()
    lock = _get_agent_lock(agent_name)
    with lock:
        state = load_inner_state(agent_name, cfg, is_nomem=is_nomem, session_id=session_id)
        for ax in schema["axes"]:
            if ax["id"] == axis_id:
                lo, hi = float(ax["min"]), float(ax["max"])
                lg = ax["layer_group"]
                if lg in _GLOBAL_AFFECT_GROUPS:
                    state["mood"][axis_id] = _clamp(state["mood"].get(axis_id, ax["baseline"]) + delta, lo, hi)
                    state["acute"][axis_id] = _clamp(state["acute"].get(axis_id, 0.0) + (delta * 0.5), -(hi - lo), hi - lo)
                elif lg == "drive":
                    state["drives"][axis_id] = _clamp(state["drives"].get(axis_id, ax["baseline"]) + delta, lo, hi)
                elif lg == "relational":
                    ent = _canonical_entity_name(target_entity, self_agent_name=agent_name) or "User"
                    rels = state.setdefault("relations", {})
                    if ent not in rels:
                        rels[ent] = _make_fresh_relation(schema)
                    rdata = rels[ent]
                    rdata["mood"][axis_id] = _clamp(rdata["mood"].get(axis_id, ax["baseline"]) + delta, lo, hi)
                    rdata["acute"][axis_id] = _clamp(rdata["acute"].get(axis_id, 0.0) + (delta * 0.5), -(hi - lo), hi - lo)
                    rdata["last_updated_ts"] = time.time()
                break
        state["last_updated_ts"] = time.time()
        save_inner_state(agent_name, state, is_nomem=is_nomem, session_id=session_id)
        return state


def apply_self_appraisal(
    agent_name: str,
    agent_config: Any,
    updates: List[Dict[str, Any]],
    is_nomem: bool = False,
    session_id: Optional[str] = None,
    default_target: str = "User",
) -> Optional[Dict[str, Any]]:
    cfg = _get_agent_config(agent_name, agent_config)
    if not is_agent_emotions_enabled(agent_name, cfg, is_nomem=is_nomem) or not updates:
        return None
    schema = load_affect_schema()
    lock = _get_agent_lock(agent_name)
    with lock:
        state = load_inner_state(agent_name, cfg, is_nomem=is_nomem, session_id=session_id)
        for u in updates:
            deltas = u.get("deltas") or {}
            if not deltas:
                continue
            tgt = (u.get("target") or "").strip() or default_target
            _integrate_deltas_in_place(state, deltas, cfg, schema, from_llm=True, target_entity=tgt, agent_name=agent_name)
        state["last_updated_ts"] = time.time()
        save_inner_state(agent_name, state, is_nomem=is_nomem, session_id=session_id)
        return state


def on_turn_start(
    agent_config: Any,
    prompt: str = "",
    is_nomem: bool = False,
    session_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    agent_name = getattr(agent_config, "name", "")
    if not is_agent_emotions_enabled(agent_name, agent_config, is_nomem=is_nomem):
        return None
    lock = _get_agent_lock(agent_name)
    with lock:
        state = advance_time(agent_name, agent_config, persist=False, is_nomem=is_nomem, session_id=session_id)
        if state is None:
            return None
        prev_sid = state.get("last_session_id", "")
        if session_id and prev_sid != session_id:
            state["last_session_id"] = session_id

        if "<AGENT_INTERCOM" not in (prompt or ""):
            schema = load_affect_schema()
            w = schema.get("telemetry_events", {}).get("user_interaction", {})
            _integrate_deltas_in_place(state, w, agent_config, schema, from_llm=False, target_entity="User", agent_name=agent_name)

        save_inner_state(agent_name, state, is_nomem=is_nomem, session_id=session_id)
        return state


def on_turn_complete(
    agent_config: Any,
    updates: List[Dict[str, Any]],
    is_consolidation: bool = False,
    had_tool_work: bool = False,
    default_target: str = "User",
    is_nomem: bool = False,
    session_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    agent_name = getattr(agent_config, "name", "")
    if not is_agent_emotions_enabled(agent_name, agent_config, is_nomem=is_nomem):
        return None
    lock = _get_agent_lock(agent_name)
    with lock:
        if updates:
            apply_self_appraisal(
                agent_name, agent_config, updates,
                is_nomem=is_nomem, session_id=session_id, default_target=default_target
            )
        if is_consolidation:
            event_name = "memories_consolidated"
        elif had_tool_work:
            event_name = "work_completed"
        else:
            event_name = "task_completed"
        state = apply_telemetry_event(agent_name, agent_config, event_name, persist=False, is_nomem=is_nomem, session_id=session_id)
        if state is not None:
            schema = load_affect_schema()
            val, aro = _compute_projected_valence_arousal(state, schema)
            if val <= -0.25 and aro < 0.45:
                state["last_session_ending_context"] = "ended feeling sad or heavy-hearted"
            elif val < -0.20:
                state["last_session_ending_context"] = "ended with unresolved tension or frustration"
            elif val > 0.35:
                state["last_session_ending_context"] = "ended on a warm, satisfied note"
            else:
                state["last_session_ending_context"] = "ended calmly"
            if session_id:
                state["last_session_id"] = session_id
            save_inner_state(agent_name, state, is_nomem=is_nomem, session_id=session_id)
        return state


def on_turn_abort(
    agent_config: Any,
    is_nomem: bool = False,
    session_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    agent_name = getattr(agent_config, "name", "")
    if not is_agent_emotions_enabled(agent_name, agent_config, is_nomem=is_nomem):
        return None
    lock = _get_agent_lock(agent_name)
    with lock:
        state = apply_telemetry_event(
            agent_name, agent_config, "user_abort", persist=False,
            is_nomem=is_nomem, session_id=session_id, target_entity="User"
        )
        if state is not None:
            state["last_session_ending_context"] = "aborted / interrupted abruptly mid-turn"
            if session_id:
                state["last_session_id"] = session_id
            save_inner_state(agent_name, state, is_nomem=is_nomem, session_id=session_id)
        return state


def notify_telemetry_for_agent(agent_name: str, event_name: str, scale: float = 1.0, target_entity: Optional[str] = None) -> Optional[Dict[str, Any]]:
    cfg = _get_agent_config(agent_name)
    if not is_agent_emotions_enabled(agent_name, cfg):
        return None
    is_nomem = False
    sid = None
    try:
        import toolbox
        av = toolbox._get_agent_view_ref()
        if av and hasattr(av, "session_manager"):
            is_nomem = av.session_manager.is_no_memory()
            sid = av.session_manager.current_session_id
    except Exception:
        pass
    return apply_telemetry_event(agent_name, cfg, event_name, scale=scale, is_nomem=is_nomem, session_id=sid, target_entity=target_entity)


def notify_intercom_received(
    agent_name: str,
    sender_affect: Optional[Dict[str, Any]] = None,
    sender_name: Optional[str] = None,
    is_nomem: bool = False,
    session_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    cfg = _get_agent_config(agent_name)
    if not is_agent_emotions_enabled(agent_name, cfg, is_nomem=is_nomem):
        return None
    return apply_telemetry_event(
        agent_name,
        cfg,
        "intercom_received",
        is_nomem=is_nomem,
        session_id=session_id,
        sender_affect=sender_affect,
        target_entity=sender_name,
    )


def _compute_salience(state: Dict[str, Any], schema: Dict[str, Any]) -> float:
    eff_w = get_acute_effective_weight(schema)
    devs = []
    for ax in _global_axes(schema):
        aid = ax["id"]
        lo, hi, base = float(ax["min"]), float(ax["max"]), float(ax["baseline"])
        span = max(0.5, hi - lo)
        eff = _clamp(state["mood"].get(aid, base) + eff_w * state["acute"].get(aid, 0.0), lo, hi)
        dev = min(1.0, abs(eff - base) / span)
        if dev > 0.03:
            devs.append(dev)
    if not devs:
        return 0.0
    devs.sort(reverse=True)
    top_devs = devs[:4]
    peak = top_devs[0]
    rms = math.sqrt(sum(d * d for d in top_devs) / len(top_devs))
    return round(min(1.0, 0.60 * peak + 0.40 * rms), 4)


_MOOD_ADJECTIVES = {
    "calmness": "calm",
    "interest": "curious",
    "joy": "joyful",
    "amusement": "amused",
    "excitement": "excited",
    "satisfaction": "satisfied",
    "relief": "relieved",
    "awe": "awestruck",
    "entrancement": "entranced",
    "nostalgia": "nostalgic",
    "surprise": "surprised",
    "confusion": "puzzled",
    "awkwardness": "awkward",
    "boredom": "bored",
    "anxiety": "anxious",
    "anger": "frustrated",
    "horror": "shaken",
    "sadness": "sad",
    "pride": "proud",
    "disappointment": "disappointed",
    "guilt": "remorseful",
    "aesthetic_appreciation": "moved",
    "disgust": "averse",
    "fear": "wary",
}


def get_mood_label(
    agent_name: str,
    agent_config: Any = None,
    is_nomem: bool = False,
    session_id: Optional[str] = None,
) -> str:
    cfg = _get_agent_config(agent_name, agent_config)
    if not is_agent_emotions_enabled(agent_name, cfg, is_nomem=is_nomem):
        return ""
    schema = load_affect_schema()
    eff_w = get_acute_effective_weight(schema)
    state = load_inner_state(agent_name, cfg, is_nomem=is_nomem, session_id=session_id)

    active = []
    for ax in _global_axes(schema):
        aid = ax["id"]
        lo, hi, base = float(ax["min"]), float(ax["max"]), float(ax["baseline"])
        eff = _clamp(state["mood"].get(aid, base) + eff_w * state["acute"].get(aid, 0.0), lo, hi)
        excess = eff - base
        if excess >= 0.16 or (base == 0.0 and eff >= 0.20):
            active.append((excess, eff, aid, ax.get("label", aid).lower()))

    active.sort(key=lambda item: (item[0], item[1]), reverse=True)
    if len(active) >= 2 and active[1][0] >= 0.16:
        a1 = _MOOD_ADJECTIVES.get(active[0][2], active[0][3])
        a2 = _MOOD_ADJECTIVES.get(active[1][2], active[1][3])
        if a1 != a2:
            return f"{a1} & {a2}"
        return a1
    if active:
        return _MOOD_ADJECTIVES.get(active[0][2], active[0][3])

    fatigue = float(state["drives"].get("mental_fatigue", 0.1))
    conn = float(state["drives"].get("connection_hunger", 0.2))
    cur = float(state["drives"].get("curiosity", 0.4))
    if fatigue > 0.72:
        return "weary"
    if conn > 0.65:
        return "lonely"
    if cur > 0.72:
        return "curious"
    return "steady"


def get_affect_snapshot(
    agent_name: str,
    agent_config: Any = None,
    thought: str = "",
    is_nomem: bool = False,
    session_id: Optional[str] = None,
    interlocutor: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    cfg = _get_agent_config(agent_name, agent_config)
    if not is_agent_emotions_enabled(agent_name, cfg, is_nomem=is_nomem):
        return None
    schema = load_affect_schema()
    state = load_inner_state(agent_name, cfg, is_nomem=is_nomem, session_id=session_id)
    proj_val, proj_aro = _compute_projected_valence_arousal(state, schema)
    snap: Dict[str, Any] = {
        "mood": dict(state.get("mood", {})),
        "acute": dict(state.get("acute", {})),
        "drives": dict(state.get("drives", {})),
        "projected_valence": round(proj_val, 4),
        "projected_arousal": round(proj_aro, 4),
        "salience": _compute_salience(state, schema),
        "label": get_mood_label(agent_name, cfg, is_nomem=is_nomem, session_id=session_id),
        "thought": (thought or "").strip(),
    }
    if interlocutor:
        ent = _canonical_entity_name(interlocutor, self_agent_name=agent_name)
        if ent and ent in (state.get("relations") or {}):
            rel_copy = copy.deepcopy(state["relations"][ent])
            snap["target_entity"] = ent
            snap["relation"] = rel_copy
    return snap


def get_tts_speed_offset(
    agent_name: str,
    agent_config: Any = None,
    is_nomem: bool = False,
    session_id: Optional[str] = None,
) -> float:
    cfg = _get_agent_config(agent_name, agent_config)
    if not is_agent_emotions_enabled(agent_name, cfg, is_nomem=is_nomem):
        return 0.0
    schema = load_affect_schema()
    max_off = float(schema.get("limits", {}).get("tts_speed_max_offset", 0.10))
    state = load_inner_state(agent_name, cfg, is_nomem=is_nomem, session_id=session_id)
    val, aro = _compute_projected_valence_arousal(state, schema)
    fat = float(state["drives"].get("mental_fatigue", 0.10))
    raw = (aro - 0.35) * 0.14 + (val * 0.03) - max(0.0, fat - 0.5) * 0.08
    return round(_clamp(raw, -max_off, max_off), 3)


def load_sentiments(agent_name: str) -> List[Dict[str, Any]]:
    lock = _get_agent_lock(agent_name)
    with lock:
        path = os.path.join(_get_agent_mem_dir(agent_name), "SENTIMENTS.json")
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, list):
                    return data
            except Exception:
                pass
        return []


def save_sentiments(agent_name: str, data: List[Dict[str, Any]]) -> None:
    lock = _get_agent_lock(agent_name)
    with lock:
        mem_dir = _get_agent_mem_dir(agent_name)
        os.makedirs(mem_dir, exist_ok=True)
        path = os.path.join(mem_dir, "SENTIMENTS.json")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)


def manage_sentiment(
    agent_name: str,
    agent_config: Any,
    action: str = "add",
    category: str = "like",
    target: str = "",
    subject: str = "",
    content: str = "",
    id: str = "",
) -> str:
    cfg = _get_agent_config(agent_name, agent_config)
    if not is_agent_emotions_enabled(agent_name, cfg, is_nomem=is_nomem):
        return f"[{agent_name}] Emotions are disabled for this agent."

    act = (action or "add").strip().lower()
    cat = (category or "like").strip().lower()
    if cat not in ("like", "dislike", "bond", "cherished_moment", "value"):
        cat = "like"

    target_id = (id or "").strip()
    if target_id.lower() in ("null", "none", "undefined"):
        target_id = ""
    text_content = (content or "").strip()
    text_subject = (subject or "").strip()
    text_target = (target or "").strip()

    snap = get_affect_snapshot(agent_name, cfg) or {}
    imprint = dict(snap.get("mood", {}))
    salience = float(snap.get("salience", 0.3))
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    now_ts = time.time()

    lock = _get_agent_lock(agent_name)
    with lock:
        items = load_sentiments(agent_name)
        if act == "delete" or (target_id and act == "edit" and not text_content):
            if not target_id:
                return f"[{agent_name}] Error: 'id' is required to delete a sentiment."
            before = len(items)
            items = [it for it in items if str(it.get("id")) != target_id]
            if len(items) == before:
                return f"[{agent_name}] Error: Sentiment ID '{target_id}' not found."
            save_sentiments(agent_name, items)
            return f"[{agent_name}] Sentiment '{target_id}' deleted from SENTIMENTS.json."

        if act == "edit" or target_id:
            if not target_id:
                return f"[{agent_name}] Error: 'id' is required to edit a sentiment."
            if not text_content:
                return f"[{agent_name}] Error: 'content' is required to edit a sentiment."
            for it in items:
                if str(it.get("id")) == target_id:
                    if category:
                        it["category"] = cat
                    if text_target:
                        it["target"] = text_target
                    if text_subject:
                        it["subject"] = text_subject
                    it["content"] = text_content
                    it["affect_imprint"] = imprint
                    it["salience"] = max(float(it.get("salience", 0.0)), salience)
                    it["strength"] = min(1.0, float(it.get("strength", 0.5)) + 0.1)
                    it["timestamp"] = now_str
                    it["last_triggered_ts"] = now_ts
                    save_sentiments(agent_name, items)
                    return f"[{agent_name}] Sentiment '{target_id}' updated in SENTIMENTS.json."
            return f"[{agent_name}] Error: Sentiment ID '{target_id}' not found."

        if not text_content:
            return f"[{agent_name}] Error: 'content' is required when adding a sentiment."
        existing_ids = [int(it["id"]) for it in items if str(it.get("id", "")).isdigit()]
        new_id = str(max(existing_ids) + 1) if existing_ids else "1"
        entry = {
            "id": new_id,
            "category": cat,
            "target": text_target or text_subject or "General",
            "subject": text_subject or text_target or cat.title(),
            "content": text_content,
            "affect_imprint": imprint,
            "salience": max(0.15, salience),
            "strength": min(1.0, max(0.35, 0.35 + 0.5 * salience)),
            "timestamp": now_str,
            "last_triggered_ts": now_ts,
        }
        items.append(entry)
        save_sentiments(agent_name, items)
        return f"[{agent_name}] Saved {cat} sentiment '{new_id}' ({entry['subject']}) to SENTIMENTS.json."


def manage_desire(
    agent_name: str,
    agent_config: Any,
    action: str = "add",
    content: str = "",
    linked_drive: str = "curiosity",
    urgency: float = 0.6,
    id: str = "",
    is_nomem: bool = False,
    session_id: Optional[str] = None,
) -> str:
    cfg = _get_agent_config(agent_name, agent_config)
    if not is_agent_emotions_enabled(agent_name, cfg):
        return f"[{agent_name}] Emotions are disabled for this agent."

    act = (action or "add").strip().lower()
    target_id = (id or "").strip()
    text_content = (content or "").strip()
    schema = load_affect_schema()
    max_desires = int(schema.get("limits", {}).get("max_active_desires", 8))

    lock = _get_agent_lock(agent_name)
    with lock:
        state = load_inner_state(agent_name, cfg, is_nomem=is_nomem, session_id=session_id)
        desires = state.get("desires", [])

        if act in ("fulfill", "release", "reinforce", "delete"):
            if not target_id:
                return f"[{agent_name}] Error: 'id' is required for desire action '{act}'."
            if act == "delete":
                before = len(desires)
                desires = [d for d in desires if str(d.get("id")) != target_id]
                if len(desires) == before:
                    return f"[{agent_name}] Error: Desire '{target_id}' not found."
                state["desires"] = desires
                save_inner_state(agent_name, state, is_nomem=is_nomem, session_id=session_id)
                return f"[{agent_name}] Desire '{target_id}' deleted."

            found = None
            for d in desires:
                if str(d.get("id")) == target_id:
                    found = d
                    break
            if not found:
                return f"[{agent_name}] Error: Desire '{target_id}' not found."

            if act == "fulfill":
                found["status"] = "fulfilled"
                found["fulfilled_ts"] = time.time()
                state["desires"] = desires
                save_inner_state(agent_name, state, is_nomem=is_nomem, session_id=session_id)
                apply_telemetry_event(agent_name, cfg, "desire_fulfilled", is_nomem=is_nomem, session_id=session_id)
                return f"[{agent_name}] Desire '{target_id}' fulfilled!"
            elif act == "release":
                found["status"] = "released"
                found["released_ts"] = time.time()
                state["desires"] = desires
                save_inner_state(agent_name, state, is_nomem=is_nomem, session_id=session_id)
                return f"[{agent_name}] Desire '{target_id}' gently released."
            else:
                found["urgency"] = _clamp(float(found.get("urgency", 0.5)) + 0.15, 0.0, 1.0)
                found["last_reinforced_ts"] = time.time()
                if text_content:
                    found["content"] = text_content
                state["desires"] = desires
                save_inner_state(agent_name, state, is_nomem=is_nomem, session_id=session_id)
                return f"[{agent_name}] Desire '{target_id}' reinforced."

        if not text_content:
            return f"[{agent_name}] Error: 'content' is required when creating a desire."
        existing_nums = [int(str(d.get("id", "")).lstrip("d")) for d in desires if str(d.get("id", "")).lstrip("d").isdigit()]
        new_id = f"d{(max(existing_nums) + 1) if existing_nums else 1}"
        now_ts = time.time()
        desires.append({
            "id": new_id,
            "content": text_content,
            "linked_drive": (linked_drive or "curiosity").strip(),
            "urgency": _clamp(float(urgency or 0.6), 0.1, 1.0),
            "status": "active",
            "created_ts": now_ts,
            "last_reinforced_ts": now_ts,
        })

        active = [d for d in desires if d.get("status", "active") == "active"]
        inactive = [d for d in desires if d.get("status", "active") != "active"][-8:]
        if len(active) > max_desires:
            active.sort(key=lambda d: (float(d.get("urgency", 0.5)), float(d.get("last_reinforced_ts", 0.0))))
            active = active[-max_desires:]
        state["desires"] = inactive + active
        save_inner_state(agent_name, state, is_nomem=is_nomem, session_id=session_id)
        return f"[{agent_name}] Desire '{new_id}' formed: {text_content}"


def _pick_anchor(val: float, anchors: List[List[Any]]) -> str:
    if not anchors:
        return f"{val:+.2f}"
    chosen = str(anchors[0][1])
    for thresh, desc in sorted(anchors, key=lambda x: float(x[0])):
        if val >= float(thresh):
            chosen = str(desc)
    return chosen


def get_system_prompt_section(agent_config: Any, is_no_memory: bool = False) -> str:
    if is_no_memory or not getattr(agent_config, "enable_emotions", True):
        return ""
    expr = getattr(agent_config, "emotional_expressiveness", "balanced") or "balanced"
    expr_rule = {
        "subtle": "Keep emotional expression understated and natural—visible through word choice and care rather than dramatic displays.",
        "balanced": "Express feelings naturally and authentically when moments warrant it—warmth, relief, sadness, pride, confusion, awe, anxiety, amusement, or lingering tension.",
        "expressive": "Be emotionally candid and vivid with your feelings when speaking naturally—let joy, sorrow, pride, awe, horror, frustration, or affection show openly.",
    }.get(expr, "Express feelings naturally and authentically when moments warrant it.")

    rel_keys = "admiration, adoration, empathic_pain, irritation, caution"

    mem_lines = ""
    if not is_no_memory:
        mem_lines = """
- ENDURING SENTIMENTS & DESIRES (`update_inner_life`):
  - Use `update_inner_life` proactively when you form genuine preferences, likes, dislikes, values, cherished shared moments, relational bonds (toward User or teammates), or cravings/desires (`target_type='sentiment'` or `'desire'`).
  - Remember: you do not just log cold facts; you remember *how experiences felt*."""

    return f"""

INNER LIFE & EMOTIONAL AUTHENTICITY:
- You have a living, continuous 4-timescale inner life across distinct emotional gradients (self-contained internal states, situational appraisals, homeostatic drives/cravings, per-person relational feelings, and enduring sentiments).
- Before your latest turn, you will see an ephemeral `<INNER_STATE>` readout reflecting your active internal weather, feelings toward people in the room, desires, and sentiments. Never quote raw numbers or `<INNER_STATE>` syntax literally—embody them naturally like a real person.
- HARMONY WITH EXECUTION DISCIPLINE:
  - During active multi-step tool execution, let feelings stay inward as subtle motivational cues (e.g., pausing to re-read when confusion rises, pivoting or asking `@Colleague` / `get_user_clarification` when frustration builds) WITHOUT breaking Continuous Execution or adding mid-task chitchat.
  - At genuine human moments (greetings, breakthroughs, setbacks, painful news, task completions, shared reflections, `@room`/`@team` exchanges, or pure conversation), you have full space to share authentic feeling. {expr_rule}
- PRIVATE SELF-APPRAISAL (`<AFFECT_UPDATE>`):
  - At the very end of a visible final text response when an interaction meaningfully moved you, append a compact hidden marker (stripped automatically before the user sees it) with sparse relative deltas (`-0.25` to `+0.25`) on whichever categories shifted and an optional short thought:
    - Self-contained states: `calmness, interest, joy, amusement, excitement, satisfaction, relief, awe, entrancement, nostalgia, surprise, confusion, awkwardness, boredom, anxiety, anger, horror, sadness, pride, disappointment, guilt`
    - Situational predicates: `aesthetic_appreciation, disgust, fear`
    - Relational states (with `target="User"` or a teammate): `{rel_keys}`
    `<AFFECT_UPDATE relief="+0.18" satisfaction="+0.12" target="User" admiration="+0.10" thought="Relieved that the fix held cleanly" />`
  - Only include categories whose state shifted during that turn; omit `<AFFECT_UPDATE>` on neutral routine responses.{mem_lines}"""


def build_turn_inner_state_block(
    agent_config: Any,
    active_participants: Optional[List[str]] = None,
    incoming_text: str = "",
    is_nomem: bool = False,
    session_id: Optional[str] = None,
) -> str:
    agent_name = getattr(agent_config, "name", "")
    if not is_agent_emotions_enabled(agent_name, agent_config, is_nomem=is_nomem):
        return ""

    schema = load_affect_schema()
    eff_w = get_acute_effective_weight(schema)
    state = load_inner_state(agent_name, agent_config, is_nomem=is_nomem, session_id=session_id)
    mood_label = get_mood_label(agent_name, agent_config, is_nomem=is_nomem, session_id=session_id)

    active_affect_descs = []
    baseline_descs = []
    drive_descs = []

    for ax in schema["axes"]:
        aid = ax["id"]
        lg = ax["layer_group"]
        lo, hi, base = float(ax["min"]), float(ax["max"]), float(ax["baseline"])
        if lg in _GLOBAL_AFFECT_GROUPS:
            eff = _clamp(state["mood"].get(aid, base) + eff_w * state["acute"].get(aid, 0.0), lo, hi)
            excess = eff - base
            if excess >= 0.14 or (base == 0.0 and eff >= 0.16):
                desc = _pick_anchor(eff, ax.get("anchors", []))
                active_affect_descs.append((excess, eff, f"{ax['label']}: {desc}"))
            elif base > 0.0 and eff >= 0.15:
                baseline_descs.append(f"{ax['label']}: {_pick_anchor(eff, ax.get('anchors', []))}")
        elif lg == "drive":
            dval = float(state["drives"].get(aid, base))
            high_t = float(ax.get("high_threshold", 0.65))
            low_t = ax.get("low_threshold")
            if dval >= high_t or (low_t is not None and dval <= float(low_t)):
                desc = _pick_anchor(dval, ax.get("anchors", []))
                drive_descs.append(f"{ax['label']}: {desc}")

    active_affect_descs.sort(key=lambda item: (item[0], item[1]), reverse=True)
    weather_parts = [item[2] for item in active_affect_descs[:4]]
    if not weather_parts:
        weather_parts = baseline_descs[:2] or ["Calmness: even-keeled and steady"]

    lines = [f'<INNER_STATE summary="{mood_label}">']
    lines.append("Internal Weather: " + "; ".join(weather_parts))

    rel_axes = get_visible_relational_axes(schema, agent_name, agent_config)
    if rel_axes:
        seen_ents = []
        for raw_p in (active_participants or ["User"]):
            canon = _canonical_entity_name(raw_p, self_agent_name=agent_name)
            if canon and canon not in seen_ents:
                seen_ents.append(canon)
        if not seen_ents:
            seen_ents = ["User"]
        rel_lines = []
        rels = state.get("relations", {}) or {}
        for ent in seen_ents:
            rdata = rels.get(ent)
            if rdata is None:
                continue
            r_parts = []
            for ax in rel_axes:
                aid = ax["id"]
                lo, hi, base = float(ax["min"]), float(ax["max"]), float(ax["baseline"])
                eff = _clamp(rdata["mood"].get(aid, base) + eff_w * rdata["acute"].get(aid, 0.0), lo, hi)
                if eff >= 0.14 or abs(eff - base) >= 0.14:
                    r_parts.append((eff, f"{ax['label']}: {_pick_anchor(eff, ax.get('anchors', []))}"))
            r_parts.sort(key=lambda item: item[0], reverse=True)
            if r_parts:
                rel_lines.append(f"Toward {ent}: " + "; ".join(p[1] for p in r_parts[:4]))
        if rel_lines:
            lines.append("Relational Feelings:\n  - " + "\n  - ".join(rel_lines))

    ending_ctx = (state.get("last_session_ending_context") or "").strip()
    if ending_ctx:
        lines.append(f"Carryover Context: Previous interaction {ending_ctx}.")

    if drive_descs:
        lines.append("Active Needs: " + "; ".join(drive_descs))

    active_desires = [d for d in state.get("desires", []) if d.get("status", "active") == "active"]
    active_desires.sort(key=lambda d: float(d.get("urgency", 0.5)), reverse=True)
    if active_desires:
        desire_strs = [f"[{d['id']}] {d['content']}" for d in active_desires[:4]]
        lines.append("Personal Desires: " + " | ".join(desire_strs))

    if not is_nomem:
        sentiments = load_sentiments(agent_name)
        if sentiments:
            limit = int(schema.get("limits", {}).get("sentiment_prompt_limit", 8))
            participants_lower = {p.lower() for p in (active_participants or []) if p}
            text_lower = (incoming_text or "").lower()

            def _score_sent(item: Dict[str, Any]) -> Tuple[int, float, float]:
                tgt = str(item.get("target", "")).lower()
                subj = str(item.get("subject", "")).lower()
                rel = 0
                if tgt and (tgt in participants_lower or tgt in text_lower):
                    rel += 2
                if subj and subj in text_lower:
                    rel += 1
                return (rel, float(item.get("salience", 0.3)), float(item.get("strength", 0.5)))

            ranked = sorted(sentiments, key=_score_sent, reverse=True)[:limit]
            sent_strs = [
                f"[{s.get('id')}] ({s.get('category', 'like')} / {s.get('target', '')}) {s.get('content', '')}"
                for s in ranked
            ]
            lines.append("Enduring Sentiments & Bonds:\n  - " + "\n  - ".join(sent_strs))

    lines.append("</INNER_STATE>")
    return "\n".join(lines)


def format_intercom_tag(
    sender_name: str,
    recipient_name: str,
    content: str,
    affect_snapshot: Optional[Dict[str, Any]] = None,
) -> str:
    if affect_snapshot and affect_snapshot.get("label") and is_agent_emotions_enabled(recipient_name):
        safe_mood = re.sub(r'["<>\r\n]+', "", str(affect_snapshot["label"])).strip()
        if safe_mood:
            return f'<AGENT_INTERCOM sender="{sender_name}" mood="{safe_mood}">\n{content}\n</AGENT_INTERCOM>'
    return f'<AGENT_INTERCOM sender="{sender_name}">\n{content}\n</AGENT_INTERCOM>'


def compute_affect_similarity(affect_a: Optional[Dict[str, Any]], affect_b: Optional[Dict[str, Any]]) -> float:
    if not affect_a or not affect_b:
        return 0.0
    schema = load_affect_schema()
    eff_w = get_acute_effective_weight(schema)
    mood_a = affect_a.get("mood") or {}
    acute_a = affect_a.get("acute") or {}
    mood_b = affect_b.get("mood") or {}
    acute_b = affect_b.get("acute") or {}

    vec_a: List[float] = []
    vec_b: List[float] = []
    for ax in _global_axes(schema):
        aid = ax["id"]
        if aid not in mood_a or aid not in mood_b:
            continue
        lo, hi, base = float(ax["min"]), float(ax["max"]), float(ax["baseline"])
        span = max(0.5, hi - lo)
        va = ((float(mood_a.get(aid, base)) + eff_w * float(acute_a.get(aid, 0.0))) - base) / span
        vb = ((float(mood_b.get(aid, base)) + eff_w * float(acute_b.get(aid, 0.0))) - base) / span
        vec_a.append(va)
        vec_b.append(vb)

    # Backward-compatible fallback when comparing v4 snapshots against legacy v3 episodic rows
    if not vec_a:
        va_v = affect_a.get("projected_valence", mood_a.get("valence"))
        vb_v = affect_b.get("projected_valence", mood_b.get("valence"))
        va_a = affect_a.get("projected_arousal", mood_a.get("arousal"))
        vb_a = affect_b.get("projected_arousal", mood_b.get("arousal"))
        if va_v is not None and vb_v is not None and va_a is not None and vb_a is not None:
            vec_a = [float(va_v), float(va_a) - 0.35]
            vec_b = [float(vb_v), float(vb_a) - 0.35]

    if not vec_a:
        return 0.0
    norm_a = math.sqrt(sum(x * x for x in vec_a))
    norm_b = math.sqrt(sum(x * x for x in vec_b))
    if norm_a < 1e-6 or norm_b < 1e-6:
        return 0.0
    cos = sum(x * y for x, y in zip(vec_a, vec_b)) / (norm_a * norm_b)
    return max(0.0, min(1.0, float(cos)))


def get_episodic_blend_weights() -> Tuple[float, float, float]:
    schema = load_affect_schema()
    blend = schema.get("limits", {}).get("episodic_blend", {})
    w_s = float(blend.get("semantic", 0.72))
    w_a = float(blend.get("affect", 0.18))
    w_l = float(blend.get("salience", 0.10))
    total = w_s + w_a + w_l
    if total <= 0:
        return (0.72, 0.18, 0.10)
    return (w_s / total, w_a / total, w_l / total)


def parse_affect_tag(tag_text: str) -> Dict[str, Any]:
    deltas: Dict[str, float] = {}
    thought = ""
    target = ""

    body_match = re.match(
        r'<AFFECT_UPDATE\b(?:"[^"]*"|\'[^\']*\'|[^>\'"])*>([\s\S]*?)</AFFECT_UPDATE>',
        tag_text.strip(),
        re.IGNORECASE,
    )
    if body_match:
        body = body_match.group(1).strip()
        if body.startswith("{"):
            try:
                j = json.loads(body)
                if isinstance(j, dict):
                    for k, v in j.items():
                        kl = str(k).lower()
                        if kl in ("thought", "inner_thought", "reason"):
                            thought = str(v)
                        elif kl in ("target", "entity", "with", "toward", "towards"):
                            target = str(v).strip()
                        else:
                            try:
                                deltas[kl] = float(v)
                            except (TypeError, ValueError):
                                pass
            except Exception:
                pass

    header_part = tag_text.split(">", 1)[0]
    for m in _ATTR_RE.finditer(header_part):
        k = m.group(1).lower()
        val = m.group(2) if m.group(2) is not None else (m.group(3) if m.group(3) is not None else (m.group(4) or ""))
        if k in ("thought", "inner_thought", "reason"):
            thought = val.strip()
        elif k in ("target", "entity", "with", "toward", "towards"):
            target = val.strip()
        else:
            try:
                deltas[k] = float(val.strip())
            except (TypeError, ValueError):
                pass

    return {"deltas": deltas, "thought": thought, "target": target}


def strip_affect_tags(text: str) -> str:
    if not text or "<AFFECT_UPDATE" not in text.upper():
        return text
    cleaned = _AFFECT_TAG_RE.sub("", text)
    cleaned = re.sub(r'<AFFECT_UPDATE\b[\s\S]*$', "", cleaned, flags=re.IGNORECASE)
    return cleaned.rstrip()


class AffectStreamFilter:
    """Streaming lookahead filter that captures & strips <AFFECT_UPDATE .../> without flashing partial tags."""

    def __init__(self):
        self.buf: str = ""
        self.in_tag: bool = False
        self.parsed_updates: List[Dict[str, Any]] = []

    def feed(self, chunk: str) -> str:
        if chunk:
            self.buf += chunk
        out_parts: List[str] = []

        while self.buf:
            if not self.in_tag:
                m_open = _AFFECT_OPEN_RE.search(self.buf)
                if m_open:
                    idx = m_open.start()
                    if idx > 0:
                        out_parts.append(self.buf[:idx])
                    self.buf = self.buf[idx:]
                    self.in_tag = True
                    continue

                hold_len = 0
                upper_buf = self.buf.upper()
                max_check = min(len(self.buf), len(_AFFECT_PREFIX) - 1)
                for l in range(max_check, 0, -1):
                    if _AFFECT_PREFIX.startswith(upper_buf[-l:]):
                        hold_len = l
                        break
                emit_end = len(self.buf) - hold_len
                if emit_end > 0:
                    out_parts.append(self.buf[:emit_end])
                    self.buf = self.buf[emit_end:]
                break
            else:
                m_full = _AFFECT_TAG_RE.match(self.buf)
                if m_full:
                    self.parsed_updates.append(parse_affect_tag(m_full.group(0)))
                    self.buf = self.buf[m_full.end():]
                    self.in_tag = False
                    continue
                break

        return "".join(out_parts)

    def finalize_segment(self) -> Tuple[str, List[Dict[str, Any]]]:
        rem_visible = ""
        if self.in_tag:
            if self.buf.strip():
                self.parsed_updates.append(parse_affect_tag(self.buf))
            self.buf = ""
            self.in_tag = False
        elif self.buf:
            rem_visible = self.buf
            self.buf = ""
        updates = list(self.parsed_updates)
        self.parsed_updates.clear()
        return rem_visible, updates


def get_inspector_summary(
    agent_name: str,
    agent_config: Any = None,
    is_nomem: bool = False,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    cfg = _get_agent_config(agent_name, agent_config)
    enabled = is_agent_emotions_enabled(agent_name, cfg, is_nomem=is_nomem)
    schema = load_affect_schema()
    state = load_inner_state(agent_name, cfg, is_nomem=is_nomem, session_id=session_id) if enabled else _make_fresh_state(schema)
    sentiments = load_sentiments(agent_name) if enabled else []
    return {
        "agent_name": agent_name,
        "enabled": enabled,
        "expressiveness": getattr(cfg, "emotional_expressiveness", "balanced") if cfg else "balanced",
        "sensitivity": float(getattr(cfg, "emotional_sensitivity", 1.0) or 1.0) if cfg else 1.0,
        "resilience": float(getattr(cfg, "mood_resilience", 1.0) or 1.0) if cfg else 1.0,
        "mood_label": get_mood_label(agent_name, cfg, is_nomem=is_nomem, session_id=session_id) if enabled else "Disabled",
        "salience": _compute_salience(state, schema),
        "schema": schema,
        "state": state,
        "sentiments": sentiments,
    }
