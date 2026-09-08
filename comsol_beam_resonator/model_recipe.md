# Manual build recipe (COMSOL Desktop GUI)

Reliable, version-independent alternative to the Java script — these are
GUI feature names/menus that have been stable across recent COMSOL releases.
See `../README.md` first for why the as-given numbers won't reach 50–100 MHz
before you spend time here.

## 0. Model Wizard

`Space Dimension`: 3D → `Physics`: Structural Mechanics → **Solid Mechanics
(solid)** → `Study`: **Eigenfrequency**.

## 1. Global Definitions > Parameters

Add rows (name / expression / description):

| Name | Expression | Description |
|---|---|---|
| `beam_L` | `100[um]` | Beam length |
| `beam_W` | `10[um]` | Beam width |
| `beam_H` | `5[um]` | Beam thickness |
| `m_proof` | `100[mg]` | Proof mass (lumped, applied at free end) |
| `E_Si` | `170[GPa]` | Young's modulus (isotropic approx.) |
| `nu_Si` | `0.28` | Poisson's ratio |
| `rho_Si` | `2329[kg/m^3]` | Density |

Keeping every dimension and material constant as a named parameter is what
makes this optimizable later — nothing below should reference a bare number.

## 2. Geometry 1

Add a **Block**:
- Width (x) = `beam_L`, Depth (y) = `beam_W`, Height (z) = `beam_H`
- Base = `Corner`, Position = `(0, -beam_W/2, -beam_H/2)`

This puts the beam's axis on the x-axis, centered in y/z, running from the
"frame" end at `x=0` to the proof-mass end at `x=beam_L`. Click **Build All**.

## 3. Materials

Add a **Blank Material** (or start from the built-in "Silicon" entry and
override), set under `Basic`/`def` property group:
- Young's modulus = `E_Si`
- Poisson's ratio = `nu_Si`
- Density = `rho_Si`

Assign it to the block domain (should be automatic with one domain).

## 4. Solid Mechanics (solid) physics

**Fixed Constraint**
- Selection: the face at `x = 0` (pick it in the Graphics window — with the
  block built as above it's the face whose all four corners sit at `x=0`).

**Prescribed Displacement** (on the face at `x = beam_L`)
- Selection: the opposite end face.
- Enable `u_y` and `u_z`, set both to 0. Leave `u_x` **unconstrained** (free)
  — this is what lets the mass move only longitudinally under
  tension/compression while being pinned transversely, per the spec.

**Added Mass** (on the *same* face at `x = beam_L`)
- Selection: same end face as above.
- Set surface mass density = `m_proof/(beam_W*beam_H)`.
- This lumps the whole `m_proof` onto that face as inertia only (no added
  stiffness), which is the right way to represent an attached mass whose own
  geometry/shape isn't specified — and, given the 100 mg vs. 100 µm scale
  mismatch discussed in the README, isn't practical to draw as real geometry
  at these dimensions anyway.

## 5. Mesh

For a long, thin beam, a **swept mesh** gives much better-conditioned
elements than free tetrahedra:
- On the `x=0` (or `x=beam_L`) face, add a **Mapped** mesh (Distribution:
  e.g. 4–6 elements across `beam_W`, 4–6 elements across `beam_H`).
- Add **Swept** for the domain, sweeping along x with ~20–30 elements
  (Distribution on the sweep edge).

(A free-tetrahedral mesh at "Finer"/"Extra fine" physics-controlled setting
also works as a quick first pass if you want to skip the swept setup.)

## 6. Study > Eigenfrequency

- `Desired number of eigenfrequencies`: 6 (or more — with a heavy tip mass
  relative to the beam, expect several low-order bending/rocking-type modes
  of the assembly before you reach the pure axial tension/compression mode).
- `Search for eigenfrequencies around`: set this near your current estimate
  (start with the bare-rod value from `analytical_estimate.py`, ~21 MHz for
  the as-given beam dimensions, or wherever your parameter sweep currently
  points) instead of leaving it at 0 — this steers the solver toward the
  modes you actually care about.
- Run.

## 7. Results

- Default **Eigenfrequency** plot group shows mode shapes — click through
  the computed frequencies and look for the one that is pure axial
  stretch/compression of the beam (the mass translates along x with no
  visible bending) to confirm you're reading the right mode, not a
  bending/rocking mode of the tip mass.
- Add a **Global Evaluation** (Derived Values) on `solid.freq` if you want
  the frequency list as a table/export.

## 8. Optimize

- `Study > Parametric Sweep`: vary `beam_L`, `beam_W`, `beam_H`, `m_proof`
  (and re-run the eigenfrequency study for each combination).
- Or, with the Optimization Module: set an objective like
  `abs(solid.freq(1) - 75[MHz])` (minimize) with those same parameters as
  control variables and bounds you pick, once `analytical_estimate.py` (or
  a first sweep) has told you roughly which region of parameter space is
  worth exploring in full FEA.
