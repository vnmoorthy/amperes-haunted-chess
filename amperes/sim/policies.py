"""Policy adapter interface, the scripted baseline controller, and an open-loop action-replay policy.

The baseline is a classical scripted state machine (perceive → approach → descend → close → lift → transport →
lower → release → retreat). It is a *baseline controller*, not a learned or foundation-model policy.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Protocol

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from amperes.sim.env import CONTROL_DT, MAX_GRIP_FORCE, MAX_SPEED, WORKSPACE_HIGH


class PolicyAdapter(Protocol):
    """Interface every policy (scripted, learned, or remote VLA wrapper) must implement."""

    policy_id: str
    policy_version: str

    def reset(self) -> None: ...

    def act(self, obs: dict[str, np.ndarray]) -> tuple[np.ndarray, bool]:
        """Return (action in [-1, 1]^4, done flag)."""
        ...

    def annotations(self) -> list[dict[str, Any]]:
        """Policy-reported events (e.g. phase changes). Recorded as policy claims, never used for verification."""
        ...

    def config_dict(self) -> dict[str, Any]: ...


class ScriptedConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    policy_id: str = "scripted_pick_place"
    version: str = "1.0.0"
    approach_height: float = Field(0.10, ge=0.03, le=0.3)
    transport_height: float = Field(0.12, ge=0.04, le=0.32)
    grasp_z_offset: float = Field(0.0, ge=-0.01, le=0.02)
    place_clearance: float = Field(0.01, ge=0.0, le=0.08)
    grip_force: float = Field(6.0, ge=0.5, le=MAX_GRIP_FORCE)
    open_force: float = Field(8.0, ge=0.5, le=MAX_GRIP_FORCE)
    max_speed: float = Field(0.35, gt=0.0, le=MAX_SPEED)
    max_accel: float = Field(3.0, gt=0.0, le=20.0)
    pos_tolerance: float = Field(0.004, gt=0.0, le=0.03)
    grasp_settle_s: float = Field(0.3, ge=0.0, le=2.0)
    release_settle_s: float = Field(0.3, ge=0.0, le=2.0)
    perception_samples: int = Field(1, ge=1, le=50)
    obstacle_aware: bool = False
    obstacle_clearance: float = Field(0.03, ge=0.0, le=0.1)
    grasp_check: bool = False
    max_grasp_retries: int = Field(0, ge=0, le=5)
    phase_timeout_s: float = Field(3.0, gt=0.0, le=20.0)

    def config_hash(self) -> str:
        return hashlib.sha256(json.dumps(self.model_dump(), sort_keys=True).encode()).hexdigest()[:12]

    def label(self) -> str:
        return f"{self.policy_id}@{self.version}#{self.config_hash()}"


PHASES = ["perceive", "move_above", "descend", "close", "lift", "transport", "lower", "release", "retreat", "done"]


class ScriptedPickPlace:
    def __init__(self, cfg: ScriptedConfig):
        self.cfg = cfg
        self.policy_id = cfg.policy_id
        self.policy_version = f"{cfg.version}+{cfg.config_hash()}"
        self.reset()

    def config_dict(self) -> dict[str, Any]:
        return self.cfg.model_dump()

    def reset(self) -> None:
        self.phase = "perceive"
        self.phase_t = 0.0
        self.t = 0.0
        self.speed = 0.0
        self.samples: list[np.ndarray] = []
        self.obj_est: np.ndarray | None = None
        self.retries = 0
        self._events: list[dict[str, Any]] = [{"t": 0.0, "type": "policy_phase", "value": "perceive"}]

    def annotations(self) -> list[dict[str, Any]]:
        return self._events

    def _enter(self, phase: str, note: str | None = None) -> None:
        self.phase = phase
        self.phase_t = 0.0
        self.speed = 0.0
        ev = {"t": round(self.t, 4), "type": "policy_phase", "value": phase}
        if note:
            ev["note"] = note
        self._events.append(ev)

    def _velocity_toward(self, cmd: np.ndarray, goal: np.ndarray) -> np.ndarray:
        """Trapezoidal-profile velocity command from the current servo target toward goal."""
        delta = goal - cmd
        dist = float(np.linalg.norm(delta))
        if dist < 1e-6:
            self.speed = 0.0
            return np.zeros(3)
        a = self.cfg.max_accel
        speed = min(self.cfg.max_speed, self.speed + a * CONTROL_DT, (2 * a * dist) ** 0.5)
        if speed * CONTROL_DT > dist:
            speed = dist / CONTROL_DT
        self.speed = speed
        return delta / dist * speed

    def _transport_z(self, obs) -> float:
        z = self.cfg.transport_height
        if self.cfg.obstacle_aware:
            h = float(obs["object_half_size"][0])
            z = max(z, float(obs["obstacle_height"][0]) + self.cfg.obstacle_clearance + h)
        return min(z, float(WORKSPACE_HIGH[2]))

    def act(self, obs: dict[str, np.ndarray]) -> tuple[np.ndarray, bool]:
        c = self.cfg
        tcp, cmd = obs["tcp_pos"], obs["tcp_cmd"]
        h = float(obs["object_half_size"][0])
        target = obs["target_pos"]
        grip = -c.open_force
        goal = cmd.copy()
        timed_out = self.phase_t > c.phase_timeout_s

        def reached(g: np.ndarray) -> bool:
            return bool(np.linalg.norm(g - tcp) < c.pos_tolerance and np.linalg.norm(g - cmd) < 1e-6)

        if self.phase == "perceive":
            self.samples.append(obs["object_pos_est"].copy())
            if len(self.samples) >= c.perception_samples:
                self.obj_est = np.mean(self.samples, axis=0)
                self._enter("move_above")
        elif self.phase == "move_above":
            goal = np.array([self.obj_est[0], self.obj_est[1], c.approach_height])
            if reached(goal) or timed_out:
                self._enter("descend", "phase_timeout" if timed_out else None)
        elif self.phase == "descend":
            goal = np.array([self.obj_est[0], self.obj_est[1], h + c.grasp_z_offset])
            if reached(goal) or timed_out:
                self._enter("close", "phase_timeout" if timed_out else None)
        elif self.phase == "close":
            grip = c.grip_force
            if self.phase_t >= c.grasp_settle_s:
                gap = float(obs["finger_gap"][0])
                bad = gap < 2 * h - 0.008 or gap > 2 * h + 0.01
                if c.grasp_check and bad and self.retries < c.max_grasp_retries:
                    self.retries += 1
                    self.samples = []
                    self._enter("perceive", f"grasp_check_failed gap={gap:.4f} retry={self.retries}")
                    grip = -c.open_force
                else:
                    self._enter("lift")
        elif self.phase == "lift":
            grip = c.grip_force
            goal = np.array([cmd[0], cmd[1], self._transport_z(obs)])
            if reached(goal) or timed_out:
                self._enter("transport", "phase_timeout" if timed_out else None)
        elif self.phase == "transport":
            grip = c.grip_force
            goal = np.array([target[0], target[1], self._transport_z(obs)])
            if reached(goal) or timed_out:
                self._enter("lower", "phase_timeout" if timed_out else None)
        elif self.phase == "lower":
            grip = c.grip_force
            goal = np.array([target[0], target[1], h + c.place_clearance])
            if reached(goal) or timed_out:
                self._enter("release", "phase_timeout" if timed_out else None)
        elif self.phase == "release":
            if self.phase_t >= c.release_settle_s:
                self._enter("retreat")
        elif self.phase == "retreat":
            goal = np.array([cmd[0], cmd[1], c.approach_height])
            if reached(goal) or timed_out:
                self._enter("done")

        if self.phase == "perceive" and self.obj_est is not None:
            # During a grasp retry, lift slightly clear of the object while re-perceiving.
            goal = np.array([cmd[0], cmd[1], max(float(cmd[2]), h + 0.04)])
        vel = self._velocity_toward(cmd, goal) if self.phase not in ("close", "release", "done") else np.zeros(3)
        action = np.concatenate([vel / MAX_SPEED, [grip / MAX_GRIP_FORCE]])
        self.t += CONTROL_DT
        self.phase_t += CONTROL_DT
        return action, self.phase == "done"


class ActionReplayPolicy:
    """Open-loop replay of a recorded action sequence (used by the reproduction engine)."""

    def __init__(self, actions: list[list[float]], source_episode_id: str, done_at: int | None):
        self.actions = actions
        self.policy_id = "action_replay"
        self.policy_version = f"replay-of:{source_episode_id}"
        self.done_at = done_at
        self.i = 0

    def config_dict(self) -> dict[str, Any]:
        return {"policy_id": self.policy_id, "source_episode_id": self.policy_version, "n_actions": len(self.actions)}

    def reset(self) -> None:
        self.i = 0

    def annotations(self) -> list[dict[str, Any]]:
        return []

    def act(self, obs):
        if self.i >= len(self.actions):
            return np.zeros(4), True
        a = np.asarray(self.actions[self.i], dtype=float)
        self.i += 1
        done = self.done_at is not None and self.i >= self.done_at
        return a, done


def make_policy(cfg: dict[str, Any]) -> ScriptedPickPlace:
    """Only registered, validated policy configs can be instantiated. No code is loaded from experiment files."""
    pid = cfg.get("policy_id", "scripted_pick_place")
    if pid != "scripted_pick_place":
        raise ValueError(f"unknown policy_id {pid!r}; registered: ['scripted_pick_place']")
    return ScriptedPickPlace(ScriptedConfig(**cfg))
