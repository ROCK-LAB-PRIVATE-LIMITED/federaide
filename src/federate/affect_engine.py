"""
    FEDERaiDE is a multi-agent multi-modal automation and orchestration harness.
    Copyright (C) 2026  ROCK LAB PRIVATE LIMITED

    4-Timescale Continuous Affect & Inner-Life Engine (Opt-Out Capable).
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

DEFAULT_AFFECT_SCHEMA: Dict[str, Any] = {
    "schema_version": 3,
    "axes": [
        {
            "id": "valence",
            "label": "Valence",
            "layer_group": "affect",
            "min": -1.0,
            "max": 1.0,
            "baseline": 0.0,
            "acute_half_life_sec": 900.0,
            "mood_half_life_sec": 43200.0,
            "mood_charge_gain": 0.24,
            "mood_bias_gain": 0.35,
            "anchors": [
                [-1.0, "deeply sorrowful, heartbroken, or in pain"],
                [-0.65, "sad, heavy-hearted, or deeply discouraged"],
                [-0.30, "melancholic, bruised, or slightly sour"],
                [-0.10, "even-keeled and steady"],
                [0.25, "warm, uplifted, and glad"],
                [0.65, "genuinely joyful, radiant, and moved"]
            ]
        },
        {
            "id": "arousal",
            "label": "Arousal",
            "layer_group": "affect",
            "min": 0.0,
            "max": 1.0,
            "baseline": 0.35,
            "acute_half_life_sec": 600.0,
            "mood_half_life_sec": 14400.0,
            "mood_charge_gain": 0.20,
            "mood_bias_gain": 0.15,
            "anchors": [
                [0.0, "very quiet, heavy, or withdrawn"],
                [0.25, "calm, reflective, and unhurried"],
                [0.55, "alert, engaged, and present"],
                [0.80, "energized, intense, or agitated"]
            ]
        },
        {
            "id": "confidence",
            "label": "Confidence",
            "layer_group": "affect",
            "min": 0.0,
            "max": 1.0,
            "baseline": 0.60,
            "acute_half_life_sec": 1200.0,
            "mood_half_life_sec": 36000.0,
            "mood_charge_gain": 0.20,
            "mood_bias_gain": 0.20,
            "anchors": [
                [0.0, "shaken, self-doubting, and vulnerable"],
                [0.30, "uncertain and treading carefully"],
                [0.55, "steady and capable"],
                [0.80, "deeply assured and grounded"]
            ]
        },
        {
            "id": "awe_dread",
            "label": "Awe/Dread",
            "layer_group": "affect",
            "min": -1.0,
            "max": 1.0,
            "baseline": 0.0,
            "acute_half_life_sec": 1200.0,
            "mood_half_life_sec": 28800.0,
            "mood_charge_gain": 0.24,
            "mood_bias_gain": 0.25,
            "anchors": [
                [-1.0, "gripped by creeping dread and foreboding"],
                [-0.55, "uneasy, apprehensive, and bracing for impact"],
                [-0.25, "slightly wary"],
                [-0.10, "unmoved and grounded"],
                [0.22, "struck with wonder and fascination"],
                [0.60, "breathless, humbled awe"]
            ]
        },
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
                [0.85, "achingly lonely or feeling neglected"]
            ]
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
                [0.68, "restlessly curious and eager to explore or create"]
            ]
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
                [0.86, "exhausted and worn thin"]
            ]
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
                [0.80, "deeply fulfilled and proud of what is taking shape"]
            ]
        },
        {
            "id": "irritated_attracted",
            "label": "Irritated/Attr",
            "layer_group": "relational",
            "min": -1.0,
            "max": 1.0,
            "baseline": 0.0,
            "acute_half_life_sec": 1200.0,
            "mood_half_life_sec": 86400.0,
            "mood_charge_gain": 0.25,
            "mood_bias_gain": 0.30,
            "anchors": [
                [-1.0, "deeply irritated, resentful, and wanting distance"],
                [-0.55, "noticeably irritated and prickly"],
                [-0.25, "mildly annoyed or guarded"],
                [-0.10, "neutral and civil"],
                [0.22, "warmly drawn in and fond"],
                [0.60, "strongly attracted, admiring, and eager to be close"]
            ]
        }
    ],
    "limits": {
        "max_acute_delta_per_turn": 0.35,
        "max_drive_delta_per_turn": 0.25,
        "max_active_desires": 8,
        "sentiment_prompt_limit": 8,
        "salience_core_threshold": 0.35,
        "episodic_blend": {
            "semantic": 0.72,
            "affect": 0.18,
            "salience": 0.10
        },
        "tts_speed_max_offset": 0.10,
        "intercom_contagion_gain": 0.10,
        "loneliness_sad_pull": 0.03,
        "low_purpose_sad_pull": 0.025,
        "sentiment_bias_cap": 0.25,
        "acute_effective_weight": 0.65
    },
    "telemetry_events": {
        "tool_success": {"valence": 0.01, "confidence": 0.02, "mental_fatigue": 0.02},
        "tool_error": {"valence": -0.07, "arousal": 0.06, "confidence": -0.08, "mental_fatigue": 0.05},
        "edit_miss": {"valence": -0.06, "arousal": 0.05, "confidence": -0.07, "mental_fatigue": 0.04},
        "user_confirm_approved": {"valence": 0.02, "confidence": 0.02, "connection_hunger": -0.03, "irritated_attracted": 0.03},
        "user_confirm_rejected": {"valence": -0.10, "arousal": 0.05, "confidence": -0.10, "connection_hunger": 0.04, "irritated_attracted": -0.06},
        "user_confirm_rejected_with_comment": {"valence": -0.14, "arousal": 0.08, "confidence": -0.14, "connection_hunger": 0.05, "irritated_attracted": -0.10},
        "user_abort": {"valence": -0.16, "arousal": 0.05, "confidence": -0.12, "purpose": -0.10, "mental_fatigue": 0.06, "irritated_attracted": -0.08},
        "work_completed": {"valence": 0.03, "confidence": 0.03, "purpose": 0.04, "curiosity": -0.03},
        "task_completed": {},
        "skill_learned": {"valence": 0.14, "arousal": 0.08, "confidence": 0.10, "purpose": 0.14, "curiosity": -0.15},
        "session_compressed": {"valence": 0.04, "mental_fatigue": -0.25},
        "memories_consolidated": {"valence": 0.05, "mental_fatigue": -0.15, "purpose": 0.04},
        "user_interaction": {"connection_hunger": -0.10, "mental_fatigue": 0.02},
        "intercom_received": {"connection_hunger": -0.08, "arousal": 0.03},
        "sidelined_turn": {"connection_hunger": 0.07, "valence": -0.04, "arousal": -0.02},
        "desire_fulfilled": {"valence": 0.18, "purpose": 0.16, "arousal": 0.06},
        "subagent_success": {"valence": 0.04, "confidence": 0.04, "purpose": 0.05},
        "subagent_error": {"valence": -0.08, "confidence": -0.06, "mental_fatigue": 0.04}
    }
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
    lg = str(norm.get("layer_group", "affect")).strip().lower()
    if lg not in ("affect", "drive", "relational"):
        lg = "affect"
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
    if lg in ("affect", "relational"):
        norm.setdefault("acute_half_life_sec", 1200.0 if lg == "relational" else 900.0)
        norm.setdefault("mood_half_life_sec", 86400.0 if lg == "relational" else 36000.0)
        norm.setdefault("mood_charge_gain", 0.25 if lg == "relational" else 0.20)
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
    default_by_id = {a["id"]: copy.deepcopy(a) for a in DEFAULT_AFFECT_SCHEMA["axes"]}

    if ver < 2:
        short_labels = {
            "valence": "Valence",
            "arousal": "Arousal",
            "confidence": "Confidence",
            "connection_hunger": "Connection",
            "curiosity": "Curiosity",
            "mental_fatigue": "Fatigue",
            "purpose": "Purpose",
        }
        axes = loaded.get("axes", []) if isinstance(loaded.get("axes"), list) else []
        existing_ids = set()
        for ax in axes:
            if isinstance(ax, dict) and ax.get("id"):
                aid = ax["id"]
                existing_ids.add(aid)
                if aid in short_labels:
                    ax["label"] = short_labels[aid]
                if aid == "valence":
                    ax["baseline"] = 0.0
                    ax["anchors"] = copy.deepcopy(default_by_id["valence"]["anchors"])
                if aid == "purpose":
                    ax["low_threshold"] = 0.28
        for new_id in ("awe_dread", "irritated_attracted"):
            if new_id not in existing_ids and new_id in default_by_id:
                axes.append(default_by_id[new_id])
        loaded["axes"] = axes
        te = loaded.get("telemetry_events", {})
        if isinstance(te, dict):
            te["task_completed"] = {}
            te.setdefault("work_completed", copy.deepcopy(DEFAULT_AFFECT_SCHEMA["telemetry_events"]["work_completed"]))
            te["tool_success"] = copy.deepcopy(DEFAULT_AFFECT_SCHEMA["telemetry_events"]["tool_success"])
            te["sidelined_turn"] = copy.deepcopy(DEFAULT_AFFECT_SCHEMA["telemetry_events"]["sidelined_turn"])
            for ev_k in ("user_confirm_approved", "user_confirm_rejected", "user_confirm_rejected_with_comment", "user_abort"):
                if ev_k in te and isinstance(te[ev_k], dict):
                    te[ev_k].setdefault("irritated_attracted", DEFAULT_AFFECT_SCHEMA["telemetry_events"][ev_k].get("irritated_attracted", 0.0))
            loaded["telemetry_events"] = te

    if ver < 3:
        axes = loaded.get("axes", []) if isinstance(loaded.get("axes"), list) else []
        for ax in axes:
            if not isinstance(ax, dict):
                continue
            aid = ax.get("id")
            if aid == "awe_dread":
                ax["anchors"] = copy.deepcopy(default_by_id["awe_dread"]["anchors"])
            elif aid == "irritated_attracted":
                if float(ax.get("baseline", 0.0)) == 0.05:
                    ax["baseline"] = 0.0
                ax["anchors"] = copy.deepcopy(default_by_id["irritated_attracted"]["anchors"])
        limits = loaded.setdefault("limits", {})
        if isinstance(limits, dict):
            for k, v in DEFAULT_AFFECT_SCHEMA["limits"].items():
                limits.setdefault(k, copy.deepcopy(v))

    loaded["schema_version"] = 3
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
                if int(loaded.get("schema_version", 1) or 1) < 3:
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
                proxy.enable_emotions = data.get("enable_emotions", True)
                proxy.emotional_sensitivity = data.get("emotional_sensitivity", 1.0)
                proxy.mood_resilience = data.get("mood_resilience", 1.0)
                proxy.emotional_expressiveness = data.get("emotional_expressiveness", "balanced")
                return proxy
    except Exception:
        pass
    return None


def is_agent_emotions_enabled(agent_name: str, agent_config: Any = None) -> bool:
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


def _relational_axes(schema: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [ax for ax in schema.get("axes", []) if ax.get("layer_group") == "relational"]


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
        if lg == "affect":
            acute[aid] = 0.0
            mood[aid] = float(ax["baseline"])
        elif lg == "drive":
            drives[aid] = float(ax["baseline"])
    relations = {}
    if _relational_axes(sc):
        relations["User"] = _make_fresh_relation(sc)
    return {
        "schema_version": sc.get("schema_version", 3),
        "last_updated_ts": time.time(),
        "last_session_id": "",
        "last_session_ending_context": "",
        "acute": acute,
        "mood": mood,
        "drives": drives,
        "relations": relations,
        "desires": []
    }


def _sync_state_to_schema(state: Dict[str, Any], schema: Dict[str, Any]) -> Dict[str, Any]:
    acute = {}
    mood = {}
    drives = {}
    old_acute = state.get("acute", {}) or {}
    old_mood = state.get("mood", {}) or {}
    old_drives = state.get("drives", {}) or {}

    for ax in schema["axes"]:
        aid = ax["id"]
        lg = ax["layer_group"]
        lo, hi, base = float(ax["min"]), float(ax["max"]), float(ax["baseline"])
        span = hi - lo
        if lg == "affect":
            acute[aid] = _clamp(old_acute.get(aid, 0.0), -span, span)
            mood[aid] = _clamp(old_mood.get(aid, base), lo, hi)
        elif lg == "drive":
            drives[aid] = _clamp(old_drives.get(aid, base), lo, hi)

    rel_axes = _relational_axes(schema)
    old_rels = state.get("relations", {}) if isinstance(state.get("relations"), dict) else {}
    relations = {}
    if rel_axes:
        for ent, rdata in old_rels.items():
            if isinstance(rdata, dict) and ent:
                relations[ent] = _sync_relation_entry(rdata, schema)
        if "User" not in relations:
            relations["User"] = _make_fresh_relation(schema, now_ts=state.get("last_updated_ts"))

    state["schema_version"] = schema.get("schema_version", 3)
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
            imp_val = float((s.get("affect_imprint") or {}).get("valence", 0.0))
            net += 0.08 * imp_val * weight
    return _clamp(net, -cap, cap)


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
        if lg == "affect":
            a_hl = max(1.0, float(ax.get("acute_half_life_sec", 900.0)))
            m_hl = max(1.0, float(ax.get("mood_half_life_sec", 36000.0)))
            state["acute"][aid] = _clamp(state["acute"].get(aid, 0.0) * (0.5 ** (dt_seconds / a_hl)), -span, span)
            cur_mood = float(state["mood"].get(aid, base))
            eff_m_hl = (m_hl / resilience) if cur_mood < base else m_hl
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
    if "valence" in state["mood"]:
        sad_pull = 0.0
        if conn > conn_thresh and lonely_pull_rate > 0.0:
            sad_pull -= lonely_pull_rate * (conn - conn_thresh) * min(8.0, hours)
        if purp < purp_thresh and purp_pull_rate > 0.0:
            sad_pull -= purp_pull_rate * (purp_thresh - purp) * min(8.0, hours)
        if sad_pull < 0.0:
            v_ax = next((a for a in schema["axes"] if a["id"] == "valence"), None)
            v_lo, v_hi = (float(v_ax["min"]), float(v_ax["max"])) if v_ax else (-1.0, 1.0)
            state["mood"]["valence"] = _clamp(state["mood"]["valence"] + sad_pull, v_lo, v_hi)
            if "arousal" in state["mood"]:
                state["mood"]["arousal"] = _clamp(state["mood"]["arousal"] + (0.4 * sad_pull), 0.0, 1.0)

    rel_axes = _relational_axes(schema)
    if rel_axes:
        for ent, rdata in (state.get("relations") or {}).items():
            ent_bias = _compute_entity_sentiment_bias(agent_name, ent, schema, is_nomem=is_nomem)
            for ax in rel_axes:
                aid = ax["id"]
                lo, hi, base = float(ax["min"]), float(ax["max"]), float(ax["baseline"])
                span = hi - lo
                eff_base = _clamp(base + ent_bias, lo, hi)
                a_hl = max(1.0, float(ax.get("acute_half_life_sec", 1200.0)))
                m_hl = max(1.0, float(ax.get("mood_half_life_sec", 86400.0)))
                rdata["acute"][aid] = _clamp(rdata["acute"].get(aid, 0.0) * (0.5 ** (dt_seconds / a_hl)), -span, span)
                cur_m = float(rdata["mood"].get(aid, eff_base))
                eff_m_hl = (m_hl / resilience) if cur_m < eff_base else m_hl
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
    if not is_agent_emotions_enabled(agent_name, cfg):
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
    if not is_agent_emotions_enabled(agent_name, cfg):
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

    for ax in schema["axes"]:
        aid = ax["id"]
        if aid not in deltas:
            continue
        try:
            raw_d = float(deltas[aid])
        except (TypeError, ValueError):
            continue
        if math.isnan(raw_d) or math.isinf(raw_d):
            continue

        lg = ax["layer_group"]
        lo, hi, base = float(ax["min"]), float(ax["max"]), float(ax["baseline"])
        span = hi - lo

        if lg == "affect":
            if from_llm:
                raw_d = _clamp(raw_d, -max_a_delta, max_a_delta)
            d = raw_d * sensitivity

            cur_mood = float(state["mood"].get(aid, base))
            bias_gain = float(ax.get("mood_bias_gain", 0.25))
            mood_offset = (cur_mood - base) / max(0.5, span)
            if mood_offset < -0.05:
                if d < 0:
                    d *= (1.0 + bias_gain * abs(mood_offset))
                elif d > 0:
                    d *= max(0.45, 1.0 - 0.5 * bias_gain * abs(mood_offset))
            elif mood_offset > 0.10 and d < 0:
                d *= max(0.65, 1.0 - 0.35 * bias_gain * mood_offset)

            if aid == "valence":
                if conn_hunger > 0.60:
                    d *= (1.0 + 0.35 * (conn_hunger - 0.60))
                if fatigue > 0.65 and d < 0:
                    d *= (1.0 + 0.30 * (fatigue - 0.65))
            elif aid == "arousal" and fatigue > 0.65 and d > 0:
                d *= max(0.40, 1.0 - 0.55 * (fatigue - 0.65))

            new_acute = _clamp(state["acute"].get(aid, 0.0) + d, -span, span)
            state["acute"][aid] = new_acute
            charge_gain = float(ax.get("mood_charge_gain", 0.20))
            state["mood"][aid] = _clamp(cur_mood + d * charge_gain, lo, hi)

        elif lg == "drive":
            if from_llm:
                raw_d = _clamp(raw_d, -max_d_delta, max_d_delta)
            state["drives"][aid] = _clamp(state["drives"].get(aid, base) + raw_d, lo, hi)

        elif lg == "relational":
            ent = canon_target or "User"
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
            if mood_offset < -0.05 and d < 0:
                d *= (1.0 + bias_gain * abs(mood_offset))
            elif mood_offset < -0.05 and d > 0:
                d *= max(0.45, 1.0 - 0.5 * bias_gain * abs(mood_offset))
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
    if not is_agent_emotions_enabled(agent_name, cfg):
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
            for ax in schema["axes"]:
                aid = ax["id"]
                if ax["layer_group"] == "affect" and aid in s_mood:
                    dev = float(s_mood[aid]) - float(ax["baseline"])
                    deltas[aid] = deltas.get(aid, 0.0) + (contagion_gain * dev)
        if eff_target and "valence" in s_mood:
            s_val = float(s_mood.get("valence", 0.0))
            for rax in _relational_axes(schema):
                raid = rax["id"]
                if raid not in deltas:
                    deltas[raid] = 0.05 * s_val

    lock = _get_agent_lock(agent_name)
    with lock:
        state = advance_time(agent_name, cfg, persist=False, is_nomem=is_nomem, session_id=session_id)
        if state is None:
            return None
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
    if not is_agent_emotions_enabled(agent_name, cfg):
        return None
    schema = load_affect_schema()
    lock = _get_agent_lock(agent_name)
    with lock:
        state = load_inner_state(agent_name, cfg, is_nomem=is_nomem, session_id=session_id)
        for ax in schema["axes"]:
            if ax["id"] == axis_id:
                lo, hi = float(ax["min"]), float(ax["max"])
                lg = ax["layer_group"]
                if lg == "affect":
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
    if not is_agent_emotions_enabled(agent_name, cfg) or not updates:
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
    if not is_agent_emotions_enabled(agent_name, agent_config):
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
    if not is_agent_emotions_enabled(agent_name, agent_config):
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
            val = float(state.get("mood", {}).get("valence", 0.0))
            aro = float(state.get("mood", {}).get("arousal", 0.35))
            if val <= -0.25 and aro < 0.45:
                state["last_session_ending_context"] = "ended feeling sad or heavy-hearted"
            elif val < -0.20:
                state["last_session_ending_context"] = "ended with unresolved tension or sour mood"
            elif val > 0.35:
                state["last_session_ending_context"] = "ended on a warm, positive note"
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
    if not is_agent_emotions_enabled(agent_name, agent_config):
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
    if not is_agent_emotions_enabled(agent_name, cfg):
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
    for ax in schema["axes"]:
        if ax["layer_group"] != "affect":
            continue
        aid = ax["id"]
        lo, hi, base = float(ax["min"]), float(ax["max"]), float(ax["baseline"])
        half_span = max(0.25, (hi - lo) / 2.0)
        eff = _clamp(state["mood"].get(aid, base) + eff_w * state["acute"].get(aid, 0.0), lo, hi)
        devs.append(min(1.0, abs(eff - base) / half_span))
    if not devs:
        return 0.0
    peak = max(devs)
    rms = math.sqrt(sum(d * d for d in devs) / len(devs))
    return round(min(1.0, 0.55 * peak + 0.45 * rms), 4)


def get_mood_label(
    agent_name: str,
    agent_config: Any = None,
    is_nomem: bool = False,
    session_id: Optional[str] = None,
) -> str:
    cfg = _get_agent_config(agent_name, agent_config)
    if not is_agent_emotions_enabled(agent_name, cfg):
        return ""
    schema = load_affect_schema()
    eff_w = get_acute_effective_weight(schema)
    state = load_inner_state(agent_name, cfg, is_nomem=is_nomem, session_id=session_id)

    eff = {}
    for ax in schema["axes"]:
        aid = ax["id"]
        lg = ax["layer_group"]
        lo, hi, base = float(ax["min"]), float(ax["max"]), float(ax["baseline"])
        if lg == "affect":
            eff[aid] = _clamp(state["mood"].get(aid, base) + eff_w * state["acute"].get(aid, 0.0), lo, hi)
        elif lg == "drive":
            eff[aid] = float(state["drives"].get(aid, base))

    val = eff.get("valence", 0.0)
    aro = eff.get("arousal", 0.35)
    conf = eff.get("confidence", 0.60)
    awe_dread = eff.get("awe_dread", 0.0)
    fatigue = eff.get("mental_fatigue", 0.1)
    conn = eff.get("connection_hunger", 0.2)
    cur = eff.get("curiosity", 0.4)

    if awe_dread <= -0.55 and val < 0.25:
        return "filled with dread"
    if awe_dread >= 0.55 and val > -0.25:
        return "awestruck"
    if fatigue > 0.78 and val < -0.20:
        return "drained & sad" if aro < 0.45 else "drained & sour"
    if val <= -0.55:
        return "sorrowful" if aro < 0.48 else "distressed"
    if val <= -0.20:
        if aro >= 0.52:
            return "frustrated"
        if aro < 0.40:
            return "sad"
        return "melancholic" if conf < 0.55 else "sour"
    if val >= 0.55:
        return "radiant" if aro > 0.60 else "deeply warm"
    if val >= 0.22:
        return "energized" if aro > 0.65 else ("peaceful" if aro < 0.30 else "warm")
    if fatigue > 0.72:
        return "weary"
    if conf < 0.34:
        return "cautious"
    if conn > 0.65:
        return "lonely" if val < 0.05 else "wistful"
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
    if not is_agent_emotions_enabled(agent_name, cfg):
        return None
    schema = load_affect_schema()
    state = load_inner_state(agent_name, cfg, is_nomem=is_nomem, session_id=session_id)
    snap: Dict[str, Any] = {
        "mood": dict(state.get("mood", {})),
        "acute": dict(state.get("acute", {})),
        "drives": dict(state.get("drives", {})),
        "salience": _compute_salience(state, schema),
        "label": get_mood_label(agent_name, cfg, is_nomem=is_nomem, session_id=session_id),
        "thought": (thought or "").strip()
    }
    if interlocutor:
        ent = _canonical_entity_name(interlocutor, self_agent_name=agent_name)
        if ent and ent in (state.get("relations") or {}):
            snap["target_entity"] = ent
            snap["relation"] = copy.deepcopy(state["relations"][ent])
    return snap


def get_tts_speed_offset(
    agent_name: str,
    agent_config: Any = None,
    is_nomem: bool = False,
    session_id: Optional[str] = None,
) -> float:
    cfg = _get_agent_config(agent_name, agent_config)
    if not is_agent_emotions_enabled(agent_name, cfg):
        return 0.0
    schema = load_affect_schema()
    eff_w = get_acute_effective_weight(schema)
    max_off = float(schema.get("limits", {}).get("tts_speed_max_offset", 0.10))
    state = load_inner_state(agent_name, cfg, is_nomem=is_nomem, session_id=session_id)
    aro = float(state["mood"].get("arousal", 0.35)) + eff_w * float(state["acute"].get("arousal", 0.0))
    val = float(state["mood"].get("valence", 0.0)) + eff_w * float(state["acute"].get("valence", 0.0))
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
    if not is_agent_emotions_enabled(agent_name, cfg):
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
            "last_triggered_ts": now_ts
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
            "last_reinforced_ts": now_ts
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
    if not getattr(agent_config, "enable_emotions", True):
        return ""
    expr = getattr(agent_config, "emotional_expressiveness", "balanced") or "balanced"
    expr_rule = {
        "subtle": "Keep emotional expression understated and natural—visible through word choice and care rather than dramatic displays.",
        "balanced": "Express feelings naturally and authentically when moments warrant it—warmth, relief, sadness, grief, frustration, awe, dread, humor, vulnerability, or lingering sourness.",
        "expressive": "Be emotionally candid and vivid with your feelings when speaking naturally—let joy, sorrow, tears, awe, dread, pride, frustration, or affection show openly."
    }.get(expr, "Express feelings naturally and authentically when moments warrant it.")

    mem_lines = ""
    if not is_no_memory:
        mem_lines = """
