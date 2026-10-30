"""MuJoCo pick-and-place task.

Robot: a Cartesian gantry (x/y/z slide joints driven by position servos) carrying a parallel two-finger gripper
whose fingers are force-controlled motors. Grasping is purely contact/friction based, so slip, collision and
release failures emerge from the physics rather than from scripted outcomes.

Coordinates: the table surface is the plane z = 0. The gantry's z joint position equals the tool-centre-point
(TCP) height; fingertips sit 5 mm below the TCP.
"""

from __future__ import annotations

import math
from typing import Any

import mujoco
import numpy as np
from pydantic import BaseModel, ConfigDict, Field

TASK_ID = "pick_place_box"
TASK_VERSION = "1.0.0"

PHYSICS_DT = 0.002
SUBSTEPS = 10
CONTROL_DT = PHYSICS_DT * SUBSTEPS  # 50 Hz control
MAX_SPEED = 0.6  # m/s, scale of the xyz velocity part of the action
MAX_GRIP_FORCE = 30.0  # N, scale of the gripper part of the action
WORKSPACE_LOW = np.array([-0.28, -0.28, 0.012])
WORKSPACE_HIGH = np.array([0.28, 0.28, 0.33])
HOME = np.array([0.0, -0.05, 0.22])
FINGER_OFFSET = 0.05
FINGER_HALF_THICKNESS = 0.006
OPEN_GAP = 2 * (FINGER_OFFSET - FINGER_HALF_THICKNESS)
FINGER_STROKE = FINGER_OFFSET - FINGER_HALF_THICKNESS
OBSTACLE_Y = 0.01
OBSTACLE_HALF_LENGTH = 0.12
OBSTACLE_HALF_THICKNESS = 0.01

# Contact bitmask written into the recorded state stream.
C_LEFT_OBJECT = 1
C_RIGHT_OBJECT = 2
C_OBJECT_TABLE = 4
C_OBJECT_OBSTACLE = 8
C_ROBOT_OBSTACLE = 16
C_ROBOT_TABLE = 32
C_PALM_OBJECT = 64

ACTION_SPEC = {
    "dims": 4,
    "names": ["vx", "vy", "vz", "grip"],
    "low": [-1.0, -1.0, -1.0, -1.0],
    "high": [1.0, 1.0, 1.0, 1.0],
    "units": ["normalized (x0.6 m/s)"] * 3 + ["normalized (x30 N, + closes)"],
    # Largest per-step change the baseline controller can make by design: a phase reset drops speed from at most
    # 0.35/0.6 = 0.58 to 0, plus one acceleration step (0.1). Used by data-quality rule S003.
    "max_delta_per_step": [0.7, 0.7, 0.7, 1.0],
}

OBSERVATION_SPEC = {
    "tcp_pos": {"dims": 3, "units": "m", "source": "joint encoders (exact)"},
    "tcp_cmd": {"dims": 3, "units": "m", "source": "commanded servo target"},
    "finger_gap": {"dims": 1, "units": "m", "source": "finger encoders (exact)"},
    "object_pos_est": {"dims": 3, "units": "m", "source": "ground truth + i.i.d. Gaussian noise (obs_noise_std)"},
    "object_yaw_est": {"dims": 1, "units": "rad", "source": "ground truth + Gaussian noise (5 x obs_noise_std)"},
    "object_half_size": {"dims": 1, "units": "m", "source": "exact"},
    "target_pos": {"dims": 3, "units": "m", "source": "exact"},
    "obstacle_height": {"dims": 1, "units": "m", "source": "exact"},
}


