import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from vco_studio import site
from vco_studio.catalog import default_project
from vco_studio.generator import make_graph, validate_graph
from vco_studio.spec import validate


class UserBindingTests(unittest.TestCase):
    def test_user_identifier_resolves_without_technology_aliases(self):
        with tempfile.TemporaryDirectory() as folder:
            profile=Path(folder)/'site.json'
            binding=Path(__file__).resolve().parents[1]/'examples/bindings/demo_a.json'
            profile.write_text(json.dumps({'bindings':{'my_devices':str(binding)}}))
            with patch.dict(os.environ,{'VCO_SITE':str(profile)}):
                site.reset_cache()
                try:
                    spec,_=validate({'pdk':'my_devices','frequency_band_hz':[7e9,9e9],'power_max_mw':5,'vdd_v':1.1})
                    project=default_project('murphy','nmos',spec=spec)
                    graph,resolved=make_graph(project)
                    self.assertEqual(project['pdk'],'my_devices')
                    self.assertEqual(project['parameters']['vdd'],1.1)
                    self.assertTrue(validate_graph(graph,resolved['binding'])['valid'])
                finally:
                    site.reset_cache()

    def test_existing_user_binding_file_is_a_valid_spec_identity(self):
        binding=Path(__file__).resolve().parents[1]/'examples/bindings/demo_a.json'
        spec,_=validate({'pdk':str(binding),'frequency_band_hz':[7e9,9e9],'power_max_mw':5,'vdd_v':1.1})
        self.assertEqual(spec['pdk'],str(binding))
