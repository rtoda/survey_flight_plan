#!/usr/bin/env python3
"""
COMSOL model-building script, Python port of model_beam_eigenfrequency.java.

Uses the third-party `mph` package (https://pypi.org/project/mph/), which
talks to a local COMSOL installation over its Java layer (via JPype) and
exposes the same node/feature API as the Java script - `pymodel.java` below
is the same `com.comsol.model.Model` object the .java file builds, just
called from Python instead of compiled Java. Requires:

    pip install mph
    # and a COMSOL installation on this machine (mph starts/attaches to it)

Run:
    python model_beam_eigenfrequency.py

CAVEAT: written without access to a COMSOL installation to run this against,
so treat it as a best-effort, untested port - same caveat as the .java file.
Likely trouble spots if it errors out:
  - JPype's automatic Python->Java type conversion for array-valued `set()`
    calls (`size`, `pos`, `PrescribedDisplacementActive` below). If a call
    raises a type/overload error, wrap the list explicitly, e.g.:
        import jpype
        jpype.JArray(jpype.JString)(["beam_L", "beam_W", "beam_H"])
    for string arrays, or `jpype.JArray(jpype.JInt)([0, 1, 1])` for int arrays.
  - The exact string tags COMSOL expects for the Box selection "condition"
    property, and the "AddedMass"/"PrescribedDisplacement" feature type
    names/property keys - see the same note in model_beam_eigenfrequency.java.

See ../README.md before trusting the default parameters below: as given
(100 mg tip mass) the axial eigenfrequency comes out around 4.6 kHz, not
50-100 MHz. Adjust m_proof / beam_L / beam_W / beam_H and re-run.
"""
import mph