class EnvParams(BaseModel):
    """Every physically meaningful, perturbable parameter of the task, with validated bounds."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    object_x: float = Field(0.0, ge=-0.08, le=0.08)
    object_y: float = Field(-0.12, ge=-0.18, le=-0.06)
    object_yaw: float = Field(0.0, ge=-0.8, le=0.8)
    object_half_size: float = Field(0.02, ge=0.014, le=0.03)
    object_mass: float = Field(0.15, ge=0.03, le=1.0)
    object_friction: float = Field(0.8, ge=0.15, le=1.5)
    target_x: float = Field(0.0, ge=-0.1, le=0.1)
    target_y: float = Field(0.14, ge=0.1, le=0.2)
    target_half_size: float = Field(0.04, ge=0.02, le=0.06)
    obstacle_height: float = Field(0.04, ge=0.0, le=0.16)
    obs_noise_std: float = Field(0.0, ge=0.0, le=0.02)
    max_steps: int = Field(600, ge=50, le=3000)
    settle_steps: int = Field(25, ge=5, le=200)


def build_mjcf(p: EnvParams) -> str:
    h = p.object_half_size
    obstacle = ""
    if p.obstacle_height > 1e-4:
        oh = p.obstacle_height / 2
        obstacle = (
            f'<geom name="obstacle" type="box" pos="0 {OBSTACLE_Y} {oh}" '
            f'size="{OBSTACLE_HALF_LENGTH} {OBSTACLE_HALF_THICKNESS} {oh}" rgba=".55 .35 .2 1"/>'
        )
    fo, ft = FINGER_OFFSET, FINGER_HALF_THICKNESS
    return f"""
<mujoco model="amperes_pick_place">
  <compiler angle="radian" autolimits="true"/>
  <option timestep="{PHYSICS_DT}" gravity="0 0 -9.81" cone="elliptic" impratio="10" integrator="implicitfast"/>
  <visual><global offwidth="640" offheight="480"/></visual>
  <worldbody>
    <light pos="0 0 1.5" dir="0 0 -1" directional="true"/>
    <geom name="table" type="plane" size="0.6 0.6 0.01" friction="0.5 0.005 0.0001" rgba=".85 .85 .85 1"/>
    <site name="target" type="box" pos="{p.target_x} {p.target_y} 0.0005"
          size="{p.target_half_size} {p.target_half_size} 0.0005" rgba="0 .7 0 .35"/>
    {obstacle}
    <body name="object" pos="{p.object_x} {p.object_y} {h + 0.0005}" euler="0 0 {p.object_yaw}">
      <freejoint name="object_joint"/>
      <geom name="object" type="box" size="{h} {h} {h}" mass="{p.object_mass}"
            friction="{p.object_friction} 0.005 0.0001" condim="4" priority="1" rgba=".15 .35 .85 1"/>
    </body>
    <body name="carriage_x">
      <inertial pos="0 0 0" mass="2" diaginertia="0.02 0.02 0.02"/>
      <joint name="x" type="slide" axis="1 0 0" range="-0.3 0.3" damping="5"/>
      <body name="carriage_y">
        <inertial pos="0 0 0" mass="1.5" diaginertia="0.02 0.02 0.02"/>
        <joint name="y" type="slide" axis="0 1 0" range="-0.3 0.3" damping="5"/>
        <body name="tool" gravcomp="1">
          <joint name="z" type="slide" axis="0 0 1" range="0 0.35" damping="5"/>
          <geom name="palm" type="box" pos="0 0 0.075" size="0.06 0.02 0.01" mass="0.4" rgba=".3 .3 .3 1"/>
          <site name="tcp" pos="0 0 0" size="0.004" rgba="1 0 0 1"/>
          <body name="left_finger" pos="{-fo} 0 0" gravcomp="1">
            <joint name="left" type="slide" axis="1 0 0" range="0 {FINGER_STROKE}" damping="15"/>
            <geom name="left_finger" type="box" pos="0 0 0.03" size="{ft} 0.012 0.035" mass="0.05"
                  friction="0.5 0.005 0.0001" condim="4" rgba=".7 .2 .2 1"/>
          </body>
          <body name="right_finger" pos="{fo} 0 0" gravcomp="1">
            <joint name="right" type="slide" axis="-1 0 0" range="0 {FINGER_STROKE}" damping="15"/>
            <geom name="right_finger" type="box" pos="0 0 0.03" size="{ft} 0.012 0.035" mass="0.05"
                  friction="0.5 0.005 0.0001" condim="4" rgba=".7 .2 .2 1"/>
          </body>
        </body>
      </body>
    </body>
  </worldbody>
  <actuator>
    <position name="ax" joint="x" kp="4000" kv="300" forcerange="-300 300"/>
    <position name="ay" joint="y" kp="4000" kv="220" forcerange="-300 300"/>
    <position name="az" joint="z" kp="4000" kv="110" forcerange="-300 300"/>
    <motor name="aleft" joint="left" ctrlrange="-{MAX_GRIP_FORCE} {MAX_GRIP_FORCE}"/>
    <motor name="aright" joint="right" ctrlrange="-{MAX_GRIP_FORCE} {MAX_GRIP_FORCE}"/>
  </actuator>
