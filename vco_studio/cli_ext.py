"""Extension commands (selection, seeding, auto-sizing, campaigns, verification stages, delivery, knowledge).

Kept separate from cli.py so new capabilities can be added without touching the core contract.
"""
EXT = []


def register(sub):
    for module_name, register_fn, _ in _modules():
        register_fn(sub)


def dispatch(args):
    for module_name, _, dispatch_fn in _modules():
        out = dispatch_fn(args)
        if out is not None:
            return out
    raise ValueError('unknown command ' + str(args.command))


def _modules():
    import importlib
    out = []
    for name in ('knowledge', 'select', 'autosize', 'tools_cli', 'loopgain', 'sizing', 'stages'):
        try:
            mod = importlib.import_module('vco_studio.' + name)
        except ModuleNotFoundError as exc:
            if exc.name == 'vco_studio.' + name:
                continue
            raise
        if hasattr(mod, 'cli_register') and hasattr(mod, 'cli_dispatch'):
            out.append((name, mod.cli_register, mod.cli_dispatch))
    return out
