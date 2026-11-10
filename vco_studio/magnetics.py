"""Magnetic-mode quality factor of the explicit (ideal L + R/K) magnetic network at a frequency.

Rule of this stage (docs/RULES.md): the largest standalone magnetic-mode Q at the actual
fundamental f0 must be in (0, 30]; the second harmonic is not restricted.
Method (same as the v11 release audit): Zmag = (inv(Rs + jw*Lmatrix) + diag(1/Rp))^-1 at f0, then
generalized eigenvalues of (Im Zmag, Re Zmag) over all magnetic port modes.
"""
from __future__ import annotations

import math


def _graphs(graph):
    return [graph] + list(graph.get('subcircuits', []))


def magnetic_q(graph, frequency_hz):
    """Return (q_max, detail) for the top-level magnetic network of a generated graph."""
    import numpy as np
    g = graph
    inds = [i for i in g['instances'] if i['role'] == 'ind']
    if not inds:
        raise ValueError('No magnetic devices in the top-level graph')
    by = {i['name']: i for i in g['instances']}
    names = [x['name'] for x in inds]
    ls = np.array([float(i['parameters']['l']) for i in inds])
    M = np.diag(ls)
    rs, rp = [], []
    for n in names:
        if 'RS' + n not in by:
            raise ValueError('inductor %s has no series loss RS%s' % (n, n))
        rs.append(float(by['RS' + n]['parameters']['r']))
        rp.append(float(by['RP' + n]['parameters']['r']) if 'RP' + n in by else float('inf'))
    for i in g['instances']:
        if i['role'] == 'mutual':
            p = i['parameters']
            if p['ind1'] in names and p['ind2'] in names:
                a, b = names.index(p['ind1']), names.index(p['ind2'])
                M[a, b] = M[b, a] = float(p['coupling']) * math.sqrt(ls[a] * ls[b])
    if np.linalg.eigvalsh(M).min() <= 0:
        raise ValueError('nonphysical inductance matrix')
    w = 2 * math.pi * float(frequency_hz)
    Zs = np.diag(rs) + 1j * w * M
    Z = np.linalg.inv(np.linalg.inv(Zs) + np.diag(1 / np.array(rp)))
    vals, V = np.linalg.eigh(Z.real)
    if vals.min() <= 0:
        raise ValueError('non-positive magnetic loss matrix')
    inv = V @ np.diag(1 / np.sqrt(vals)) @ V.T
    qs = np.linalg.eigvalsh(inv @ Z.imag @ inv)
    q = float(max(abs(qs)))
    return q, {'frequency_hz': float(frequency_hz), 'windings': names, 'mode_q': [float(x) for x in qs],
               'method': 'generalized eig(Im Zmag, Re Zmag) of standalone magnetic ports at f0'}


def loss_scale_for_q(graph, frequency_hz, current_loss_scale, q_target):
    """Loss scaling that moves the max magnetic-mode Q at f0 to q_target (Q ~ 1/loss_scale)."""
    q, _ = magnetic_q(graph, frequency_hz)
    return float(current_loss_scale) * q / float(q_target), q


Q_BASE_LIMIT = 30.0


def q_limit(graph):
    """Rule (docs/RULES.md): the magnetic-mode Q at f0 -- which already contains the (1+|k|) enhancement of a
    coupled network -- must be <= 30. Returns (limit, k_max); k is reported for information."""
    ks = [abs(float(i['parameters'].get('coupling', 0))) for i in graph['instances'] if i['role'] == 'mutual']
    return Q_BASE_LIMIT, (max(ks) if ks else 0.0)


def core_type(graph):
    """'cmos' when the top-level core has both NMOS and PMOS (excluding the bias device MB), else 'single'.
    Differential peak swing limit: ~VDD for CMOS, ~2*VDD for single-type (NMOS or PMOS) cores."""
    roles = {i['role'] for i in graph['instances'] if i['role'] in ('nmos', 'pmos') and not i['name'].startswith('MB')}
    return 'cmos' if roles == {'nmos', 'pmos'} else 'single'


def check_q(graph, frequency_hz):
    q, detail = magnetic_q(graph, frequency_hz)
    limit, k = q_limit(graph)
    return {'q_at_f0': q, 'limit': limit, 'k_max': k, 'ok': 0 < q <= limit * (1 + 1e-9), 'mode_q': detail['mode_q']}
