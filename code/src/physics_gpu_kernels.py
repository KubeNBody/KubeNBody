# SPDX-FileCopyrightText: 2026 Piotr Grzesik, Dariusz R. Augustyn, Łukasz Wyciślik
# SPDX-License-Identifier: Apache-2.0
"""CUDA C source of the GPU engine's kernels (physics_gpu.GPUNBodyEngine): the
whole-system module and the package module, generated for float64 or float32."""

from string import Template


# CUDA source shared by both kernel modules: precision typedefs (the $real_type
# and $vec_type placeholders are substituted with the module's template), the
# body structs, and the force accumulation over one shared-memory source tile.
_CUDA_COMMON_SOURCE = """
#include <cuda_runtime.h>

// Precision configured at runtime
typedef $real_type real;
typedef $vec_type vec3;

__device__ __host__ inline vec3 MakeVec3(real x, real y, real z) {
    vec3 v;
    v.x = x;
    v.y = y;
    v.z = z;
    return v;
}

// Body state structure matching our CSV format
struct BodyState {
    int bodyIdentifier;
    real bodyMass;
    real positionX, positionY, positionZ;
    real velocityX, velocityY, velocityZ;
};

// Slim shared-memory tile entry: force evaluation only needs id, mass and
// position. Full BodyState is 64B in float64, this is 40B — lower shared
// memory pressure per block means better occupancy and less bandwidth.
struct SourceBody {
    int bodyIdentifier;
    real bodyMass;
    real positionX, positionY, positionZ;
};

// Add the acceleration on one target from the first tileSize entries of the
// shared-memory source tile to (accelX, accelY, accelZ). Sources with the
// target's ID (itself) are skipped. The caller loads the tile and places the
// __syncthreads() around this call. Only active threads call it.
__device__ __forceinline__ void AccumulateSourceTile(
    const SourceBody* sourceTile,
    const int tileSize,
    const int myId,
    const real myX, const real myY, const real myZ,
    const real gravitationalConstant,
    const real softeningDistanceSquared,
    real& accelX, real& accelY, real& accelZ)
{
    for (int j = 0; j < tileSize; j++) {
        const SourceBody sourceBody = sourceTile[j];

        // Skip self-interaction
        if (myId == sourceBody.bodyIdentifier) continue;

        const real dx = sourceBody.positionX - myX;
        const real dy = sourceBody.positionY - myY;
        const real dz = sourceBody.positionZ - myZ;

        const real distSq = dx*dx + dy*dy + dz*dz + softeningDistanceSquared;
        const real invDist = (real)1.0 / sqrt(distSq);
        const real invDistCubed = invDist * invDist * invDist;
        const real scale = gravitationalConstant * sourceBody.bodyMass * invDistCubed;

        accelX += scale * dx;
        accelY += scale * dy;
        accelZ += scale * dz;
    }
}
"""


