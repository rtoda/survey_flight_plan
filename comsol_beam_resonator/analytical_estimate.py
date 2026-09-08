#!/usr/bin/env python3
"""
Closed-form sanity check for the Si beam + end proof-mass resonator, to be used
alongside the COMSOL eigenfrequency model (see model_recipe.md / model_beam_eigenfrequency.java
in this folder) and before spending time on a full FEA parametric sweep.

Two idealizations of the same geometry are computed:

1. "axial"    - the mode the geometry is actually built for: the proof mass is
                pinned transversely at the beam tip and can only move along the
                beam axis, so the beam acts as an axial (tension/compression)
                spring, k = E*A/L, loaded by the tip mass (mass-spring system).
2. "flexural" - the fundamental bending mode of the *bare* cantilever (no tip
                mass), for reference only, since the transverse pin in the
                actual design suppresses this mode at the tip.

Run directly to reproduce the numbers discussed with the user, or import
`axial_freq`/`required_mass_for_freq` to drive your own optimization loop.
"""
import math

# ---- default (as-specified) parameters -------------------------------------
BEAM_L = 100e-6      # m
BEAM_W = 10e-6       # m
BEAM_H = 5e-6        # m
M_PROOF = 100e-3 * 1e-3   # 100 mg in kg
E_SI = 170e9         # Pa, isotropic approximation
NU_SI = 0.28
RHO_SI = 2329.0      # kg/m^3


def kg_to_str(m):
    if m >= 1e-6:
        return f"{m * 1e6:.4f} mg"
    if m >= 1e-9:
        return f"{m * 1e9:.4f} ug"
    if m >= 1e-12:
        return f"{m * 1e12:.4f} ng"
    return f"{m * 1e15:.4f} pg"


def axial_stiffness(L=BEAM_L, W=BEAM_W, H=BEAM_H, E=E_SI):
    """Axial (tension/compression) spring constant of the beam, k = E*A/L."""
    A = W * H
    return E * A / L


def axial_freq(L=BEAM_L, W=BEAM_W, H=BEAM_H, m_proof=M_PROOF, E=E_SI):
    """Fundamental frequency (Hz) of the tip mass on the beam's axial spring."""
    k = axial_stiffness(L, W, H, E)
    return (1.0 / (2 * math.pi)) * math.sqrt(k / m_proof)


def required_mass_for_freq(f_target, L=BEAM_L, W=BEAM_W, H=BEAM_H, E=E_SI):
    """Tip mass (kg) needed to put the axial mode at f_target (Hz)."""
    k = axial_stiffness(L, W, H, E)
    return k / (2 * math.pi * f_target) ** 2


def bare_rod_axial_freq(L=BEAM_L, E=E_SI, rho=RHO_SI):
    """1st axial resonance of the beam alone (fixed-free rod, no tip mass)."""
    c = math.sqrt(E / rho)
    return c / (4 * L)


def flexural_freq_bare(L=BEAM_L, W=BEAM_W, H=BEAM_H, E=E_SI, rho=RHO_SI):
    """1st bending-mode frequency of the bare cantilever (no tip mass), for reference."""
    I = W * H ** 3 / 12
    A = W * H
    beta1L = 1.87510407
    return (beta1L ** 2 / (2 * math.pi)) * math.sqrt(E * I / (rho * A * L ** 4))


if __name__ == "__main__":
    print("=== As-specified design (100 um x 10 um x 5 um beam, 100 mg tip mass) ===")
    k = axial_stiffness()
    f_ax = axial_freq()
    print(f"axial stiffness k = E*A/L        = {k:.3e} N/m")
    print(f"axial (tension/compression) mode = {f_ax:.3f} Hz  <-- NOT in the 50-100 MHz target band")
    print()

    vol = M_PROOF / RHO_SI
    print(f"a 100 mg lump of silicon has volume {vol:.3e} m^3, i.e. a cube "
          f"~{vol ** (1/3) * 1e3:.2f} mm on a side - about 35,000x the beam's 100 um length.")
    print()

    print("=== What it would take to land the axial mode in 50-100 MHz ===")
    for f_target in (50e6, 100e6):
        m_needed = required_mass_for_freq(f_target)
        print(f"  target {f_target/1e6:.0f} MHz -> tip mass must be {kg_to_str(m_needed)} "
              f"({m_needed:.3e} kg), holding beam_L/W/H fixed")

    f_rod = bare_rod_axial_freq()
    print(f"\nFor reference, the beam alone (no tip mass at all) has a 1st axial "
          f"resonance of {f_rod/1e6:.2f} MHz - already below 50 MHz, so any real "
          f"added mass only pulls the frequency down further.")

    print("\n=== Bare-beam bending mode, for reference (transverse motion is pinned "
          "at the tip in this design, so this mode is not what the model targets) ===")
    f_flex = flexural_freq_bare()
    print(f"  1st bending mode of bare cantilever = {f_flex/1e6:.3f} MHz")

    print("\n=== Beam lengths that put the BARE ROD axial resonance in 50-100 MHz ===")
    c = math.sqrt(E_SI / RHO_SI)
    for f_target in (50e6, 100e6):
        L_needed = c / (4 * f_target)
        print(f"  {f_target/1e6:.0f} MHz -> beam_L ~= {L_needed * 1e6:.2f} um (with ~zero added tip mass)")
