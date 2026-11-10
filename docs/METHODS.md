# Scope of calculations

The shared graph drives netlist generation, native scripts and the circuit viewer. Scaling and overrides are applied before declared-domain validation. Fixed reference capacitors retain the generator's protection rule. Each generated circuit has a hash-bound manifest; controls and solver settings are recorded separately from physical identity.

Startup initial conditions follow the selected mode and current bias for the supported topology templates. Generation alone does not prove startup. DC loop gain, periodic convergence, transient startup, noise, tuning coverage and PVT each require their own measurements.

Evidence comparison requires recorded conditions and source hashes. Empty evidence is unknown. Independent convergence must change actual solver settings while retaining circuit and adopted controls. Never mix conditions or physical identities to claim a tuning band.