</mujoco>
"""


class PickPlaceEnv:
    """Gymnasium-style reset/step interface (without a gymnasium dependency)."""

    task_id = TASK_ID
    task_version = TASK_VERSION

    def __init__(self, params: EnvParams, seed: int):
        self.params = params
        self.seed = int(seed)
        self.model = mujoco.MjModel.from_xml_string(build_mjcf(params))
        self.data = mujoco.MjData(self.model)
        g = lambda name: mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_GEOM, name)  # noqa: E731
        self.g_left, self.g_right, self.g_palm = g("left_finger"), g("right_finger"), g("palm")
        self.g_object, self.g_table, self.g_obstacle = g("object"), g("table"), g("obstacle")
        self.robot_geoms = {self.g_left, self.g_right, self.g_palm}
        self.obj_qadr = self.model.jnt_qposadr[mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_JOINT, "object_joint")]
        self.obj_vadr = self.model.jnt_dofadr[mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_JOINT, "object_joint")]
        jq = lambda name: self.model.jnt_qposadr[mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_JOINT, name)]  # noqa: E731
        self.q_xyz = [jq("x"), jq("y"), jq("z")]
        self.q_left, self.q_right = jq("left"), jq("right")
        self.rng = np.random.default_rng(self.seed)
        self.target = HOME.copy()
        self.step_count = 0

    def metadata(self) -> dict[str, Any]:
        return {
            "task_id": TASK_ID,
            "task_version": TASK_VERSION,
            "simulator": "mujoco",
            "simulator_version": mujoco.__version__,
            "physics_dt": PHYSICS_DT,
            "control_dt": CONTROL_DT,
            "action_spec": ACTION_SPEC,
            "observation_spec": OBSERVATION_SPEC,
        }

    def reset(self) -> dict[str, np.ndarray]:
        mujoco.mj_resetData(self.model, self.data)
        for i, q in enumerate(self.q_xyz):
            self.data.qpos[q] = HOME[i]
        self.target = HOME.copy()
        self.data.ctrl[:3] = self.target
        self.data.ctrl[3:5] = -8.0  # hold fingers open
        mujoco.mj_forward(self.model, self.data)
        self.rng = np.random.default_rng(self.seed)
        self.step_count = 0
        return self._observe()

    # ---- state access -------------------------------------------------------------------------------------
    def tcp_pos(self) -> np.ndarray:
        return np.array([self.data.qpos[q] for q in self.q_xyz])

    def finger_q(self) -> np.ndarray:
        return np.array([self.data.qpos[self.q_left], self.data.qpos[self.q_right]])

    def object_pos(self) -> np.ndarray:
        return self.data.qpos[self.obj_qadr : self.obj_qadr + 3].copy()

    def object_quat(self) -> np.ndarray:
        return self.data.qpos[self.obj_qadr + 3 : self.obj_qadr + 7].copy()

    def object_linvel(self) -> np.ndarray:
        return self.data.qvel[self.obj_vadr : self.obj_vadr + 3].copy()

    def contact_mask(self) -> int:
        mask = 0
        for i in range(self.data.ncon):
            c = self.data.contact[i]
            pair = {int(c.geom1), int(c.geom2)}
            if self.g_object in pair:
                other = (pair - {self.g_object}) or {self.g_object}
                o = next(iter(other))
                if o == self.g_left:
                    mask |= C_LEFT_OBJECT
                elif o == self.g_right:
                    mask |= C_RIGHT_OBJECT
                elif o == self.g_palm:
                    mask |= C_PALM_OBJECT
                elif o == self.g_table:
                    mask |= C_OBJECT_TABLE
                elif o == self.g_obstacle:
                    mask |= C_OBJECT_OBSTACLE
            elif pair & self.robot_geoms:
                if self.g_obstacle >= 0 and self.g_obstacle in pair:
                    mask |= C_ROBOT_OBSTACLE
                if self.g_table in pair:
                    mask |= C_ROBOT_TABLE
        return mask

    def unstable(self) -> bool:
        if self.data.warning[mujoco.mjtWarning.mjWARN_BADQACC].number > 0:
            return True
        return not (np.all(np.isfinite(self.data.qpos)) and np.all(np.isfinite(self.data.qvel)))

    def ground_truth(self) -> dict[str, Any]:
        return {
            "tcp_pos": self.tcp_pos(),
            "tcp_cmd": self.target.copy(),
            "finger_q": self.finger_q(),
            "object_pos": self.object_pos(),
            "object_quat": self.object_quat(),
            "object_linvel": self.object_linvel(),
            "contacts": self.contact_mask(),
        }

    def _observe(self) -> dict[str, np.ndarray]:
        p = self.params
        # Noise is drawn every step (even when std == 0) so the RNG stream is identical across conditions.
        n = self.rng.normal(0.0, 1.0, 4)
        obj = self.object_pos()
        q = self.object_quat()
        yaw = math.atan2(2 * (q[0] * q[3] + q[1] * q[2]), 1 - 2 * (q[2] ** 2 + q[3] ** 2))
        fq = self.finger_q()
        return {
            "tcp_pos": self.tcp_pos(),
            "tcp_cmd": self.target.copy(),
            "finger_gap": np.array([OPEN_GAP - fq[0] - fq[1]]),
            "object_pos_est": obj + p.obs_noise_std * n[:3],
            "object_yaw_est": np.array([yaw + 5 * p.obs_noise_std * n[3]]),
            "object_half_size": np.array([p.object_half_size]),
            "target_pos": np.array([p.target_x, p.target_y, 0.0]),
            "obstacle_height": np.array([p.obstacle_height]),
        }

    def step(self, action) -> tuple[dict[str, np.ndarray], dict[str, Any]]:
        info: dict[str, Any] = {"invalid_action": False, "clipped": False, "workspace_clipped": False}
        a = np.asarray(action, dtype=float).reshape(-1)
        if a.shape != (4,) or not np.all(np.isfinite(a)):
            info["invalid_action"] = True
            a = np.array([0.0, 0.0, 0.0, 0.0])
        c = np.clip(a, -1.0, 1.0)
        info["clipped"] = bool(np.any(c != a))
        new_target = self.target + c[:3] * MAX_SPEED * CONTROL_DT
        bounded = np.clip(new_target, WORKSPACE_LOW, WORKSPACE_HIGH)
        info["workspace_clipped"] = bool(np.any(bounded != new_target))
        self.target = bounded
        self.data.ctrl[:3] = self.target
        self.data.ctrl[3] = self.data.ctrl[4] = c[3] * MAX_GRIP_FORCE
        for _ in range(SUBSTEPS):
            mujoco.mj_step(self.model, self.data)
        self.step_count += 1
        info["applied_action"] = c
        info["unstable"] = self.unstable()
        return self._observe(), info