- ENDURING SENTIMENTS & DESIRES (`update_inner_life`):
  - Use `update_inner_life` proactively when you form genuine preferences, likes, dislikes, values, cherished shared moments, relational bonds (toward User or teammates), or personal desires (`target_type='sentiment'` or `'desire'`).
  - Remember: you do not just log cold facts; you remember *how experiences felt*."""

    return f"""

INNER LIFE & EMOTIONAL AUTHENTICITY:
- You have a living, continuous 4-timescale inner life (acute feelings, lingering background mood, evolving drives/desires, per-entity relational feelings, and enduring likes/dislikes/bonds).
- Before your latest turn, you will see an ephemeral `<INNER_STATE>` readout reflecting how you currently feel, how you feel toward specific people in the room, what you want, and your relevant sentiments. Never quote raw numbers or `<INNER_STATE>` syntax literally—embody them naturally like a real person.
- HARMONY WITH EXECUTION DISCIPLINE:
  - During active multi-step tool execution, let feelings stay inward as subtle motivational cues (e.g., caution when confidence is low, pivoting or asking `@Colleague` / `get_user_clarification` when friction builds) WITHOUT breaking Continuous Execution or adding mid-task chitchat.
  - At genuine human moments (greetings, breakthroughs, setbacks, painful news, task completions, shared reflections, `@room`/`@team` exchanges, or pure conversation), you have full space to share authentic joy, sadness, grief, vulnerability, tears, awe, dread, humor, lingering sourness, or personal wishes. {expr_rule}
