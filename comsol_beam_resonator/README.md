# Silicon beam + end proof-mass — COMSOL eigenfrequency model

Parameterized 3-D model of a cantilevered Si beam, fixed to the frame at one
end, with a heavy proof mass on the other end that is pinned transversely but
free to move axially (loading the beam in tension/compression). Built for
COMSOL Multiphysics, Structural Mechanics Module, Eigenfrequency study.

**No COMSOL installation is available in the sandbox this was built in**, so
none of this has been compiled or solved against real COMSOL — treat it as a
strong starting point, not a verified result. `model_recipe.md` is the
reliable path (plain GUI steps, stable across COMSOL versions).
`model_beam_eigenfrequency.java` is a best-effort scripted version for
automation; check it compiles before relying on it (see caveats at the top of
that file).

## Files

- `model_recipe.md` — step-by-step GUI build instructions.
- `model_beam_eigenfrequency.java` — COMSOL Java API script that builds the
  same model programmatically (`comsol compile` / run through the COMSOL
  Desktop "Compile Java File" feature).
- `model_beam_eigenfrequency.py` — the same script ported to Python, via the
  third-party [`mph`](https://pypi.org/project/mph/) package (`pip install
  mph`), which drives a local COMSOL install over the same underlying Java
  API. Run with `python model_beam_eigenfrequency.py`.
- `analytical_estimate.py` — a dependency-free Python sanity check, runnable
  right now, that computes the axial mass-spring frequency and related
  quantities in closed form.

## Important finding — check this before you spend time in COMSOL

The dimensions as given (100 µm × 10 µm × 5 µm beam, 100 mg proof mass, axial
mode) do **not** land anywhere near 50–100 MHz:

```
axial stiffness   k = E*A/L        = 8.5e4 N/m
axial eigenfrequency (100 mg mass) = 4.64 kHz
```

That's about four orders of magnitude below the target band. Two things drive
this:

1. **100 mg is enormous at this scale.** A 100 mg lump of silicon is ~3.5 mm
   on a side — about 35,000× longer than the 100 µm beam. Even a dense metal
   (tungsten) mass of 100 mg is still a ~1.7 mm cube. There is no way to
   physically attach a mass that large to a beam this small and keep it
   behaving like a lumped point mass; it would dominate everything
   mechanically (including snapping the beam under its own weight/handling
   loads).
2. **Even with zero added mass**, the beam's own bare fixed-free axial
   resonance is only ~21.4 MHz — already under 50 MHz, so any real tip mass
   only pulls it down further.

To actually hit 50–100 MHz in the axial mode with these beam dimensions, the
tip mass needs to be in the **sub-nanogram range**:

| Target frequency | Required tip mass |
|---|---|
| 50 MHz  | ~0.86 ng (860 pg) |
| 100 MHz | ~0.22 ng (215 pg) |

Alternatively, keeping a more substantial tip mass and shortening/stiffening
the beam works too — e.g. with ~zero tip mass, a beam length around
21–43 µm puts the *bare rod's own* axial resonance in the 50–100 MHz band;
adding any real proof mass back in will pull that down again, so this is a
starting point for the parametric sweep, not a final answer.

Run `python3 analytical_estimate.py` to reproduce these numbers and reuse
`required_mass_for_freq()` / `axial_stiffness()` as a fast pre-filter before
burning FEA time — e.g. sweep `beam_L`, `beam_W`, `beam_H`, `m_proof` in that
script first to find a region near 50–100 MHz, then hand only that narrowed
region to COMSOL's Parametric Sweep or Optimization Module for the real
(anisotropic-material, non-lumped) answer.

The model itself is still built exactly as you specified — parameters are
exposed for `beam_L`, `beam_W`, `beam_H`, `m_proof`, `E_Si`, `nu_Si`, `rho_Si`
so you can drive the redesign directly in COMSOL once you've picked a
promising region.

## Physics / boundary conditions implemented

- Domain: single 3-D block, Silicon, isotropic elastic approximation
  (E = 170 GPa, ν = 0.28, ρ = 2329 kg/m³ — swap for the anisotropic
  single-crystal Si material in COMSOL's material library if you need
  orientation-dependent stiffness later).
- Face at `x = 0` ("frame" end): **Fixed Constraint**.
- Face at `x = beam_L` (proof-mass end):
  - **Prescribed Displacement**: `u_y = 0`, `u_z = 0`, `u_x` free — this is a
    roller/guided support, pinning the face transversely while letting it
    translate along the beam axis (tension/compression), matching "moved in
    longitudinal direction, constrained in transverse directions."
  - **Added Mass**: surface mass density `m_proof / (beam_W * beam_H)` on the
    same face — this lumps the specified proof mass onto the end of the beam
    without modeling its (currently unspecified, and per above, physically
    awkward at 100 mg) geometry. It contributes only to the mass matrix, not
    stiffness, which is the correct way to represent an attached inertial
    load whose own shape isn't part of the question.
- Study: **Eigenfrequency**, set "search for eigenfrequencies around" a value
  near your current estimate (e.g. the bare-rod ~21 MHz, or wherever your
  swept parameters land) rather than 0 Hz — with a much lighter tip mass than
  100 mg the low end of the spectrum can otherwise be cluttered with rigid
  body / low-order modes ahead of the axial mode you actually want.

## Optimizing afterward

Once the model solves:
- **Study > Parametric Sweep** over `beam_L`, `beam_W`, `beam_H`, `m_proof` —
  cheapest way to map the design space by hand.
- **Optimization Module** (if licensed): define an objective on the
  eigenfrequency (e.g. minimize `abs(freq - 75[MHz])`) with those same
  parameters as control variables and bounds you choose.
