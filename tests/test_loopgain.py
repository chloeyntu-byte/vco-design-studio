"""loopgain helpers (no Spectre): DC node parsing and testbench port mapping."""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vco_studio import loopgain  # noqa: E402


class LoopgainHelpers(unittest.TestCase):
    def test_dc_nodes(self):
        p = Path(tempfile.mkdtemp()) / 'dcOp.dc'
        p.write_text('VALUE\n"OUTP" "V" 9.968e-01\n"DUT.S" "V" 2.597e-01\n"VDD_SRC:p" "I" -1e-3\nEND\n')
        n = loopgain._dc_nodes(p)
        self.assertAlmostEqual(n['DUT.S'], 0.2597)
        self.assertNotIn('VDD_SRC:p', n)

    def test_dut_map(self):
        circuit = 'subckt inv A B\nends inv\nsubckt TOP OUTN OUTP VDD VSS\nM1 (OUTP OUTN S VSS) nch\nends TOP\n'
        tb = ['VDD_SRC (VDD 0) vsource dc=1', 'DUT (OUTN OUTP VDD 0) TOP']
        inst, ports = loopgain._dut_map(tb, circuit, 'TOP')
        self.assertEqual(inst, 'DUT')
        self.assertEqual(ports['VSS'], '0')
        self.assertEqual(ports['OUTP'], 'OUTP')


if __name__ == '__main__':
    unittest.main()
