# User device bindings

The four `examples/bindings/demo_*.json` files are fictional software fixtures. Their library `example_devices`, cells `demo_nmos`, `demo_pmos`, `demo_cap` and `demo_res`, dimensions and limits are invented. The example include contains comments only. They permit graph validation and code generation, never meaningful simulation. The second example uses a one-dimensional capacitor seed to exercise that mapping contract.

Supply a `vco.binding.v1` JSON. Its `roles` map names canonical roles to user masters, canonical-to-actual `terminals`, `parameter_map`, `netlist_terminal_order`, `netlist_parameter_map`, a model identifier and explicit `seed_parameters`. Set `status` to `resolved` only after checking those mappings. `model_includes` is a list of `{ "path": "/path/to/device-model.scs", "section": "tt" }`; paths may use `@repo/` for repository-relative examples. Example limits are not technology rules. Encode only limits verified from your device documentation.

Use a binding path with `launch project --binding`, or register an identifier in the selected site JSON's `bindings` object. Specs use the identifier or existing binding file path in `pdk`; set the actual `vdd_v` explicitly. No aliases for commercial processes are inferred.

Native discovery needs a workspace with a valid user `cds.lib` and a user runtime profile selected by `VCO_STUDIO_NATIVE_PROFILE`. Its executable paths and optional environment setup are user inputs. Construction creates isolated outputs and refuses existing target libraries. A netlist match is separate from oscillation/noise qualification.

Optional site keys include `python`, `spectre`, `cadence_env`, `runs_root`, `native_profile`, `tools`, `bindings`, `evidence_indexes`, `path_map`, `kb` and `sim_lock_command`. `VCO_STUDIO_PYTHON`, `VCO_SPECTRE`, `VCO_CADENCE_ENV`, `VCO_RUNS_ROOT` and `VCO_KB_COMMAND` provide overrides. External tool paths belong to the user profile. License values are inherited from the user's environment and are never included in the repository.