def build():
    client = mph.start()
    pymodel = client.create("beam_eigenfrequency")
    model = pymodel.java  # raw com.comsol.model.Model, same API as the .java script

    # ---- parameters (all dimensions/material properties live here so the
    # whole model is driven from this table for later optimization) --------
    model.param().set("beam_L", "100[um]", "Beam length")
    model.param().set("beam_W", "10[um]", "Beam width")
    model.param().set("beam_H", "5[um]", "Beam thickness")
    model.param().set("m_proof", "100[mg]", "Proof mass, lumped at free end")
    model.param().set("E_Si", "170[GPa]", "Young's modulus (isotropic approx.)")
    model.param().set("nu_Si", "0.28", "Poisson's ratio")
    model.param().set("rho_Si", "2329[kg/m^3]", "Density")
    model.param().set("f_search", "21[MHz]", "Eigenfrequency search point")

    model.component().create("comp1", True)

    # ---- geometry ---------------------------------------------------------
    model.component("comp1").geom().create("geom1", 3)
    model.component("comp1").geom("geom1").lengthUnit("um")

    model.component("comp1").geom("geom1").create("blk1", "Block")
    model.component("comp1").geom("geom1").feature("blk1") \
        .set("size", ["beam_L", "beam_W", "beam_H"])
    model.component("comp1").geom("geom1").feature("blk1").set("base", "corner")
    model.component("comp1").geom("geom1").feature("blk1") \
        .set("pos", ["0", "-beam_W/2", "-beam_H/2"])

    model.component("comp1").geom("geom1").run()

    # ---- selections: end faces by position, not by (unstable) face index --
    # "fixed" face at x = 0 ("frame" end)
    model.component("comp1").selection().create("sel_fixed", "Box")
    model.component("comp1").selection("sel_fixed").set("entitydim", 2)
    model.component("comp1").selection("sel_fixed").set("xmin", "-0.01*beam_L")
    model.component("comp1").selection("sel_fixed").set("xmax", "0.01*beam_L")
    model.component("comp1").selection("sel_fixed").set("ymin", "-10*beam_W")
    model.component("comp1").selection("sel_fixed").set("ymax", "10*beam_W")
    model.component("comp1").selection("sel_fixed").set("zmin", "-10*beam_H")
    model.component("comp1").selection("sel_fixed").set("zmax", "10*beam_H")
    model.component("comp1").selection("sel_fixed").set("condition", "allvertices")

    # "proof mass" face at x = beam_L (free end)
    model.component("comp1").selection().create("sel_mass", "Box")
    model.component("comp1").selection("sel_mass").set("entitydim", 2)
    model.component("comp1").selection("sel_mass").set("xmin", "beam_L*0.99")
    model.component("comp1").selection("sel_mass").set("xmax", "beam_L*1.01")
    model.component("comp1").selection("sel_mass").set("ymin", "-10*beam_W")
    model.component("comp1").selection("sel_mass").set("ymax", "10*beam_W")
    model.component("comp1").selection("sel_mass").set("zmin", "-10*beam_H")
    model.component("comp1").selection("sel_mass").set("zmax", "10*beam_H")
    model.component("comp1").selection("sel_mass").set("condition", "allvertices")

    # ---- material -----------------------------------------------------
    model.component("comp1").material().create("mat1", "Common")
    model.component("comp1").material("mat1").propertyGroup("def") \
        .set("youngsmodulus", "E_Si")
    model.component("comp1").material("mat1").propertyGroup("def") \
        .set("poissonsratio", "nu_Si")
    model.component("comp1").material("mat1").propertyGroup("def") \
        .set("density", "rho_Si")

    # ---- physics: Solid Mechanics ---------------------------------------
    model.component("comp1").physics().create("solid", "SolidMechanics", "geom1")

    model.component("comp1").physics("solid").create("fix1", "Fixed", 2)
    model.component("comp1").physics("solid").feature("fix1") \
        .selection().named("sel_fixed")

    model.component("comp1").physics("solid").create("disp1", "PrescribedDisplacement", 2)
    model.component("comp1").physics("solid").feature("disp1") \
        .selection().named("sel_mass")
    # constrain u_y, u_z only; leave u_x free (longitudinal, tension/compression)
    model.component("comp1").physics("solid").feature("disp1") \
        .set("PrescribedDisplacementActive", [0, 1, 1])
    model.component("comp1").physics("solid").feature("disp1") \
        .set("Prescribeduy", 0)
    model.component("comp1").physics("solid").feature("disp1") \
        .set("Prescribeduz", 0)

    model.component("comp1").physics("solid").create("mass1", "AddedMass", 2)
    model.component("comp1").physics("solid").feature("mass1") \
        .selection().named("sel_mass")
    model.component("comp1").physics("solid").feature("mass1") \
        .set("AreaMassDensity", "m_proof/(beam_W*beam_H)")

    # ---- mesh: mapped cross-section, swept along the beam axis -----------
    model.component("comp1").mesh().create("mesh1")
    model.component("comp1").mesh("mesh1").create("map1", "Map")
    model.component("comp1").mesh("mesh1").feature("map1").selection().named("sel_fixed")
    model.component("comp1").mesh("mesh1").create("swe1", "Sweep")
    model.component("comp1").mesh("mesh1").feature("swe1").create("dis1", "Distribution")
    model.component("comp1").mesh("mesh1").feature("swe1").feature("dis1").set("numelem", 25)
    model.component("comp1").mesh("mesh1").run()

    # ---- study: eigenfrequency ------------------------------------------
    model.study().create("std1")
    model.study("std1").create("eig", "Eigenfrequency")
    model.study("std1").feature("eig").set("neigs", 6)
    model.study("std1").feature("eig").set("shift", "f_search")

    model.sol().create("sol1")
    model.sol("sol1").study("std1")
    model.sol("sol1").attach("std1")
    model.sol("sol1").create("st1", "StudyStep")
    model.sol("sol1").create("v1", "Variables")
    model.sol("sol1").create("e1", "Eigenvalue")
    model.sol("sol1").feature("e1").set("neigs", 6)
    model.sol("sol1").feature("e1").set("shift", "f_search")
    model.sol("sol1").runAll()

    # ---- results: default mode-shape plot + frequency table --------------
    model.result().create("pg1", "PlotGroup3D")
    model.result("pg1").create("surf1", "Surface")
    model.result("pg1").feature("surf1").set("expr", "solid.disp")

    model.result().numerical().create("gev1", "EvalGlobal")
    model.result().numerical("gev1").set("expr", ["freq"])

    pymodel.save("beam_eigenfrequency.mph")

    return pymodel


if __name__ == "__main__":
    pymodel = build()
    # mph's pythonic accessor for the solved eigenfrequencies, once solved above
    print(pymodel.evaluate("freq"))
