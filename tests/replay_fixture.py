"""Synthetic receipt fixture; runs a file-copy executable, never an EDA solver."""
from pathlib import Path
import sys
from vco_studio.catalog import default_project
from vco_studio.generator import generate
from vco_studio.simulator import run


def create_result(folder):
    root = Path(folder)
    source = Path(__file__).parent / 'fixtures/stages_psf'
    solver = root / 'replay-stub'
    solver.write_text('#!' + sys.executable + '\n' +
        'from pathlib import Path\nimport shutil\n' +
        'source=Path(' + repr(str(source)) + ')\n' +
        "Path('psf').mkdir()\n" +
        "shutil.copy2(source/'hb.txt', 'psf/hb.fd.pss_hb')\n" +
        "shutil.copy2(source/'noise.txt', 'psf/hbnoise.pm.pnoise_hbnoise')\n" +
        "Path('spectre.log').write_text('Synthetic replay; no circuit solved.\\n')\n")
    solver.chmod(0o700)
    generate(default_project('murphy', 'nmos'), root / 'generated')
    result = run(root / 'generated', root / 'replay', solver=str(solver), env_script='/nonexistent')
    if result['status'] != 'measured':
        raise AssertionError(result)
    return root / 'replay/result.json'
