# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""
Integration methods for N-body simulation.

This module defines the integration method names and the helpers that resolve
them for dispatch. The steppers themselves live in physics.py and barnes_hut.py
(CPU) and physics_gpu.py (GPU).

Base methods:
- simple_euler: explicit Euler (position from the old velocity), first order
- symplectic_euler: semi-implicit Euler (position from the new velocity), first order, symplectic (default)
- velocity_verlet: Störmer-Verlet, second order, symplectic, one force evaluation per step
- rk4: classical Runge-Kutta, fourth order, four force evaluations per step
- forest_ruth: Forest-Ruth, fourth order, symplectic, three force evaluations per step

The symplectic methods keep the energy error bounded (no secular drift) only at
a fixed dt. With adaptive timestepping the step size depends on the state, the
map is no longer symplectic, and that guarantee does not hold.

Distributed variants (gvv_*, grk4_*, gfr_*): the same base method in a package
step whose off-package background is predicted to each stage time (frozen,
linear or acceleration predictor). See package_sources._stage_source_positions.
"""

from enum import Enum


class IntegrationMethod(str, Enum):
    """Available integration methods."""
    SIMPLE_EULER = "simple_euler"
    SYMPLECTIC_EULER = "symplectic_euler"
    VELOCITY_VERLET = "velocity_verlet"
    RK4 = "rk4"
    FOREST_RUTH = "forest_ruth"

    # --- Distributed predicted-background variants ----------------------------
    # Same base one-step map as their plain counterpart, but the off-package
    # "background" bodies are extrapolated to each intermediate stage time at an
    # increasing Taylor order. The predictor order caps the achievable GLOBAL
    # order of the distributed step:
    #   frozen  (x0)              -> order 1  (control)
    #   linear  (x0 + tau*v)      -> order 2  (default for the plain names)
    #   accel   (+ 0.5*tau^2*a)   -> order 3
    # VV is intrinsically 2nd order so linear already saturates its ORDER. The
    # accel variant (gvv_accel) is still useful because its tau=dt prediction
    # x + dt*v + dt^2/2*a coincides with the Verlet drift, so the distributed
    # step reproduces the monolithic step bitwise, and the epoch-start a0 it
    # needs doubles as the package's cached acceleration (one force evaluation
    # per task instead of two).
    GVV_FROZEN = "gvv_frozen"
    GVV_LINEAR = "gvv_linear"
    GVV_ACCEL = "gvv_accel"
    GRK4_FROZEN = "grk4_frozen"
    GRK4_LINEAR = "grk4_linear"
    GRK4_ACCEL = "grk4_accel"
    GFR_FROZEN = "gfr_frozen"
    GFR_LINEAR = "gfr_linear"
    GFR_ACCEL = "gfr_accel"

    @classmethod
    def is_valid(cls, value: str) -> bool:
        """Check if a string is a valid integration method."""
        return value in [m.value for m in cls]


# The integration_method help of the job submission endpoints (v1 and v2)
INTEGRATION_METHOD_HELP = (
    "Numerical integration method: 'simple_euler' (O(dt)), 'symplectic_euler' (O(dt), default), "
    "'velocity_verlet' (O(dt^2)), 'rk4' (O(dt^4), 4 force evaluations per step) or 'forest_ruth' "
    "(symplectic, O(dt^4), 3 force evaluations per step). These orders hold for a monolithic run. In a "
    "distributed run the order also depends on how off-package bodies are predicted to each stage time: "
    "the variants gvv_*, grk4_* and gfr_* with the suffix frozen, linear or accel reach order 1, 2 or 3 "
    "(velocity Verlet at most 2). The plain velocity_verlet, rk4 and forest_ruth names use the linear "
    "predictor, so distributed rk4 and forest_ruth are second order. Use grk4_accel or gfr_accel for third order."
)


# Distributed predicted-background method resolution
# A g-named method resolves to (base integrator, predictor order): the base
# selects which stepper runs. The predictor order selects how off-package bodies
# are extrapolated to each stage time. Plain integrator names map to themselves
# with the linear predictor. So there is one stepper per base integrator, driven
# by a predictor-order integer, while the public names stay a flat list.
PREDICTOR_FROZEN = 0   # background held at epoch-start x0 (control)
PREDICTOR_LINEAR = 1   # x0 + tau*v            (order 2)
PREDICTOR_ACCEL = 2    # x0 + tau*v + 0.5*tau^2*a  (order 3)

_BASE_INTEGRATION_METHOD = {
    IntegrationMethod.GVV_FROZEN.value: IntegrationMethod.VELOCITY_VERLET.value,
    IntegrationMethod.GVV_LINEAR.value: IntegrationMethod.VELOCITY_VERLET.value,
    IntegrationMethod.GVV_ACCEL.value: IntegrationMethod.VELOCITY_VERLET.value,
    IntegrationMethod.GRK4_FROZEN.value: IntegrationMethod.RK4.value,
    IntegrationMethod.GRK4_LINEAR.value: IntegrationMethod.RK4.value,
    IntegrationMethod.GRK4_ACCEL.value: IntegrationMethod.RK4.value,
    IntegrationMethod.GFR_FROZEN.value: IntegrationMethod.FOREST_RUTH.value,
    IntegrationMethod.GFR_LINEAR.value: IntegrationMethod.FOREST_RUTH.value,
    IntegrationMethod.GFR_ACCEL.value: IntegrationMethod.FOREST_RUTH.value,
}

_PREDICTOR_ORDER = {
    IntegrationMethod.GVV_FROZEN.value: PREDICTOR_FROZEN,
    IntegrationMethod.GVV_LINEAR.value: PREDICTOR_LINEAR,
    IntegrationMethod.GVV_ACCEL.value: PREDICTOR_ACCEL,
    IntegrationMethod.GRK4_FROZEN.value: PREDICTOR_FROZEN,
    IntegrationMethod.GRK4_LINEAR.value: PREDICTOR_LINEAR,
    IntegrationMethod.GRK4_ACCEL.value: PREDICTOR_ACCEL,
    IntegrationMethod.GFR_FROZEN.value: PREDICTOR_FROZEN,
    IntegrationMethod.GFR_LINEAR.value: PREDICTOR_LINEAR,
    IntegrationMethod.GFR_ACCEL.value: PREDICTOR_ACCEL,
}


def resolve_base_method(method: str) -> str:
    """Resolve a (possibly g-prefixed) method name to its base integrator name.

    grk4_accel -> rk4, gfr_linear -> forest_ruth, gvv_frozen -> velocity_verlet.
    Plain names (and unknown names) are returned unchanged.
    """
    return _BASE_INTEGRATION_METHOD.get(method, method)


def get_predictor_order(method: str) -> int:
    """Background predictor order for a distributed package step.

    0 frozen (x0), 1 linear (x0 + tau*v), 2 accel (+ 0.5*tau^2*a). Plain
    integrator names default to linear (1). Euler methods evaluate only at tau=0, so the
    value is irrelevant for them.
    """
    return _PREDICTOR_ORDER.get(method, PREDICTOR_LINEAR)


def is_velocity_verlet(method: str) -> bool:
    """True for velocity_verlet and the gvv_* variants."""
    return resolve_base_method(method) == IntegrationMethod.VELOCITY_VERLET.value


def is_rk4(method: str) -> bool:
    """True for rk4 and the grk4_* variants."""
    return resolve_base_method(method) == IntegrationMethod.RK4.value


def is_forest_ruth(method: str) -> bool:
    """True for forest_ruth and the gfr_* variants."""
    return resolve_base_method(method) == IntegrationMethod.FOREST_RUTH.value
