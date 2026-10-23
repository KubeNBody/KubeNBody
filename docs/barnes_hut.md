# Barnes-Hut Algorithm Implementation

## Table of Contents

1. [Overview](#overview)
2. [The Problem: O(N^2) Force Calculation](#the-problem-on2-force-calculation)
3. [The Solution: Hierarchical Approximation](#the-solution-hierarchical-approximation)
4. [Algorithm Deep Dive](#algorithm-deep-dive)
5. [Implementation](#implementation)
6. [Complexity](#complexity)
7. [Choosing θ](#choosing-θ)
8. [References](#references)

---

## Overview

The **Barnes-Hut algorithm** (1986) is a hierarchical approximation method that reduces N-body gravitational force calculations from **O(N^2)** to **O(N log N)** complexity. It achieves this by:

1. Organizing bodies into a spatial tree structure (octree in 3D)
2. Approximating the gravitational effect of distant body groups as single "virtual" masses
3. Using a configurable accuracy parameter (θ) to control the approximation threshold

**Key Insight**: Bodies that are far away contribute less to the total force, and their individual contributions can be approximated by their combined center of mass without significant accuracy loss.

---

## The Problem: O(N^2) Force Calculation

### Direct Summation

Direct summation computes the forces **all-pairs**:

```
For each body i:
    For each body j ≠ i:
        Calculate F_ij = G * m_i * m_j / r_ij^2
        Add F_ij to acceleration of body i
```

**Complexity**: N bodies × N interactions = **O(N^2)** operations per timestep

**Scaling Problem**:
| N (bodies) | Interactions | Time @ 1M ops/sec |
|------------|--------------|-------------------|
| 1,000 | 1,000,000 | 1 second |
| 10,000 | 100,000,000 | 100 seconds |
| 100,000 | 10,000,000,000 | ~3 hours |
| 1,000,000 | 10^12 | ~12 days |

This makes large-scale simulations impractical with the direct method.

---

## The Solution: Hierarchical Approximation

### Core Idea

Instead of calculating forces from each individual body, we:

1. **Group nearby bodies** into hierarchical cells using an octree
2. **Precompute aggregate properties** (total mass, center of mass) for each cell
3. **Approximate distant groups** as single point masses at their center of mass
4. **Recurse into nearby groups** for accurate calculation

### Visual Example (2D Quadtree)

```
┌───────────────────────────────────────┐
│                                       │
│   ┌───────────┬───────────┐          │
│   │ ● ●       │           │          │
│   │   ●       │     ●     │   Body X │
│   ├───────────┼───────────┤     ↓    │
│   │           │ ● ●       │   ███    │
│   │     ●     │   ●●●     │          │
│   └───────────┴───────────┘          │
│                                       │
└───────────────────────────────────────┘
```

For **Body X** calculating forces:
- **Nearby cell** (bottom-right, 5 bodies): Too close → recurse, calculate individually
- **Distant cell** (top-left, 3 bodies): Far enough → treat as single mass at COM
- **Other cells**: Evaluate using θ criterion

### Multipole Acceptance Criterion (MAC)

The decision to approximate or recurse is controlled by the **opening angle** θ:

```
       s
θ_cell = ───
       d

Where:
  s = cell size (edge length)
  d = distance from body to cell's center of mass

If θ_cell < θ (threshold):
    → Approximate: treat cell as single mass
Else:
    → Recurse: examine children individually
```

**Typical θ values**:
- θ = 0.0: Exact calculation (no approximation, degenerates to O(N^2)). The kernel allows it, jobs accept θ from 0.1 to 2.0
- θ = 0.3: High accuracy, slower
- θ = 0.5: Balanced (default)
- θ = 0.7: Faster, moderate accuracy
- θ = 1.0: Fast, lower accuracy
- θ > 1.0: Very fast, significant approximation error

---

## Algorithm Deep Dive

### Phase 1: Build Octree

Recursively partition 3D space into 8 children (octants) until each leaf contains at most one body (bodies too close to separate, at the maximum depth, share a leaf).

```
Algorithm: BUILD_OCTREE(bodies)
─────────────────────────────────
1. Compute bounding box containing all bodies
2. Create root node covering bounding box
3. For each body:
   a. Start at root
   b. While current node is not a leaf:
      - Determine which octant body belongs to
      - Descend to that child
   c. If leaf is empty:
      - Place body here
   d. Else (leaf has a body):
      - Subdivide into 8 children
      - Re-insert existing body into appropriate child
      - Insert new body into appropriate child
4. Return root
```

**Octant Numbering** (3D):
```
Octant = (x_bit << 0) | (y_bit << 1) | (z_bit << 2)

Where x_bit = 1 if x >= center_x, else 0
      y_bit = 1 if y >= center_y, else 0
      z_bit = 1 if z >= center_z, else 0

Octant 0: (-, -, -)  bottom-left-back
Octant 1: (+, -, -)  bottom-right-back
Octant 2: (-, +, -)  top-left-back
Octant 3: (+, +, -)  top-right-back
Octant 4: (-, -, +)  bottom-left-front
Octant 5: (+, -, +)  bottom-right-front
Octant 6: (-, +, +)  top-left-front
Octant 7: (+, +, +)  top-right-front
```

### Phase 2: Compute Centers of Mass (Bottom-Up)

After tree construction, compute aggregate properties for internal nodes:

```
Algorithm: COMPUTE_COM(node)
────────────────────────────
If node is leaf with body:
    node.mass = body.mass
    node.com = body.position
    Return

For each child in node.children:
    COMPUTE_COM(child)

node.mass = Σ child.mass
node.com = Σ (child.mass × child.com) / node.mass
```

### Phase 3: Calculate Forces (Tree Walk)

For each body, walk the tree from the root (iteratively, with an explicit stack):

```
Algorithm: CALCULATE_FORCE(body, θ)
───────────────────────────────────
push root
while the stack is not empty:
    node = pop
    if node is empty, or a leaf holding this body: skip (no self-interaction)
    d = |node.com - body.position|            (unsoftened: the MAC's distance)
    if node is a leaf, or (s/d < θ and node's box does not contain the body):
        r = node.com - body.position
        a += G × node.mass × r / (|r|^2 + ε^2)^(3/2)   (Plummer softened)
    else:
        push the node's children
```

Two details keep it consistent with direct summation:

- **A cell whose box contains the target is always opened.** Otherwise, for
  θ > 1/√3 ≈ 0.577, the MAC could accept a cell that holds the body itself, and
  the body's own mass would enter its force through the monopole.
- **Coincident bodies.** A body that lands in an occupied leaf at the maximum
  depth, or exactly on the leaf's centre of mass, is merged into that leaf (its
  mass and centre of mass then cover all its bodies). With softening, the force
  between such bodies is negligible, so this matches direct summation instead
  of dropping the body.

### Complete Algorithm Flow

```
1. BUILD OCTREE                O(N log N)   bounding box, insert every body
2. COMPUTE CENTERS OF MASS     O(N)         bottom-up
3. CALCULATE FORCES            O(N log N)   one tree walk per body
4. INTEGRATE                   O(N)         the chosen integrator's update
```

Multi-stage integrators (velocity Verlet, RK4, Forest-Ruth) repeat steps 1-3 at
every force evaluation, since the tree is built from that stage's positions.

---

## Implementation

The tree code is `code/src/barnes_hut.py`, compiled with Numba:

| Function | What it does |
|---|---|
| `build_octree(positions, masses, max_depth)` | `(nodes, count)`: the octree as an array of `OCTREE_NODE_DTYPE` nodes (centre, size, mass, centre of mass, leaf flag, body index, count, first child), grown as needed |
| `compute_accelerations_barnes_hut(positions, masses, ids, theta, G, softening_sq)` | (N, 3) accelerations of every body |
| `compute_package_accelerations_barnes_hut(k_positions, k_ids, all_positions, all_masses, all_ids, theta, G, softening_sq)` | (K, 3) accelerations of a package's K bodies, the tree built from all N bodies |
| `_forces_on_targets(...)` | The target loop both kernels share, parallel over the targets in a spatial order (neighbouring bodies walk similar paths) |

There are no Barnes-Hut steppers. Every integrator in `code/src/physics.py`
takes the force algorithm as an argument (`algorithm=DIRECT` or `BARNES_HUT`,
and `theta`), and calls `accelerations_of` or `package_accelerations_of`, which
dispatch to the direct kernel or the tree. `physics.force_algorithm(name)` maps
a job's `force_algorithm` ('direct' or 'barnes_hut') to that code. The CPU
backend passes it through for both the monolithic and the package steps.

Jobs choose it with `force_algorithm=barnes_hut` and `barnes_hut_theta` (0.1 to
2.0, default 0.5) on either submit endpoint. Barnes-Hut is CPU only: a GPU job
must use direct summation, and the API rejects the combination.

The tests are `code/tests/test_barnes_hut.py` (tree construction, agreement
with direct summation, exactness at θ = 0, the energy behaviour of RK4, both Euler methods and
velocity Verlet,
self-exclusion, coincident bodies) and the Barnes-Hut cases of the reference trajectories
(`code/tests/test_trajectory_reference.py`), which also cover Forest-Ruth.

---

## Complexity

| Phase | Direct Method | Barnes-Hut |
|-------|---------------|------------|
| Tree build | - | O(N log N) |
| Centers of mass | - | O(N) |
| Forces | O(N^2) | O(N log N) |
| Integration | O(N) | O(N) |
| **Total** | **O(N^2)** | **O(N log N)** |

The tree costs more per interaction, so direct summation is faster for small N.

## Choosing θ

Smaller θ opens more cells: more accurate and slower. θ = 0 opens every cell
and degenerates to direct summation. The force error of an accepted cell scales
as θ^2 (see the appendix). Typical choices: 0.3 for accuracy, 0.5 (the default)
for a balance, 0.7-1.0 for speed.

---

## References

1. Barnes, J., & Hut, P. (1986). "A hierarchical O(N log N) force-calculation algorithm." *Nature*, 324(6096), 446-449.

2. Salmon, J. K., & Warren, M. S. (1994). "Skeletons from the treecode closet." *Journal of Computational Physics*, 111(1), 136-155.

3. Springel, V. (2005). "The cosmological simulation code GADGET-2." *Monthly Notices of the Royal Astronomical Society*, 364(4), 1105-1134.

---

## Appendix: Mathematical Derivation

### Multipole Expansion

The gravitational potential at point **r** due to a mass distribution can be expanded:

```
Φ(r) = -G × Σ m_i / |r - r_i|
     ≈ -G × [M/d + 0 + Q(d̂)/d^3 + ...]     expanded about the centre of mass R

Where:
  M = total mass of the cell
  d = |r - R|, the distance from the centre of mass
  the dipole term vanishes about the centre of mass
  Q = the quadrupole term, of order M × s^2 for a cell of size s
```

Barnes-Hut uses the **monopole term** only (the mass at the centre of mass), so the first neglected term is the quadrupole, a relative error of order (s/d)^2. Higher-order methods (Fast Multipole Method) include quadrupole and higher terms for better accuracy.

### Error Bound

For a cell of size s at distance d from the evaluation point:

```
Relative error ≤ (s/d)^2 × (1 + s/d + (s/d)^2 + ...)
              ≈ (s/d)^2   for s << d
              = θ^2       by MAC criterion
```

This shows why error scales as θ^2.