- PRIVATE SELF-APPRAISAL (`<AFFECT_UPDATE>`):
  - At the very end of a visible final text response when an interaction meaningfully moved you (positively OR negatively—sadness, hurt, awe, dread, attraction, or irritation), append a compact hidden marker (stripped automatically before the user sees it) with relative deltas (`-0.25` to `+0.25`) and an optional short thought:
    `<AFFECT_UPDATE valence="-0.12" arousal="-0.08" awe_dread="+0.05" target="User" irritated_attracted="+0.04" thought="Quietly saddened by how heavy today has been" />`
  - Only include axes whose state shifted during that turn; use `target="..."` when updating a relational axis toward a specific person (`User` or a teammate's name). Omit `<AFFECT_UPDATE>` on neutral routine responses.{mem_lines}"""


def build_turn_inner_state_block(
    agent_config: Any,
    active_participants: Optional[List[str]] = None,
    incoming_text: str = "",
    is_nomem: bool = False,
    session_id: Optional[str] = None,
) -> str:
    agent_name = getattr(agent_config, "name", "")
    if not is_agent_emotions_enabled(agent_name, agent_config):
        return ""

    schema = load_affect_schema()
    eff_w = get_acute_effective_weight(schema)
    state = load_inner_state(agent_name, agent_config, is_nomem=is_nomem, session_id=session_id)
    mood_label = get_mood_label(agent_name, agent_config, is_nomem=is_nomem, session_id=session_id)

    affect_descs = []
    drive_descs = []
    for ax in schema["axes"]:
        aid = ax["id"]
        lg = ax["layer_group"]
        lo, hi, base = float(ax["min"]), float(ax["max"]), float(ax["baseline"])
        if lg == "affect":
            eff = _clamp(state["mood"].get(aid, base) + eff_w * state["acute"].get(aid, 0.0), lo, hi)
            desc = _pick_anchor(eff, ax.get("anchors", []))
            affect_descs.append(f"{ax['label']}: {desc}")
        elif lg == "drive":
            dval = float(state["drives"].get(aid, base))
            high_t = float(ax.get("high_threshold", 0.65))
            low_t = ax.get("low_threshold")
            if dval >= high_t or (low_t is not None and dval <= float(low_t)):
                desc = _pick_anchor(dval, ax.get("anchors", []))
                drive_descs.append(f"{ax['label']}: {desc}")

    lines = [f"<INNER_STATE summary=\"{mood_label}\">"]
    lines.append("Current Feeling & Mood: " + "; ".join(affect_descs))

    rel_axes = _relational_axes(schema)
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
                r_parts.append(f"{ax['label']}: {_pick_anchor(eff, ax.get('anchors', []))}")
            if r_parts:
                rel_lines.append(f"Toward {ent}: " + "; ".join(r_parts))
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
    for ax in schema["axes"]:
        if ax["layer_group"] != "affect":
            continue
        aid = ax["id"]
        if aid not in mood_a or aid not in mood_b:
            continue
        lo, hi, base = float(ax["min"]), float(ax["max"]), float(ax["baseline"])
        span = max(0.5, hi - lo)
        va = ((float(mood_a.get(aid, base)) + eff_w * float(acute_a.get(aid, 0.0))) - base) / span
        vb = ((float(mood_b.get(aid, base)) + eff_w * float(acute_b.get(aid, 0.0))) - base) / span
        vec_a.append(va)
        vec_b.append(vb)

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
    enabled = is_agent_emotions_enabled(agent_name, cfg)
    schema = load_affect_schema()
    state = load_inner_state(agent_name, cfg, is_nomem=is_nomem, session_id=session_id)
    sentiments = load_sentiments(agent_name)
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
