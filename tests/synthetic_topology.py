"""Test-only second bank connected across the same differential nodes as the main bank."""
from copy import deepcopy
from unittest.mock import patch

from vco_studio import catalog, generator


def with_two_banks():
    variant = catalog.get_variant('class_b', 'tail')
    graph = variant['graph']
    for cell in list(graph['subcircuits']):
        if cell['name'] in ('CT_R3_main', 'CT_R3U_main'):
            other = deepcopy(cell)
            other['name'] = other['name'].replace('_main', '_aux')
            for instance in other['instances']:
                if instance.get('master') == 'CT_R3U_main':
                    instance['master'] = 'CT_R3U_aux'
            graph['subcircuits'].append(other)
    main = next(instance for instance in graph['instances'] if instance.get('master') == 'CT_R3_main')
    aux = deepcopy(main)
    aux.update(name='BANK2', master='CT_R3_aux',
               nodes={pin: net.replace('main_', 'aux_', 1) if net.startswith('main_') else net
                      for pin, net in main['nodes'].items()})
    graph['instances'].append(aux)
    for port in list(graph['ports']):
        if port['name'].startswith('main_'):
            graph['ports'].append(dict(port, name=port['name'].replace('main_', 'aux_', 1)))
    main_register = graph['registers'][0]
    graph['registers'].append(dict(main_register, name='aux', role='aux',
                                   pins_msb_first=[pin.replace('main_', 'aux_', 1)
                                                   for pin in main_register['pins_msb_first']]))
    variant['defaults']['registers']['aux'] = {'bits': 4, 'value': 0}
    return variant


def use_two_banks(stack):
    variant = with_two_banks()
    for module in (catalog, generator):
        stack.enter_context(patch.object(module, 'get_variant', return_value=variant))
    return variant
