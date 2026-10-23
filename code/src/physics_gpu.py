# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""
Implements parallel N-body simulation on GPUs using CUDA kernels
"""

import logging
import os
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np

from constants import FR_C1, FR_C2, FR_D1, FR_D2, FR_TAU1, FR_TAU2, FR_TAU3, G_CONST, SOFTENING_SQ
from physics_gpu_kernels import generate_cuda_kernel_source, generate_package_kernel_source


try:
    import pycuda.autoinit  # noqa: F401 — creates the CUDA context on import
    import pycuda.driver as cuda
    from pycuda.compiler import SourceModule
    from pycuda.compiler import compile as cuda_compile
    PYCUDA_AVAILABLE = True
except ImportError:
    PYCUDA_AVAILABLE = False

logger = logging.getLogger(__name__)


@dataclass
class _PackageLaunch:
    """One package step's launch: the package (start, k as kernel ints), the
    size of its vec3 buffers, the launch configuration, the predictor order
    and the scratch allocator."""
    start: np.int32
    k: np.int32
    start_idx: int
    nbytes: int
    launch: dict
    shared: int
    order: np.int32
    alloc: Callable


class GPUNBodyEngine:
    """GPU-accelerated N-body physics engine using CUDA."""

    def __init__(self,
                 bodies: List[Dict],
                 gravitational_constant: float = G_CONST,
                 softening_squared: float = SOFTENING_SQ,
                 time_step: float = 0.01,
                 threads_per_block: int = 256,
                 use_float32: bool = False,
                 integration_method: str = "symplectic_euler"):
        """A GPU engine for the bodies (dicts with keys idBody, m, x, y, z, vx, vy,
        vz). threads_per_block: CUDA threads per block (typically 128-256).
        use_float32: float32 kernels (faster on GPUs with weak FP64) instead of
        float64 (the default). integration_method: simple_euler, symplectic_euler
        (default), velocity_verlet, rk4 or forest_ruth."""
        self.integration_method = integration_method
        self.use_float32 = use_float32
        self.float_dtype = np.float32 if use_float32 else np.float64
        if not PYCUDA_AVAILABLE:
            raise ImportError("PyCUDA not available. Install with: pip install pycuda")

        cuda.init()
        if cuda.Device.count() == 0:
            raise RuntimeError("No CUDA-capable GPU devices found")

        self.device = cuda.Device(0)
        logger.info(f"Using GPU device: {self.device.name()}")

        self.N = len(bodies)
        self.G = gravitational_constant
        self.eps2 = softening_squared
        self.dt = time_step
        self.threads_per_block = threads_per_block
        self.num_blocks = (self.N + threads_per_block - 1) // threads_per_block

        # Detect GPU compute capability
        compute_capability = self.device.compute_capability()
        major, minor = compute_capability
        detected_arch = f"sm_{major}{minor}"
        logger.info(f"GPU: {self.device.name()} (compute capability {major}.{minor})")

        if major < 3:
            logger.warning(
                f"GPU compute capability {major}.{minor} is very old. "
                f"Minimum recommended: sm_35 (Kepler) or higher. "
                f"Performance may be suboptimal."
            )

        # Allow overriding the nvcc -arch flag, for cases where the installed
        # CUDA toolkit is older than the device (e.g. CUDA 12.2 nvcc on an
        # sm_120 Blackwell GPU). The driver will JIT a lower-arch PTX cube
        # to the device at load time.
        arch_override = os.environ.get("NBODY_GPU_ARCH")
        if arch_override:
            arch_name = arch_override
            logger.info(f"NBODY_GPU_ARCH override active: compiling with {arch_name} "
                        f"(detected device arch was {detected_arch})")
        else:
            arch_name = detected_arch

        # Generate and compile CUDA kernels with detected architecture flag.
        # Pass arch via SourceModule's `arch=` kwarg (NOT via `options=[...]`)
        # — otherwise pycuda's own auto-detected `-arch sm_<device>` flag is
        # appended after ours and wins per nvcc's "last -arch wins" rule.
        #
        # When NBODY_GPU_ARCH is overridden (because the toolkit is older
        # than the device), we compile to PTX via pycuda.compiler.compile and
        # load it via module_from_buffer. PTX is forward-compatible: the
        # driver JITs it to the actual device arch at load time. Without
        # this, an sm_89 cubin would refuse to load on an sm_120 device with
        # "no kernel image is available for execution on the device".
        precision = "float32" if use_float32 else "float64"
        compile_target = 'ptx' if arch_override else 'cubin'
        logger.info(f"Compiling CUDA kernels with arch={arch_name}, target={compile_target}, "
                    f"precision={precision}...")

        kernel_source = generate_cuda_kernel_source(use_float32)
        if compile_target == 'ptx':
            ptx = cuda_compile(kernel_source, arch=arch_name, target='ptx')
            self.module = cuda.module_from_buffer(ptx)
        else:
            self.module = SourceModule(kernel_source, arch=arch_name)
        self.kernel_accel = self.module.get_function("KernelComputeAccelerations")
        self.kernel_integrate_symplectic = self.module.get_function("KernelIntegrateTargetBodiesSymplecticEuler")
        self.kernel_integrate_simple = self.module.get_function("KernelIntegrateTargetBodiesSimpleEuler")
        # Velocity Verlet kernels (monolithic)
        self.kernel_vv_position = self.module.get_function("KernelVelocityVerletPositionUpdate")
        self.kernel_vv_velocity = self.module.get_function("KernelVelocityVerletVelocityUpdate")
        # RK4 kernels (monolithic)
        self.kernel_rk4_save_state = self.module.get_function("KernelRK4SaveState")
        self.kernel_rk4_set_intermediate = self.module.get_function("KernelRK4SetIntermediateState")
        self.kernel_rk4_final = self.module.get_function("KernelRK4FinalIntegration")
        # Forest-Ruth kernels (monolithic): generic drift/kick, orchestrated from host.
        self.kernel_fr_drift = self.module.get_function("KernelForestRuthDrift")
        self.kernel_fr_kick = self.module.get_function("KernelForestRuthKick")
        # Select active integration kernel based on method (for Euler methods).
        # Reject methods we don't recognize so we never silently fall through.
        self.use_velocity_verlet = (integration_method == "velocity_verlet")
        self.use_rk4 = (integration_method == "rk4")
        self.use_forest_ruth = (integration_method == "forest_ruth")
        if integration_method == "symplectic_euler":
            self.kernel_integrate = self.kernel_integrate_symplectic
        elif integration_method == "simple_euler":
            self.kernel_integrate = self.kernel_integrate_simple
        elif self.use_velocity_verlet or self.use_rk4 or self.use_forest_ruth:
            self.kernel_integrate = None  # Not used by these methods
        else:
            raise ValueError(
                f"Unknown integration_method '{integration_method}'. "
                f"Supported: simple_euler, symplectic_euler, velocity_verlet, rk4, forest_ruth"
            )

        package_kernel_source = generate_package_kernel_source(use_float32)
        if compile_target == 'ptx':
            pkg_ptx = cuda_compile(package_kernel_source, arch=arch_name, target='ptx')
            self.package_module = cuda.module_from_buffer(pkg_ptx)
        else:
            self.package_module = SourceModule(package_kernel_source, arch=arch_name)
        self.kernel_accel_package = self.package_module.get_function("KernelComputeAccelerationsPackage")
        self.kernel_accel_package_target = self.package_module.get_function("KernelComputeAccelerationsPackageTarget")
        self.kernel_integrate_package_symplectic = self.package_module.get_function("KernelIntegratePackage")
        self.kernel_integrate_package_simple = self.package_module.get_function("KernelIntegratePackageSimpleEuler")
        # Shared package helpers
        self.kernel_save_package_state = self.package_module.get_function("KernelSavePackageState")
        self.kernel_writeback_package_state = self.package_module.get_function("KernelWritebackPackageState")
        # RK4 package kernels (target-buffer pattern)
        self.kernel_rk4_set_target_pos_package = self.package_module.get_function("KernelRK4SetTargetPosPackage")
        self.kernel_rk4_final_package = self.package_module.get_function("KernelRK4FinalIntegrationPackage")
        # Velocity Verlet package kernels
        self.kernel_vv_position_package = self.package_module.get_function("KernelVVPositionUpdatePackage")
        self.kernel_vv_velocity_package = self.package_module.get_function("KernelVVVelocityUpdatePackage")
        # Forest-Ruth package kernels
        self.kernel_fr_drift_package = self.package_module.get_function("KernelFRDriftPackage")
        self.kernel_fr_kick_package = self.package_module.get_function("KernelFRKickPackage")
        # Select active package Euler integration kernel based on method
        if integration_method == "simple_euler":
            self.kernel_integrate_package = self.kernel_integrate_package_simple
        else:  # default to symplectic for symplectic_euler. Unused for non-Euler methods
            self.kernel_integrate_package = self.kernel_integrate_package_symplectic

        logger.info(f"Integration method: {integration_method}")

        logger.info(f"CUDA kernels compiled successfully for {arch_name}")

        self.bodies_array = self._bodies_to_structured_array(bodies)

        # Create vec3 dtype for accelerations (matches CUDA vec3 struct)
        self.vec3_dtype = np.dtype([('x', self.float_dtype), ('y', self.float_dtype), ('z', self.float_dtype)])
        self.accelerations_array = np.zeros(self.N, dtype=self.vec3_dtype)

        # Allocate GPU memory with error handling
        logger.info(f"Allocating GPU memory for {self.N} bodies...")
        try:
            self.gpu_bodies = cuda.mem_alloc(self.bodies_array.nbytes)
            self.gpu_accelerations = cuda.mem_alloc(self.accelerations_array.nbytes)
            # Epoch-start accelerations a0 of ALL bodies, for the acceleration
            # predictor (predictor_order >= 2). Aligned with gpu_bodies. Uploaded
            # per step via set_source_accelerations. Zero-initialised so the
            # linear/frozen kernels (which never read it) are safe.
            self.gpu_source_accelerations = cuda.mem_alloc(self.accelerations_array.nbytes)
            # For Velocity Verlet, we need to store old accelerations
            if self.use_velocity_verlet:
                self.gpu_old_accelerations = cuda.mem_alloc(self.accelerations_array.nbytes)
                self.vv_initialized = False  # Track if initial accelerations computed
            else:
                self.gpu_old_accelerations = None
                self.vv_initialized = True
            # For RK4, we need 4 acceleration arrays + original positions/velocities
            if self.use_rk4:
                self.gpu_rk4_k1 = cuda.mem_alloc(self.accelerations_array.nbytes)
                self.gpu_rk4_k2 = cuda.mem_alloc(self.accelerations_array.nbytes)
                self.gpu_rk4_k3 = cuda.mem_alloc(self.accelerations_array.nbytes)
                self.gpu_rk4_k4 = cuda.mem_alloc(self.accelerations_array.nbytes)
                self.gpu_rk4_orig_pos = cuda.mem_alloc(self.accelerations_array.nbytes)
                self.gpu_rk4_orig_vel = cuda.mem_alloc(self.accelerations_array.nbytes)
            else:
                self.gpu_rk4_k1 = None
                self.gpu_rk4_k2 = None
                self.gpu_rk4_k3 = None
                self.gpu_rk4_k4 = None
                self.gpu_rk4_orig_pos = None
                self.gpu_rk4_orig_vel = None
        except (cuda.MemoryError, cuda.LogicError) as e:
            raise RuntimeError(
                f"GPU memory allocation failed for {self.N} bodies "
                f"({self.bodies_array.nbytes / 1e6:.1f} MB bodies + "
                f"{self.accelerations_array.nbytes / 1e6:.1f} MB accelerations required): {e}"
            )

        # Transfer initial data to GPU
        cuda.memcpy_htod(self.gpu_bodies, self.bodies_array)
        cuda.memcpy_htod(self.gpu_source_accelerations, self.accelerations_array)  # zero-init
        logger.info(f"GPU memory allocated: bodies={self.bodies_array.nbytes / 1024 / 1024:.2f} MB, "
                    f"accel={self.accelerations_array.nbytes / 1024 / 1024:.2f} MB")

        # Verify shared memory requirements for kernel
        # TILE_SIZE = threads_per_block, shared memory holds slim SourceBody
        # tile entries (id, mass, position): 40 bytes in float64, 20 in float32.
        device_props = self.device.get_attributes()
        max_shared_mem = device_props.get(cuda.device_attribute.MAX_SHARED_MEMORY_PER_BLOCK, 49152)

        self.tile_entry_bytes = 20 if use_float32 else 40
        required_shared_mem = threads_per_block * self.tile_entry_bytes

        if required_shared_mem > max_shared_mem:
            raise RuntimeError(
                f"Insufficient shared memory: need {required_shared_mem} bytes "
                f"but GPU only has {max_shared_mem} bytes per block. "
                f"Reduce threads_per_block (current: {threads_per_block}) or use float32."
            )

        logger.info(f"Shared memory: {required_shared_mem}/{max_shared_mem} bytes "
                    f"(TILE_SIZE={threads_per_block} bodies)")

    @property
    def n_bodies(self) -> int:
        return self.N

    @property
    def time_step(self) -> float:
        return self.dt

    def set_time_step(self, time_step: float) -> None:
        """Change dt without reinitializing the engine.

        dt is a per-launch kernel argument, not baked into compiled code, so
        adaptive timestepping must NOT trigger an engine rebuild.
        """
        self.dt = float(time_step)

    def update_bodies_from_arrays(
        self,
        positions: np.ndarray,
        velocities: np.ndarray,
        masses: np.ndarray,
        ids: np.ndarray,
    ) -> None:
        """Replace the body state in the existing GPU buffers (same N, no
        recompilation or reallocation)."""
        if len(ids) != self.N:
            raise ValueError(f"Body count mismatch: engine has {self.N}, got {len(ids)}")
        arr = self.bodies_array
        arr['bodyIdentifier'] = ids
        arr['bodyMass'] = masses
        arr['positionX'] = positions[:, 0]
        arr['positionY'] = positions[:, 1]
        arr['positionZ'] = positions[:, 2]
        arr['velocityX'] = velocities[:, 0]
        arr['velocityY'] = velocities[:, 1]
        arr['velocityZ'] = velocities[:, 2]
        cuda.memcpy_htod(self.gpu_bodies, arr)
        # Cached VV accelerations no longer match the replaced state
        if self.use_velocity_verlet:
            self.vv_initialized = False

    def get_state_arrays(self) -> Tuple[np.ndarray, np.ndarray]:
        """(positions, velocities) read back to the host as (N, 3) float64 arrays."""
        cuda.Context.synchronize()
        cuda.memcpy_dtoh(self.bodies_array, self.gpu_bodies)
        return self._rows_to_state(self.bodies_array)

    def _bodies_to_structured_array(self, bodies: List[Dict]) -> np.ndarray:
        """Convert list of body dicts to NumPy structured array matching BodyState.

        align=True gives the CUDA BodyState layout: 64 bytes in float64 (4
        bytes of padding after the int32), 32 in float32. Without it the GPU
        would index the wrong memory.
        """
        dtype = np.dtype([
            ('bodyIdentifier', np.int32),
            ('bodyMass', self.float_dtype),
            ('positionX', self.float_dtype),
            ('positionY', self.float_dtype),
            ('positionZ', self.float_dtype),
            ('velocityX', self.float_dtype),
            ('velocityY', self.float_dtype),
            ('velocityZ', self.float_dtype),
        ], align=True)

        arr = np.zeros(len(bodies), dtype=dtype)
        for i, body in enumerate(bodies):
            arr[i]['bodyIdentifier'] = body['idBody']
            arr[i]['bodyMass'] = body['m']
            arr[i]['positionX'] = body['x']
            arr[i]['positionY'] = body['y']
            arr[i]['positionZ'] = body['z']
            arr[i]['velocityX'] = body['vx']
            arr[i]['velocityY'] = body['vy']
            arr[i]['velocityZ'] = body['vz']

        return arr

    def get_last_accelerations(self) -> np.ndarray:
        """Read the most-recently-computed accelerations buffer back to host.

        Whichever buffer is "current" depends on the method and where in
        the step we are. Callers expect this to be called right after
        simulate_single_step() — at that point:
          - Euler:           gpu_accelerations holds a(x_old)
          - Velocity Verlet: gpu_old_accelerations holds the just-computed
                              a(x_new) (swap happens at the end of the step)
          - RK4:             gpu_rk4_k4 holds a(pos_k4)
          - Forest-Ruth:     gpu_accelerations holds a(pos3) from the last kick

        Returns an (N, 3) float64 ndarray.
        """
        if self.use_rk4:
            src = self.gpu_rk4_k4
        elif self.use_velocity_verlet:
            # After the per-step swap, gpu_old_accelerations IS the "new" buffer.
            src = self.gpu_old_accelerations
        else:
            # Euler and Forest-Ruth both end with gpu_accelerations populated.
            src = self.gpu_accelerations

        cuda.Context.synchronize()
        return self._vec3_to_host(src, self.N)

    def _vec3_to_host(self, buf, n: int) -> np.ndarray:
        """n vec3 entries of a device buffer as an (n, 3) float64 array."""
        host = np.empty(n, dtype=self.vec3_dtype)
        cuda.memcpy_dtoh(host, buf)
        return np.stack([host['x'], host['y'], host['z']], axis=1).astype(np.float64, copy=False)

    @staticmethod
    def _rows_to_state(rows: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
        """(positions, velocities) of BodyState rows as (n, 3) float64 arrays."""
        positions = np.stack([rows['positionX'], rows['positionY'], rows['positionZ']],
                             axis=1).astype(np.float64, copy=False)
        velocities = np.stack([rows['velocityX'], rows['velocityY'], rows['velocityZ']],
                              axis=1).astype(np.float64, copy=False)
        return positions, velocities

    def simulate_single_step(self):
        """Run one physics timestep on GPU."""
        # Dynamic shared memory: TILE_SIZE * sizeof(SourceBody)
        # (slim tile entries: 40 bytes float64, 20 bytes float32)
        launch = dict(block=(self.threads_per_block, 1, 1), grid=(self.num_blocks, 1, 1))
        shared = self.threads_per_block * self.tile_entry_bytes

        def accel(out):
            """a(x) of every body, into out."""
            self.kernel_accel(
                self.gpu_bodies, out,
                np.int32(self.N), self.float_dtype(self.G), self.float_dtype(self.eps2),
                shared=shared, **launch
            )

        if self.use_rk4:
            self._step_rk4(accel, launch)
        elif self.use_velocity_verlet:
            self._step_velocity_verlet(accel, launch)
        elif self.use_forest_ruth:
            self._step_forest_ruth(accel, launch)
        else:
            # Euler: accelerations, then the update, one launch each
            accel(self.gpu_accelerations)
            self.kernel_integrate(
                self.gpu_bodies, self.gpu_accelerations,
                np.int32(self.N), self.float_dtype(self.dt),
                **launch
            )

    def _step_rk4(self, accel, launch) -> None:
        """RK4, four force evaluations per step."""
        n, dt = np.int32(self.N), self.float_dtype(self.dt)
        orig_pos, orig_vel = self.gpu_rk4_orig_pos, self.gpu_rk4_orig_vel

        self.kernel_rk4_save_state(self.gpu_bodies, orig_pos, orig_vel, n, **launch)
        # k1 at the current positions
        accel(self.gpu_rk4_k1)
        # Each stage's positions from the original state and an earlier stage's
        # accelerations: stage 3 needs k1 (not k2) because
        # pos_k3 = x_0 + dt/2 * (v_0 + dt/2 * a_1), and stage 4 needs k2 (not
        # k3) because pos_k4 = x_0 + dt * (v_0 + dt/2 * a_2).
        for stage, prev, out in ((2, self.gpu_rk4_k1, self.gpu_rk4_k2),
                                 (3, self.gpu_rk4_k1, self.gpu_rk4_k3),
                                 (4, self.gpu_rk4_k2, self.gpu_rk4_k4)):
            self.kernel_rk4_set_intermediate(self.gpu_bodies, orig_pos, orig_vel, prev,
                                             n, dt, np.int32(stage), **launch)
            accel(out)
        self.kernel_rk4_final(
            self.gpu_bodies, orig_pos, orig_vel,
            self.gpu_rk4_k1, self.gpu_rk4_k2, self.gpu_rk4_k3, self.gpu_rk4_k4,
            n, dt, **launch
        )

    def _step_velocity_verlet(self, accel, launch) -> None:
        """Velocity Verlet. The accelerations carry over between steps, so the
        first step computes a(x_0) first."""
        n, dt = np.int32(self.N), self.float_dtype(self.dt)
        if not self.vv_initialized:
            accel(self.gpu_old_accelerations)
            self.vv_initialized = True
        # x_{t+1} = x_t + v_t * dt + 0.5 * a_t * dt^2
        self.kernel_vv_position(self.gpu_bodies, self.gpu_old_accelerations, n, dt, **launch)
        accel(self.gpu_accelerations)
        # v_{t+1} = v_t + 0.5 * (a_t + a_{t+1}) * dt
        self.kernel_vv_velocity(self.gpu_bodies, self.gpu_old_accelerations, self.gpu_accelerations,
                                n, dt, **launch)
        # the new accelerations are the next step's old ones
        self.gpu_old_accelerations, self.gpu_accelerations = self.gpu_accelerations, self.gpu_old_accelerations

    def _step_forest_ruth(self, accel, launch) -> None:
        """Forest-Ruth (Yoshida 4th-order symplectic), 3 force evaluations:
        D(c1) K(d1) D(c2) K(d2) D(c2) K(d1) D(c1), the coefficients
        constants.FR_* pushed to the kernels as scaled dt."""
        n, dt = np.int32(self.N), self.dt

        def drift(scale_dt):
            self.kernel_fr_drift(self.gpu_bodies, n, self.float_dtype(scale_dt), **launch)

        def kick(scale_dt):
            accel(self.gpu_accelerations)
            self.kernel_fr_kick(self.gpu_bodies, self.gpu_accelerations, n, self.float_dtype(scale_dt), **launch)

        drift(FR_C1 * dt)
        kick(FR_D1 * dt)
        drift(FR_C2 * dt)
        kick(FR_D2 * dt)
        drift(FR_C2 * dt)
        kick(FR_D1 * dt)
        drift(FR_C1 * dt)

    def set_source_accelerations(self, accelerations: np.ndarray) -> None:
        """Upload epoch-start accelerations a0 of ALL bodies for the acceleration
        predictor (predictor_order >= 2).

        `accelerations` is (N, 3) in the SAME body order as gpu_bodies (i.e. the
        full-system order the engine was initialised with). Read by
        KernelComputeAccelerationsPackageTarget for background rows only.
        """
        if accelerations.shape[0] != self.N:
            raise ValueError(
                f"source accelerations N={accelerations.shape[0]} != engine N={self.N}"
            )
        arr = np.zeros(self.N, dtype=self.vec3_dtype)
        arr['x'] = accelerations[:, 0]
        arr['y'] = accelerations[:, 1]
        arr['z'] = accelerations[:, 2]
        cuda.memcpy_htod(self.gpu_source_accelerations, arr)

    def simulate_package_step(self, start_idx: int, package_size: int, predictor_order: int = 1):
        """Run one physics timestep for a package of K bodies.

        Returns (positions, velocities, accelerations) of the package as
        (K, 3) float64 arrays, read back from its K rows only.

        predictor_order selects the background predictor used at each intermediate
        force evaluation (0 frozen / 1 linear x+tau*v / 2 accel +0.5*tau^2*a),
        matching CPU _stage_source_positions. Order 2 requires a0 to have been
        uploaded via set_source_accelerations().

        For methods with intermediate force evaluations (RK4, Velocity Verlet,
        Forest-Ruth), all_bodies is NOT mutated until the final writeback.
        Intermediate package positions live in a separate target buffer. The
        source positions read for each evaluation are drift-corrected: package
        rows use their current stage position and background rows are predicted
        to the stage time at predictor_order. This matches CPU's
        step_package_* / _stage_source_positions behavior.

        The accelerations returned are those of the method's last force
        evaluation:
          - Euler: a(x_old)
          - Velocity Verlet: a(x_new)
          - RK4: a(pos_k4) (the k4 evaluation)
          - Forest-Ruth: a(pos3) (the last kick's evaluation)
        These are all "close-to-current" accelerations suitable for
        adaptive-dt heuristic (softening-length criterion) without an extra
        force evaluation.
        """
        if start_idx + package_size > self.N:
            raise ValueError(
                f"Package exceeds body count: start={start_idx}, size={package_size}, total={self.N}"
            )

        K = package_size
        vec3_buf_nbytes = K * self.vec3_dtype.itemsize
        package_num_blocks = (K + self.threads_per_block - 1) // self.threads_per_block

        # Scratch buffers, reused across calls: allocating and freeing device
        # memory synchronises the device, on every task. They are regrown when
        # a larger package comes (K varies: the last package is shorter).
        if self._scratch_nbytes < vec3_buf_nbytes:
            self._free_scratch()
            self._scratch_nbytes = vec3_buf_nbytes
        used = []

        def alloc():
            if len(used) == len(self._scratch):
                self._scratch.append(cuda.mem_alloc(self._scratch_nbytes))
            buf = self._scratch[len(used)]
            used.append(buf)
            return buf

        p = _PackageLaunch(
            start=np.int32(start_idx), k=np.int32(K), start_idx=start_idx, nbytes=vec3_buf_nbytes,
            launch=dict(block=(self.threads_per_block, 1, 1), grid=(package_num_blocks, 1, 1)),
            shared=self.threads_per_block * self.tile_entry_bytes,
            order=np.int32(predictor_order), alloc=alloc)

        try:
            if self.use_rk4:
                last_accel_gpu = self._package_rk4(p)
            elif self.use_velocity_verlet:
                last_accel_gpu = self._package_velocity_verlet(p)
            elif self.use_forest_ruth:
                last_accel_gpu = self._package_forest_ruth(p)
            else:
                # Euler (simple or symplectic): single accel + integrate step,
                # in-place mutation of all_bodies (no intermediate accel).
                last_accel_gpu = alloc()
                self.kernel_accel_package(
                    self.gpu_bodies, last_accel_gpu,
                    np.int32(self.N), p.start, p.k,
                    self.float_dtype(self.G), self.float_dtype(self.eps2),
                    shared=p.shared, **p.launch,
                )
                self.kernel_integrate_package(self.gpu_bodies, last_accel_gpu, p.start, p.k,
                                              self.float_dtype(self.dt), **p.launch)

            # Ensure all GPU operations complete before reading results
            cuda.Context.synchronize()

            # Copy the last-stage accelerations to the host now: the scratch
            # buffer is reused by the next call.
            accel_array = self._vec3_to_host(last_accel_gpu, K)
            rows = np.empty(K, dtype=self.bodies_array.dtype)
            cuda.memcpy_dtoh(rows, int(self.gpu_bodies) + start_idx * self.bodies_array.dtype.itemsize)
            positions, velocities = self._rows_to_state(rows)
            return positions, velocities, accel_array

        except BaseException:
            self._free_scratch()   # e.g. a CUDA error: don't reuse buffers in an unknown state
            raise

    def _package_accel(self, p: _PackageLaunch, target_pos, out, tau: float) -> None:
        """a(target_pos) of the package into out, the background sources
        predicted to the stage time tau."""
        self.kernel_accel_package_target(
            self.gpu_bodies, target_pos, self.gpu_source_accelerations, out,
            np.int32(self.N), p.start, p.k,
            self.float_dtype(self.G), self.float_dtype(self.eps2),
            self.float_dtype(tau), p.order,
            shared=p.shared, **p.launch,
        )

    def _package_rk4(self, p: _PackageLaunch):
        """RK4 on a target buffer: gpu_bodies keeps the original state until the
        final kernel writes the new positions and velocities. Returns k4."""
        orig_pos, orig_vel = p.alloc(), p.alloc()
        k1, k2, k3, k4 = p.alloc(), p.alloc(), p.alloc(), p.alloc()
        target_pos = p.alloc()
        dt = self.float_dtype(self.dt)

        self.kernel_save_package_state(self.gpu_bodies, orig_pos, orig_vel, p.start, p.k, **p.launch)
        # k1 is evaluated at x_0, stage time 0
        cuda.memcpy_dtod(target_pos, orig_pos, p.nbytes)
        self._package_accel(p, target_pos, k1, 0.0)
        # The stage targets as in the full-system RK4: stage 2 (whose
        # accelerations argument is unused) and stage 3 from k1, stage 4 from k2
        for stage, prev, out, tau in ((2, k1, k2, 0.5 * self.dt), (3, k1, k3, 0.5 * self.dt),
                                      (4, k2, k4, self.dt)):
            self.kernel_rk4_set_target_pos_package(orig_pos, orig_vel, prev, target_pos,
                                                   p.k, dt, np.int32(stage), **p.launch)
            self._package_accel(p, target_pos, out, tau)
        self.kernel_rk4_final_package(self.gpu_bodies, orig_pos, orig_vel, k1, k2, k3, k4,
                                      p.start, p.k, dt, **p.launch)
        return k4

    def _package_velocity_verlet(self, p: _PackageLaunch):
        """Velocity Verlet for one package step, as the CPU's
        step_package_velocity_verlet: no acceleration is cached across calls.
        Returns a(x_new)."""
        orig_pos, orig_vel = p.alloc(), p.alloc()
        old_accel, new_accel = p.alloc(), p.alloc()
        target_pos = p.alloc()
        dt = self.float_dtype(self.dt)

        self.kernel_save_package_state(self.gpu_bodies, orig_pos, orig_vel, p.start, p.k, **p.launch)
        cuda.memcpy_dtod(target_pos, orig_pos, p.nbytes)

        # old_accel = a(x_0). With the acceleration predictor, a(x_0) of ALL
        # bodies is already on the device (set_source_accelerations: shipped
        # by the merger or recomputed), so this package's rows are its old
        # acceleration: one force evaluation per task instead of two (as on
        # the CPU backend).
        if int(p.order) >= 2:
            cuda.memcpy_dtod(
                old_accel,
                int(self.gpu_source_accelerations) + p.start_idx * self.vec3_dtype.itemsize,
                p.nbytes,
            )
        else:
            self._package_accel(p, target_pos, old_accel, 0.0)

        # target_pos = x_0 + v_0*dt + 0.5 * a_old * dt^2
        self.kernel_vv_position_package(orig_pos, orig_vel, old_accel, target_pos, p.k, dt, **p.launch)
        # new_accel = a(target_pos), the background drifted to tau = dt
        self._package_accel(p, target_pos, new_accel, self.dt)
        # v_new = v_0 + 0.5*(a_old + a_new)*dt, writeback pos+vel
        self.kernel_vv_velocity_package(self.gpu_bodies, orig_vel, old_accel, new_accel, target_pos,
                                        p.start, p.k, dt, **p.launch)
        return new_accel

    def _package_forest_ruth(self, p: _PackageLaunch):
        """Forest-Ruth on target buffers of the package's positions and
        velocities: gpu_bodies is only touched by the writeback. Returns the
        last kick's accelerations."""
        target_pos, target_vel, accel = p.alloc(), p.alloc(), p.alloc()
        dt = self.dt

        self.kernel_save_package_state(self.gpu_bodies, target_pos, target_vel, p.start, p.k, **p.launch)

        def drift(scale_dt):
            self.kernel_fr_drift_package(target_pos, target_vel, p.k, self.float_dtype(scale_dt), **p.launch)

        def kick(scale_dt, tau):
            # tau: the drift time reached before this kick, the stage time the
            # background sources are predicted to
            self._package_accel(p, target_pos, accel, tau)
            self.kernel_fr_kick_package(target_vel, accel, p.k, self.float_dtype(scale_dt), **p.launch)

        drift(FR_C1 * dt)
        kick(FR_D1 * dt, FR_TAU1 * dt)
        drift(FR_C2 * dt)
        kick(FR_D2 * dt, FR_TAU2 * dt)
        drift(FR_C2 * dt)
        kick(FR_D1 * dt, FR_TAU3 * dt)
        drift(FR_C1 * dt)

        self.kernel_writeback_package_state(self.gpu_bodies, target_pos, target_vel, p.start, p.k, **p.launch)
        return accel

    _scratch = ()          # an instance list once the first package step runs
    _scratch_nbytes = 0

    def _free_scratch(self) -> None:
        # detached first, so a failing free (e.g. after a CUDA error) can
        # leave no freed buffer in the list for the next call to reuse
        bufs, self._scratch, self._scratch_nbytes = self._scratch, [], 0
        for buf in bufs:
            try:
                buf.free()
            except Exception as e:  # noqa: BLE001 — the context may be broken
                logger.warning(f"Could not free a GPU scratch buffer: {e}")

    _DEVICE_BUFFERS = ('gpu_bodies', 'gpu_accelerations', 'gpu_source_accelerations',
                       'gpu_old_accelerations', 'gpu_rk4_k1', 'gpu_rk4_k2', 'gpu_rk4_k3',
                       'gpu_rk4_k4', 'gpu_rk4_orig_pos', 'gpu_rk4_orig_vel')

    def cleanup(self):
        """Free GPU memory (idempotent: freed buffers are set to None)."""
        for name in self._DEVICE_BUFFERS:
            buf = getattr(self, name, None)
            if buf is not None:
                buf.free()
                setattr(self, name, None)
        self._free_scratch()
        logger.info("GPU memory freed")

    def __del__(self):
        """Cleanup on destruction."""
        try:
            self.cleanup()
        except Exception:  # noqa: BLE001 — the context may already be gone
            pass


def is_gpu_available() -> bool:
    """Check if GPU is available for computation."""
    if not PYCUDA_AVAILABLE:
        return False

    try:
        cuda.init()
        return cuda.Device.count() > 0
    except Exception as e:
        logger.debug(f"GPU not available: {e}")
        return False


def get_gpu_info() -> Optional[Dict]:
    """Get GPU device information."""
    if not is_gpu_available():
        return None

    try:
        device = cuda.Device(0)
        return {
            'name': device.name(),
            'compute_capability': device.compute_capability(),
            'total_memory_mb': device.total_memory() / 1024 / 1024,
        }
    except Exception as e:
        logger.error(f"Failed to get GPU info: {e}")
        return None
