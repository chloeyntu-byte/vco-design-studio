# Scope of calculations

The shared graph drives netlist generation, native scripts and the circuit viewer. Scaling and overrides are applied before declared-domain validation. Fixed reference capacitors retain the generator's protection rule. Each generated circuit has a hash-bound manifest; controls and solver settings are recorded separately from physical identity.

Startup initial conditions follow the selected mode and current bias for the supported topology templates. Generation alone does not prove startup. DC loop gain, periodic convergence, transient startup, noise, tuning coverage and PVT each require their own measurements.

Evidence comparison requires recorded conditions and source hashes. Empty evidence is unknown. Independent convergence must change actual solver settings while retaining circuit and adopted controls. Never mix conditions or physical identities to claim a tuning band.

The built-in nominal freeze gate checks the largest standalone magnetic-mode Q at the actual fundamental f0 in (0, 30]. For coupled networks this already includes coupling enhancement; do not apply a second enhancement factor. This implementation limit is not a technology design rule. Second-harmonic Q is not restricted by that gate.

Phase-noise offsets supported by the workflow are 10 kHz, 100 kHz, 1 MHz and 10 MHz. The positive FoM convention is `-PN + 20*log10(f0/offset) - 10*log10(power_mW)`. Tuning range is `(fmax-fmin)/((fmax+fmin)/2)`. Power includes supply, bias and active controls, with the maximum taken over adopted states. Set supply and temperature from user model data; template defaults are not process recommendations.

Startup gain is measured at the drain port. A single cross-coupled pair targets roughly gm_single*Rp_diff=4 (negative-conductance margin about 2); complementary/multiple-core cases must use the measured port margin. Check the lowest-gain adopted code. Differential peak swing screens depend on core type (approximately VDD for complementary cores, 2*VDD for single-polarity cores); these are diagnosis heuristics, not reliability limits.