def generate_cuda_kernel_source(use_float32: bool = False) -> str:
    """The CUDA kernel source in float32 or float64."""
    real_type = "float" if use_float32 else "double"
    vec_type = "float3" if use_float32 else "double3"

    kernel_template = Template(_CUDA_COMMON_SOURCE + """
__global__ void KernelComputeAccelerations(
    BodyState* __restrict__ bodies,
    vec3* __restrict__ accelerations,
    const int N,
    const real gravitationalConstant,
    const real softeningDistanceSquared)
{
    // Dynamic shared memory sized to blockDim.x (slim SourceBody entries)
    extern __shared__ SourceBody sourceTile[];
    const int TILE_SIZE = blockDim.x;

    // Each thread handles one target body
    const int tid = blockIdx.x * blockDim.x + threadIdx.x;
    const bool active = (tid < N);

    // Load my target body (or dummy if inactive)
    BodyState myBody;
    if (active) {
        myBody = bodies[tid];
    }

    // Accumulate acceleration in registers (no atomics!)
    real accelX = (real)0;
    real accelY = (real)0;
    real accelZ = (real)0;

    // Loop over source tiles
    const int numTiles = (N + TILE_SIZE - 1) / TILE_SIZE;
    for (int tileIdx = 0; tileIdx < numTiles; tileIdx++) {
        const int tileStart = tileIdx * TILE_SIZE;

        // Cooperatively load source tile - ALL threads participate
        // Each thread loads one element (efficient coalesced access)
        const int loadIdx = tileStart + threadIdx.x;
        if (loadIdx < N) {
            const BodyState b = bodies[loadIdx];
            sourceTile[threadIdx.x].bodyIdentifier = b.bodyIdentifier;
            sourceTile[threadIdx.x].bodyMass = b.bodyMass;
            sourceTile[threadIdx.x].positionX = b.positionX;
            sourceTile[threadIdx.x].positionY = b.positionY;
            sourceTile[threadIdx.x].positionZ = b.positionZ;
        }
        __syncthreads();  // All threads reach here (no early returns)

        // Compute tile size for this iteration
        const int tileEnd = min(tileStart + TILE_SIZE, N);
        const int tileSize = tileEnd - tileStart;

        // Accumulate forces from this tile (only active threads compute)
        if (active) {
            AccumulateSourceTile(sourceTile, tileSize, myBody.bodyIdentifier,
                                 myBody.positionX, myBody.positionY, myBody.positionZ,
                                 gravitationalConstant, softeningDistanceSquared,
                                 accelX, accelY, accelZ);
        }
        __syncthreads();  // All threads reach here (no early returns)
    }

    // Write result (only active threads write)
    if (active) {
        accelerations[tid].x = accelX;
        accelerations[tid].y = accelY;
        accelerations[tid].z = accelZ;
    }
}

// Kernel 2: Integrate target bodies using symplectic Euler
__global__ void KernelIntegrateTargetBodiesSymplecticEuler(
    BodyState* __restrict__ targetBodiesToUpdate,
    const vec3* __restrict__ finalAccelerationsPerTarget,
    int numTargetBodies,
    real timeStepSeconds)
{
    const int globalTargetIndex = blockIdx.x * blockDim.x + threadIdx.x;
    if (globalTargetIndex >= numTargetBodies) return;

    BodyState body = targetBodiesToUpdate[globalTargetIndex];

    // v_{t+1} = v_t + a_t * dt
    body.velocityX += finalAccelerationsPerTarget[globalTargetIndex].x * timeStepSeconds;
    body.velocityY += finalAccelerationsPerTarget[globalTargetIndex].y * timeStepSeconds;
    body.velocityZ += finalAccelerationsPerTarget[globalTargetIndex].z * timeStepSeconds;

    // x_{t+1} = x_t + v_{t+1} * dt
    body.positionX += body.velocityX * timeStepSeconds;
    body.positionY += body.velocityY * timeStepSeconds;
    body.positionZ += body.velocityZ * timeStepSeconds;

    targetBodiesToUpdate[globalTargetIndex] = body;
}

// Kernel 3: Integrate target bodies using simple (explicit) Euler
__global__ void KernelIntegrateTargetBodiesSimpleEuler(
    BodyState* __restrict__ targetBodiesToUpdate,
    const vec3* __restrict__ finalAccelerationsPerTarget,
    int numTargetBodies,
    real timeStepSeconds)
{
    const int globalTargetIndex = blockIdx.x * blockDim.x + threadIdx.x;
    if (globalTargetIndex >= numTargetBodies) return;

    BodyState body = targetBodiesToUpdate[globalTargetIndex];

    // Simple Euler: position uses OLD velocity
    // x_{t+1} = x_t + v_t * dt
    body.positionX += body.velocityX * timeStepSeconds;
    body.positionY += body.velocityY * timeStepSeconds;
    body.positionZ += body.velocityZ * timeStepSeconds;

    // v_{t+1} = v_t + a_t * dt
    body.velocityX += finalAccelerationsPerTarget[globalTargetIndex].x * timeStepSeconds;
    body.velocityY += finalAccelerationsPerTarget[globalTargetIndex].y * timeStepSeconds;
    body.velocityZ += finalAccelerationsPerTarget[globalTargetIndex].z * timeStepSeconds;

    targetBodiesToUpdate[globalTargetIndex] = body;
}

// Kernel 4: Velocity Verlet position update (first half-step)
// x_{t+1} = x_t + v_t * dt + 0.5 * a_t * dt^2
__global__ void KernelVelocityVerletPositionUpdate(
    BodyState* __restrict__ bodies,
    const vec3* __restrict__ oldAccelerations,
    int numBodies,
    real timeStepSeconds)
{
    const int idx = blockIdx.x * blockDim.x + threadIdx.x;
    if (idx >= numBodies) return;

    BodyState body = bodies[idx];
    const real dt = timeStepSeconds;
    const real dt_sq_half = (real)0.5 * dt * dt;

    body.positionX += body.velocityX * dt + oldAccelerations[idx].x * dt_sq_half;
    body.positionY += body.velocityY * dt + oldAccelerations[idx].y * dt_sq_half;
    body.positionZ += body.velocityZ * dt + oldAccelerations[idx].z * dt_sq_half;

    bodies[idx] = body;
}

// Kernel 5: Velocity Verlet velocity update (using average of old and new accelerations)
// v_{t+1} = v_t + 0.5 * (a_t + a_{t+1}) * dt
__global__ void KernelVelocityVerletVelocityUpdate(
    BodyState* __restrict__ bodies,
    const vec3* __restrict__ oldAccelerations,
    const vec3* __restrict__ newAccelerations,
    int numBodies,
    real timeStepSeconds)
{
    const int idx = blockIdx.x * blockDim.x + threadIdx.x;
    if (idx >= numBodies) return;

    BodyState body = bodies[idx];
    const real dt_half = (real)0.5 * timeStepSeconds;

    body.velocityX += (oldAccelerations[idx].x + newAccelerations[idx].x) * dt_half;
    body.velocityY += (oldAccelerations[idx].y + newAccelerations[idx].y) * dt_half;
    body.velocityZ += (oldAccelerations[idx].z + newAccelerations[idx].z) * dt_half;

    bodies[idx] = body;
}

// =============================================================================
// RK4 (4th-order Runge-Kutta) Kernels
// =============================================================================

// Kernel 6: Save original body state for RK4
__global__ void KernelRK4SaveState(
    const BodyState* __restrict__ bodies,
    vec3* __restrict__ originalPositions,
    vec3* __restrict__ originalVelocities,
    int numBodies)
{
    const int idx = blockIdx.x * blockDim.x + threadIdx.x;
    if (idx >= numBodies) return;

    BodyState body = bodies[idx];
    originalPositions[idx] = MakeVec3(body.positionX, body.positionY, body.positionZ);
    originalVelocities[idx] = MakeVec3(body.velocityX, body.velocityY, body.velocityZ);
}

// Kernel 7: Set intermediate positions for RK4 k2, k3, k4 stages.
//
// Standard RK4 for dz/dt = f(z) with z = (x, v):
//   k_i = f(z_0 + scale_i * k_{i-1})
// where the position-component of k_{i-1} (call it k_{i-1}_x) is what we
// use to advance position for the i-th evaluation. Specifically:
//   k1_x = v_0
//   k2_x = v_0 + (dt/2) * a_1
//   k3_x = v_0 + (dt/2) * a_2
// and the evaluation points are:
//   pos for k2 eval = x_0 + (dt/2) * k1_x = x_0 + (dt/2) * v_0
//   pos for k3 eval = x_0 + (dt/2) * k2_x = x_0 + (dt/2) * (v_0 + (dt/2)*a_1)
//   pos for k4 eval = x_0 +    dt   * k3_x = x_0 +    dt   * (v_0 + (dt/2)*a_2)
//
// Therefore the caller must pass:
//   stage 2 -> prevAccelerations unused (any buffer is fine)
//   stage 3 -> prevAccelerations = k1 (NOT k2)
//   stage 4 -> prevAccelerations = k2 (NOT k3)
__global__ void KernelRK4SetIntermediateState(
    BodyState* __restrict__ bodies,
    const vec3* __restrict__ originalPositions,
    const vec3* __restrict__ originalVelocities,
    const vec3* __restrict__ prevAccelerations,
    int numBodies,
    real timeStepSeconds,
    int stage)
{
    const int idx = blockIdx.x * blockDim.x + threadIdx.x;
    if (idx >= numBodies) return;

    BodyState body = bodies[idx];
    vec3 origPos = originalPositions[idx];
    vec3 origVel = originalVelocities[idx];

    const real dt = timeStepSeconds;
    const real half_dt = (real)0.5 * dt;

    if (stage == 2) {
        // pos for k2 = x_0 + (dt/2) * v_0
        body.positionX = origPos.x + half_dt * origVel.x;
        body.positionY = origPos.y + half_dt * origVel.y;
        body.positionZ = origPos.z + half_dt * origVel.z;
    } else if (stage == 3) {
        // pos for k3 = x_0 + (dt/2) * (v_0 + (dt/2) * a_1)
        vec3 a1 = prevAccelerations[idx];
        body.positionX = origPos.x + half_dt * (origVel.x + half_dt * a1.x);
        body.positionY = origPos.y + half_dt * (origVel.y + half_dt * a1.y);
        body.positionZ = origPos.z + half_dt * (origVel.z + half_dt * a1.z);
    } else {  // stage == 4
        // pos for k4 = x_0 + dt * (v_0 + (dt/2) * a_2)
        vec3 a2 = prevAccelerations[idx];
        body.positionX = origPos.x + dt * (origVel.x + half_dt * a2.x);
        body.positionY = origPos.y + dt * (origVel.y + half_dt * a2.y);
        body.positionZ = origPos.z + dt * (origVel.z + half_dt * a2.z);
    }

    bodies[idx] = body;
}

// =============================================================================
// Forest-Ruth (Yoshida 4th-order symplectic) Kernels — monolithic
// =============================================================================

// Drift: x += scale_dt * v   (scale_dt = coefficient * dt, computed on host)
__global__ void KernelForestRuthDrift(
    BodyState* __restrict__ bodies,
    int numBodies,
    real scale_dt)
{
    const int idx = blockIdx.x * blockDim.x + threadIdx.x;
    if (idx >= numBodies) return;
    BodyState body = bodies[idx];
    body.positionX += scale_dt * body.velocityX;
    body.positionY += scale_dt * body.velocityY;
    body.positionZ += scale_dt * body.velocityZ;
    bodies[idx] = body;
}

// Kick: v += scale_dt * a
__global__ void KernelForestRuthKick(
    BodyState* __restrict__ bodies,
    const vec3* __restrict__ accelerations,
    int numBodies,
    real scale_dt)
{
    const int idx = blockIdx.x * blockDim.x + threadIdx.x;
    if (idx >= numBodies) return;
    BodyState body = bodies[idx];
    vec3 a = accelerations[idx];
    body.velocityX += scale_dt * a.x;
    body.velocityY += scale_dt * a.y;
    body.velocityZ += scale_dt * a.z;
    bodies[idx] = body;
}

// Kernel 8: Final RK4 integration
// x_new = x_orig + (k1_x + 2*k2_x + 2*k3_x + k4_x) * dt/6
// v_new = v_orig + (k1_v + 2*k2_v + 2*k3_v + k4_v) * dt/6
// where k_x = velocity at that stage, k_v = acceleration at that stage
__global__ void KernelRK4FinalIntegration(
    BodyState* __restrict__ bodies,
    const vec3* __restrict__ originalPositions,
    const vec3* __restrict__ originalVelocities,
    const vec3* __restrict__ k1_accel,
    const vec3* __restrict__ k2_accel,
    const vec3* __restrict__ k3_accel,
    const vec3* __restrict__ k4_accel,
    int numBodies,
    real timeStepSeconds)
{
    const int idx = blockIdx.x * blockDim.x + threadIdx.x;
    if (idx >= numBodies) return;

    vec3 origPos = originalPositions[idx];
    vec3 origVel = originalVelocities[idx];
    vec3 a1 = k1_accel[idx];
    vec3 a2 = k2_accel[idx];
    vec3 a3 = k3_accel[idx];
    vec3 a4 = k4_accel[idx];

    real dt = timeStepSeconds;
    real dt_sixth = dt / (real)6.0;
    real dt_half = dt * (real)0.5;

    // Compute k velocities (derivatives of position)
    // k1_x = v_orig
    // k2_x = v_orig + k1_v * dt/2 = v_orig + a1 * dt/2
    // k3_x = v_orig + k2_v * dt/2 = v_orig + a2 * dt/2
    // k4_x = v_orig + k3_v * dt = v_orig + a3 * dt
    vec3 k1_x = origVel;
    vec3 k2_x = MakeVec3(origVel.x + a1.x * dt_half, origVel.y + a1.y * dt_half, origVel.z + a1.z * dt_half);
    vec3 k3_x = MakeVec3(origVel.x + a2.x * dt_half, origVel.y + a2.y * dt_half, origVel.z + a2.z * dt_half);
    vec3 k4_x = MakeVec3(origVel.x + a3.x * dt, origVel.y + a3.y * dt, origVel.z + a3.z * dt);

    // Final position: x_new = x_orig + (k1_x + 2*k2_x + 2*k3_x + k4_x) * dt/6
    BodyState body = bodies[idx];
    body.positionX = origPos.x + (k1_x.x + (real)2.0*k2_x.x + (real)2.0*k3_x.x + k4_x.x) * dt_sixth;
    body.positionY = origPos.y + (k1_x.y + (real)2.0*k2_x.y + (real)2.0*k3_x.y + k4_x.y) * dt_sixth;
    body.positionZ = origPos.z + (k1_x.z + (real)2.0*k2_x.z + (real)2.0*k3_x.z + k4_x.z) * dt_sixth;

    // Final velocity: v_new = v_orig + (k1_v + 2*k2_v + 2*k3_v + k4_v) * dt/6
    // where k_v = acceleration
    body.velocityX = origVel.x + (a1.x + (real)2.0*a2.x + (real)2.0*a3.x + a4.x) * dt_sixth;
    body.velocityY = origVel.y + (a1.y + (real)2.0*a2.y + (real)2.0*a3.y + a4.y) * dt_sixth;
    body.velocityZ = origVel.z + (a1.z + (real)2.0*a2.z + (real)2.0*a3.z + a4.z) * dt_sixth;

    bodies[idx] = body;
}
""")

    return kernel_template.substitute(real_type=real_type, vec_type=vec_type)


def generate_package_kernel_source(use_float32: bool = False) -> str:
    """Generate CUDA kernels for package-based computation.

    Thread grid covers K bodies (package size). For methods with intermediate
    force evaluations (RK4, Velocity Verlet, Forest-Ruth), `all_bodies` is NOT
    mutated during the step: intermediate package positions are kept in a
    separate vec3[K] buffer and only the final integration kernel writes back.
    The source positions READ for each force evaluation are drift-corrected —
    package rows use their current stage position (from the target buffer) and
    background rows are predicted to the stage time by a Taylor series of the
    selected order (frozen, + tau*v, or + 0.5*tau^2*a as well), exactly as the
    CPU _stage_source_positions does. The predictor order bounds the global
    order of the distributed scheme.

    The simple/symplectic Euler kernels keep the in-place mutation pattern
    (they have no intermediate accel evaluation, so it doesn't matter).
    """
    real_type = "float" if use_float32 else "double"
    vec_type = "float3" if use_float32 else "double3"

    kernel_template = Template(_CUDA_COMMON_SOURCE + """
// =============================================================================
// Acceleration kernels — sources read from all_bodies (background predicted
// to the stage time, package rows overridden by their current stage position)
// =============================================================================

// Targets read from all_bodies (used by simple/symplectic Euler).
__global__ void KernelComputeAccelerationsPackage(
    const BodyState* __restrict__ all_bodies,
    vec3* __restrict__ accelerations,
    const int N,
    const int package_start,
    const int package_size,
    const real gravitationalConstant,
    const real softeningDistanceSquared)
{
    extern __shared__ SourceBody sourceTile[];
    const int TILE_SIZE = blockDim.x;

    const int tid_in_package = blockIdx.x * blockDim.x + threadIdx.x;
    const bool active = (tid_in_package < package_size);
    const int global_body_idx = package_start + tid_in_package;

    BodyState myBody;
    if (active) {
        myBody = all_bodies[global_body_idx];
    }

    real accelX = (real)0;
    real accelY = (real)0;
    real accelZ = (real)0;

    const int numTiles = (N + TILE_SIZE - 1) / TILE_SIZE;
    for (int tileIdx = 0; tileIdx < numTiles; tileIdx++) {
        const int tileStart = tileIdx * TILE_SIZE;
        const int loadIdx = tileStart + threadIdx.x;
        if (loadIdx < N) {
            const BodyState b = all_bodies[loadIdx];
            sourceTile[threadIdx.x].bodyIdentifier = b.bodyIdentifier;
            sourceTile[threadIdx.x].bodyMass = b.bodyMass;
            sourceTile[threadIdx.x].positionX = b.positionX;
            sourceTile[threadIdx.x].positionY = b.positionY;
            sourceTile[threadIdx.x].positionZ = b.positionZ;
        }
        __syncthreads();

        const int tileEnd = min(tileStart + TILE_SIZE, N);
        const int tileSize = tileEnd - tileStart;

        if (active) {
            AccumulateSourceTile(sourceTile, tileSize, myBody.bodyIdentifier,
                                 myBody.positionX, myBody.positionY, myBody.positionZ,
                                 gravitationalConstant, softeningDistanceSquared,
                                 accelX, accelY, accelZ);
        }
        __syncthreads();
    }

    if (active) {
        accelerations[tid_in_package].x = accelX;
        accelerations[tid_in_package].y = accelY;
        accelerations[tid_in_package].z = accelZ;
    }
}

// Targets read from a separate target_positions[K] buffer.
// Used for RK4 / VV / FR intermediate evaluations: all_bodies storage is left
// untouched until the final writeback, but source positions are drift-corrected
// per stage (background: Taylor predictor of order predictorOrder, package rows:
// their stage position). Matches CPU's _stage_source_positions.
__global__ void KernelComputeAccelerationsPackageTarget(
    const BodyState* __restrict__ all_bodies,
    const vec3* __restrict__ target_positions,
    const vec3* __restrict__ all_source_accelerations,
    vec3* __restrict__ accelerations,
    const int N,
    const int package_start,
    const int package_size,
    const real gravitationalConstant,
    const real softeningDistanceSquared,
    const real sourceDriftTau,
    const int predictorOrder)
{
    extern __shared__ SourceBody sourceTile[];
    const int TILE_SIZE = blockDim.x;

    const int tid_in_package = blockIdx.x * blockDim.x + threadIdx.x;
    const bool active = (tid_in_package < package_size);
    const int global_body_idx = package_start + tid_in_package;

    int myId = 0;
    vec3 myPos = MakeVec3((real)0, (real)0, (real)0);
    if (active) {
        myId = all_bodies[global_body_idx].bodyIdentifier;
        myPos = target_positions[tid_in_package];
    }

    real accelX = (real)0;
    real accelY = (real)0;
    real accelZ = (real)0;

    const int numTiles = (N + TILE_SIZE - 1) / TILE_SIZE;
    for (int tileIdx = 0; tileIdx < numTiles; tileIdx++) {
        const int tileStart = tileIdx * TILE_SIZE;
        const int loadIdx = tileStart + threadIdx.x;
        if (loadIdx < N) {
            const BodyState b = all_bodies[loadIdx];
            // Drift-corrected sources: package rows read their current stage
            // position from target_positions, background rows are predicted to
            // the stage time (below), as in the CPU _stage_source_positions.
            real sx, sy, sz;
            if (loadIdx >= package_start && loadIdx < package_start + package_size) {
                const vec3 tp = target_positions[loadIdx - package_start];
                sx = tp.x; sy = tp.y; sz = tp.z;
            } else {
                // Background: Taylor predictor of order `predictorOrder`, matching
                // CPU _stage_source_positions exactly:
                //   0 frozen (x0), 1 linear (+tau*v), 2 accel (+0.5*tau^2*a).
                // a0 comes from all_source_accelerations (aligned with all_bodies),
                // only read at order >= 2.
                sx = b.positionX;
                sy = b.positionY;
                sz = b.positionZ;
                if (predictorOrder >= 1) {
                    sx += sourceDriftTau * b.velocityX;
                    sy += sourceDriftTau * b.velocityY;
                    sz += sourceDriftTau * b.velocityZ;
                }
                if (predictorOrder >= 2) {
                    const real halfTauSq = (real)0.5 * sourceDriftTau * sourceDriftTau;
                    const vec3 a0 = all_source_accelerations[loadIdx];
                    sx += halfTauSq * a0.x;
                    sy += halfTauSq * a0.y;
                    sz += halfTauSq * a0.z;
                }
            }
            sourceTile[threadIdx.x].bodyIdentifier = b.bodyIdentifier;
            sourceTile[threadIdx.x].bodyMass = b.bodyMass;
            sourceTile[threadIdx.x].positionX = sx;
            sourceTile[threadIdx.x].positionY = sy;
            sourceTile[threadIdx.x].positionZ = sz;
        }
        __syncthreads();

        const int tileEnd = min(tileStart + TILE_SIZE, N);
        const int tileSize = tileEnd - tileStart;

        if (active) {
            AccumulateSourceTile(sourceTile, tileSize, myId, myPos.x, myPos.y, myPos.z,
                                 gravitationalConstant, softeningDistanceSquared,
                                 accelX, accelY, accelZ);
        }
        __syncthreads();
    }

    if (active) {
        accelerations[tid_in_package].x = accelX;
        accelerations[tid_in_package].y = accelY;
        accelerations[tid_in_package].z = accelZ;
    }
}

// =============================================================================
// Euler integration kernels (no intermediate accel — in-place is fine)
// =============================================================================

__global__ void KernelIntegratePackage(
    BodyState* __restrict__ all_bodies,
    const vec3* __restrict__ accelerations,
    const int package_start,
    const int package_size,
    const real timeStepSeconds)
{
    const int tid_in_package = blockIdx.x * blockDim.x + threadIdx.x;
    if (tid_in_package >= package_size) return;

    const int global_body_idx = package_start + tid_in_package;
    BodyState body = all_bodies[global_body_idx];

    body.velocityX += accelerations[tid_in_package].x * timeStepSeconds;
    body.velocityY += accelerations[tid_in_package].y * timeStepSeconds;
    body.velocityZ += accelerations[tid_in_package].z * timeStepSeconds;

    body.positionX += body.velocityX * timeStepSeconds;
    body.positionY += body.velocityY * timeStepSeconds;
    body.positionZ += body.velocityZ * timeStepSeconds;

    all_bodies[global_body_idx] = body;
}

__global__ void KernelIntegratePackageSimpleEuler(
    BodyState* __restrict__ all_bodies,
    const vec3* __restrict__ accelerations,
    const int package_start,
    const int package_size,
    const real timeStepSeconds)
{
    const int tid_in_package = blockIdx.x * blockDim.x + threadIdx.x;
    if (tid_in_package >= package_size) return;

    const int global_body_idx = package_start + tid_in_package;
    BodyState body = all_bodies[global_body_idx];

    // Simple Euler: position first with OLD velocity
    body.positionX += body.velocityX * timeStepSeconds;
    body.positionY += body.velocityY * timeStepSeconds;
    body.positionZ += body.velocityZ * timeStepSeconds;

    // Then velocity
    body.velocityX += accelerations[tid_in_package].x * timeStepSeconds;
    body.velocityY += accelerations[tid_in_package].y * timeStepSeconds;
    body.velocityZ += accelerations[tid_in_package].z * timeStepSeconds;

    all_bodies[global_body_idx] = body;
}

// =============================================================================
// Shared helpers: save / writeback K-body state
// =============================================================================

// Copy K bodies' pos and vel from all_bodies into vec3[K] buffers.
__global__ void KernelSavePackageState(
    const BodyState* __restrict__ all_bodies,
    vec3* __restrict__ saved_pos,
    vec3* __restrict__ saved_vel,
    const int package_start,
    const int package_size)
{
    const int tid = blockIdx.x * blockDim.x + threadIdx.x;
    if (tid >= package_size) return;
    BodyState body = all_bodies[package_start + tid];
    saved_pos[tid] = MakeVec3(body.positionX, body.positionY, body.positionZ);
    saved_vel[tid] = MakeVec3(body.velocityX, body.velocityY, body.velocityZ);
}

// Write target_pos / target_vel back to all_bodies (used by FR final writeback).
__global__ void KernelWritebackPackageState(
    BodyState* __restrict__ all_bodies,
    const vec3* __restrict__ target_pos,
    const vec3* __restrict__ target_vel,
    const int package_start,
    const int package_size)
{
    const int tid = blockIdx.x * blockDim.x + threadIdx.x;
    if (tid >= package_size) return;
    vec3 p = target_pos[tid];
    vec3 v = target_vel[tid];
    const int global_body_idx = package_start + tid;
    BodyState body = all_bodies[global_body_idx];
    body.positionX = p.x;
    body.positionY = p.y;
    body.positionZ = p.z;
    body.velocityX = v.x;
    body.velocityY = v.y;
    body.velocityZ = v.z;
    all_bodies[global_body_idx] = body;
}

// =============================================================================
// RK4 Package Kernels — target buffer pattern
// =============================================================================

// Set target position for stage 2/3/4 RK4 force eval. Writes to target_pos[K]
// only. all_bodies keeps the step's original state until the final writeback.
//
// Stage 2: pos = x_0 + (dt/2) * v_0                                (prevAccel unused)
// Stage 3: pos = x_0 + (dt/2) * (v_0 + (dt/2) * a_1)               (prevAccel = a_1)
// Stage 4: pos = x_0 +    dt   * (v_0 + (dt/2) * a_2)              (prevAccel = a_2)
__global__ void KernelRK4SetTargetPosPackage(
    const vec3* __restrict__ orig_pos,
    const vec3* __restrict__ orig_vel,
    const vec3* __restrict__ prev_accel,
    vec3* __restrict__ target_pos,
    const int package_size,
    const real dt,
    const int stage)
{
    const int tid = blockIdx.x * blockDim.x + threadIdx.x;
    if (tid >= package_size) return;
    vec3 op = orig_pos[tid];
    vec3 ov = orig_vel[tid];
    const real half_dt = (real)0.5 * dt;

    if (stage == 2) {
        target_pos[tid] = MakeVec3(
            op.x + half_dt * ov.x,
            op.y + half_dt * ov.y,
            op.z + half_dt * ov.z
        );
    } else if (stage == 3) {
        vec3 a = prev_accel[tid];
        target_pos[tid] = MakeVec3(
            op.x + half_dt * (ov.x + half_dt * a.x),
            op.y + half_dt * (ov.y + half_dt * a.y),
            op.z + half_dt * (ov.z + half_dt * a.z)
        );
    } else {  // stage 4
        vec3 a = prev_accel[tid];
        target_pos[tid] = MakeVec3(
            op.x + dt * (ov.x + half_dt * a.x),
            op.y + dt * (ov.y + half_dt * a.y),
            op.z + dt * (ov.z + half_dt * a.z)
        );
    }
}

// Final RK4 step: combine k1..k4 and write new pos+vel back to all_bodies.
__global__ void KernelRK4FinalIntegrationPackage(
    BodyState* __restrict__ all_bodies,
    const vec3* __restrict__ orig_pos,
    const vec3* __restrict__ orig_vel,
    const vec3* __restrict__ k1_accel,
    const vec3* __restrict__ k2_accel,
    const vec3* __restrict__ k3_accel,
    const vec3* __restrict__ k4_accel,
    const int package_start,
    const int package_size,
    const real dt)
{
    const int tid = blockIdx.x * blockDim.x + threadIdx.x;
    if (tid >= package_size) return;

    vec3 op = orig_pos[tid];
    vec3 ov = orig_vel[tid];
    vec3 a1 = k1_accel[tid];
    vec3 a2 = k2_accel[tid];
    vec3 a3 = k3_accel[tid];
    vec3 a4 = k4_accel[tid];

    const real dt_sixth = dt / (real)6.0;
    const real dt_half = dt * (real)0.5;

    // k_x values (position derivatives):
    //   k1_x = v_0
    //   k2_x = v_0 + (dt/2) * a_1
    //   k3_x = v_0 + (dt/2) * a_2
    //   k4_x = v_0 +    dt  * a_3
    vec3 k1_x = ov;
    vec3 k2_x = MakeVec3(ov.x + a1.x * dt_half, ov.y + a1.y * dt_half, ov.z + a1.z * dt_half);
    vec3 k3_x = MakeVec3(ov.x + a2.x * dt_half, ov.y + a2.y * dt_half, ov.z + a2.z * dt_half);
    vec3 k4_x = MakeVec3(ov.x + a3.x * dt, ov.y + a3.y * dt, ov.z + a3.z * dt);

    const int global_body_idx = package_start + tid;
    BodyState body = all_bodies[global_body_idx];

    body.positionX = op.x + (k1_x.x + (real)2.0*k2_x.x + (real)2.0*k3_x.x + k4_x.x) * dt_sixth;
    body.positionY = op.y + (k1_x.y + (real)2.0*k2_x.y + (real)2.0*k3_x.y + k4_x.y) * dt_sixth;
    body.positionZ = op.z + (k1_x.z + (real)2.0*k2_x.z + (real)2.0*k3_x.z + k4_x.z) * dt_sixth;

    body.velocityX = ov.x + (a1.x + (real)2.0*a2.x + (real)2.0*a3.x + a4.x) * dt_sixth;
    body.velocityY = ov.y + (a1.y + (real)2.0*a2.y + (real)2.0*a3.y + a4.y) * dt_sixth;
    body.velocityZ = ov.z + (a1.z + (real)2.0*a2.z + (real)2.0*a3.z + a4.z) * dt_sixth;

    all_bodies[global_body_idx] = body;
}

// =============================================================================
// Velocity Verlet Package Kernels — target buffer pattern
// =============================================================================

// Compute new target position: x_new = x_0 + v_0*dt + 0.5 * a_old * dt^2
__global__ void KernelVVPositionUpdatePackage(
    const vec3* __restrict__ orig_pos,
    const vec3* __restrict__ orig_vel,
    const vec3* __restrict__ old_accel,
    vec3* __restrict__ target_pos,
    const int package_size,
    const real dt)
{
    const int tid = blockIdx.x * blockDim.x + threadIdx.x;
    if (tid >= package_size) return;
    vec3 op = orig_pos[tid];
    vec3 ov = orig_vel[tid];
    vec3 ao = old_accel[tid];
    const real dt_sq_half = (real)0.5 * dt * dt;
    target_pos[tid] = MakeVec3(
        op.x + ov.x * dt + ao.x * dt_sq_half,
        op.y + ov.y * dt + ao.y * dt_sq_half,
        op.z + ov.z * dt + ao.z * dt_sq_half
    );
}

// Compute velocity update and write final pos+vel to all_bodies.
//   v_new = v_0 + 0.5 * (a_old + a_new) * dt
__global__ void KernelVVVelocityUpdatePackage(
    BodyState* __restrict__ all_bodies,
    const vec3* __restrict__ orig_vel,
    const vec3* __restrict__ old_accel,
    const vec3* __restrict__ new_accel,
    const vec3* __restrict__ target_pos,
    const int package_start,
    const int package_size,
    const real dt)
{
    const int tid = blockIdx.x * blockDim.x + threadIdx.x;
    if (tid >= package_size) return;

    vec3 ov = orig_vel[tid];
    vec3 ao = old_accel[tid];
    vec3 an = new_accel[tid];
    vec3 tp = target_pos[tid];

    const real dt_half = (real)0.5 * dt;

    const int global_body_idx = package_start + tid;
    BodyState body = all_bodies[global_body_idx];
    body.positionX = tp.x;
    body.positionY = tp.y;
    body.positionZ = tp.z;
    body.velocityX = ov.x + (ao.x + an.x) * dt_half;
    body.velocityY = ov.y + (ao.y + an.y) * dt_half;
    body.velocityZ = ov.z + (ao.z + an.z) * dt_half;
    all_bodies[global_body_idx] = body;
}

// =============================================================================
// Forest-Ruth Package Kernels — target buffer pattern
// =============================================================================

// Drift: target_pos += scale_dt * target_vel
__global__ void KernelFRDriftPackage(
    vec3* __restrict__ target_pos,
    const vec3* __restrict__ target_vel,
    const int package_size,
    const real scale_dt)
{
    const int tid = blockIdx.x * blockDim.x + threadIdx.x;
    if (tid >= package_size) return;
    vec3 p = target_pos[tid];
    vec3 v = target_vel[tid];
    target_pos[tid] = MakeVec3(
        p.x + scale_dt * v.x,
        p.y + scale_dt * v.y,
        p.z + scale_dt * v.z
    );
}

// Kick: target_vel += scale_dt * accel
__global__ void KernelFRKickPackage(
    vec3* __restrict__ target_vel,
    const vec3* __restrict__ accel,
    const int package_size,
    const real scale_dt)
{
    const int tid = blockIdx.x * blockDim.x + threadIdx.x;
    if (tid >= package_size) return;
    vec3 v = target_vel[tid];
    vec3 a = accel[tid];
    target_vel[tid] = MakeVec3(
        v.x + scale_dt * a.x,
        v.y + scale_dt * a.y,
        v.z + scale_dt * a.z
    );
}
""")

    return kernel_template.substitute(real_type=real_type, vec_type=vec_type)
