"""Diagnosis and next-action state machine for a design task.

`diagnose()` turns one measured (or failed) result into symptom codes with numbers and concrete
parameter suggestions; `next_action()` looks at the whole task and returns the single next required
step, the exact commands, the diagnosis, and the knowledge cards that apply (pushed just-in-time and
recorded as a consult receipt in the task).
"""
from __future__ import annotations

import math
from pathlib import Path

from .common import read_json

OFFSETS = (10000, 100000, 1000000, 10000000)
CENTER_TOL = 0.02          # spec: center frequency within +-2 %
AMP_LOW_FRAC = 0.4         # differential peak below 0.4*VDD -> current/loss limited
FLICKER_SLOPE = 25.0       # dB/dec between 100k and 1M above this -> 1/f^3 corner above ~100 kHz


def _pn(m, off):
    v = m.get('pn_%d_dbc_hz' % off)
    return v if isinstance(v, (int, float)) and math.isfinite(v) else None


def diagnose(result, spec, project=None, band=None):
    """Return a list of {code, detail, suggestion?} for one result dict (result.json content).
    band: sorted measured frequencies of the same candidate; with >= 2 codes the frequency check uses
    the measured band center instead of this single code (endpoints are not mistaken for centering errors)."""
    out = []
    status = result.get('status')
    if status != 'measured':
        fc = result.get('failure_class') or ''
        err = result.get('error') or ''
        if fc == 'timeout' or 'timeout' in err.lower():
            out.append({'code': 'TIMEOUT', 'detail': err, 'suggestion': 'raise --timeout (example HB/PN usually < 60 s); if it repeats, check tstab_ns and that frequency_guess_ghz is near the real oscillation'})
        elif fc == 'no_oscillation' or 'oscillation' in err:
            out.append({'code': 'NO_OSC', 'detail': err, 'suggestion': 'small-signal loop gain too low or wrong HB guess: raise core_width_scale / bias (vdc), check frequency_guess_ghz, check tank loss (loss_scale) and bank switch loading'})
        else:
            out.append({'code': 'SOLVER_ERROR', 'detail': err, 'suggestion': 'read spectre.log in the run directory; most often a model-domain error (device size outside model limits) or HB non-convergence'})
        return out
    m = result['metrics']
    p = (project or {}).get('parameters', {})
    lo, hi = spec['frequency_band_hz']
    fc = (lo + hi) / 2
    f = m['frequency_hz']
    basis = 'the current controls'
    if band and len(band) >= 2:
        f = (band[0] + band[-1]) / 2
        basis = 'the measured band center (%d codes)' % len(band)
    if spec.get('band_mode') == 'center_tr' and spec.get('center_hz'):
        fc = spec['center_hz']
    rel = (f - fc) / fc
    if abs(rel) > CENTER_TOL:
        ratio = (f / fc) ** 2
        sug = {'LC_product_scale': round(ratio, 4)}
        if isinstance(p.get('L_scale'), (int, float)):
            sug['L_scale_if_C_kept'] = round(p['L_scale'] * ratio, 4)
        if isinstance(p.get('bank_cap_scale'), (int, float)):
            sug['bank_cap_scale_if_L_kept'] = round(p['bank_cap_scale'] * ratio, 4)
        out.append({'code': 'F_HIGH' if rel > 0 else 'F_LOW',
                    'detail': 'f=%.4f GHz vs target center %.4f GHz (%+.1f %%), from %s' % (f / 1e9, fc / 1e9, rel * 100, basis),
                    'suggestion': sug, 'note': 'single-code check until endpoints are measured' if basis == 'the current controls' else 'band-center check'})
    pw = m['power_mw']
    pmax = spec['power_max_mw']
    if pw > pmax:
        out.append({'code': 'P_HIGH', 'detail': 'P=%.3f mW > %.3f mW' % (pw, pmax),
                    'suggestion': {'current_scale': round(pmax / pw * 0.97, 4), 'how': 'lower tail bias vdc or core width; power ~ VDD*I'}})
    vdd = float(p.get('vdd') or spec.get('vdd_v') or 1.0)
    amp = m.get('amplitude_diff_peak_v')
    ctype, vmax = 'single', 2 * vdd
    gen = (result.get('receipt') or {}).get('generated_dir')
    if gen and (Path(gen) / 'graph.json').is_file():
        try:
            from .magnetics import core_type
            ctype = core_type(read_json(Path(gen) / 'graph.json'))
            vmax = vdd if ctype == 'cmos' else 2 * vdd
        except Exception:
            pass
    if isinstance(amp, (int, float)) and amp > 0.9 * vmax:
        out.append({'code': 'AMP_VLIMIT', 'detail': 'differential peak %.3f V >= 0.9*%.2f V (%s core limit: %s)' % (amp, vmax, ctype, 'VDD' if ctype == 'cmos' else '2*VDD'),
                    'suggestion': 'voltage-limited: more current only adds core noise; lower the current (power) or raise tank impedance only if PN still improves'})
    if isinstance(amp, (int, float)) and amp < AMP_LOW_FRAC * vmax:
        out.append({'code': 'AMP_LOW', 'detail': 'differential peak %.3f V < %.1f*%.2f V (%s core)' % (amp, AMP_LOW_FRAC, vmax, ctype),
                    'suggestion': 'current-limited or lossy tank: check bank off-state loading (bias resistor, switch size) with bank-ac, raise current if power allows, raise L (higher tank R)'})
    targets = {int(float(k)): v for k, v in (spec.get('pn_targets_dbc_hz') or {}).items()}
    failed = [o for o, lim in targets.items() if _pn(m, o) is not None and _pn(m, o) > lim]
    if failed:
        gaps = {str(o): round(_pn(m, o) - targets[o], 2) for o in failed}
        pn100k, pn1m, pn10m = _pn(m, 100000), _pn(m, 1000000), _pn(m, 10000000)
        slope = (pn100k - pn1m) if (pn100k is not None and pn1m is not None) else None
        detail = {'gaps_db': gaps, 'slope_100k_1M_db_per_dec': None if slope is None else round(-slope, 2)}
        if slope is not None and (pn100k - pn1m) > FLICKER_SLOPE:
            out.append({'code': 'PN_FLICKER', 'detail': detail,
                        'suggestion': 'flicker up-conversion dominates near the failing offset (1/f^3 region extends past 100 kHz): filter/decouple the tail (Hegazi tail filter, larger tail L, tail cap CB), use longer-L tail devices, reduce core device flicker (longer L), or move to a harmonic-shaping family'})
        else:
            out.append({'code': 'PN_THERMAL', 'detail': detail,
                        'suggestion': 'thermal-noise limited (~20 dB/dec): raise tank Q / swing (higher L at same f, fewer lossy switches), spend available power, or upgrade to a family with higher measured FoM for this cell (launch select)'})
        nc = (result.get('noise_contributors_1m') or {}).get('classes') or {}
        if nc:
            dom = max(nc.items(), key=lambda kv: kv[1]['share'])
            table = {k: '%.0f%% (flicker %.0f%%)' % (v['share'] * 100, v['flicker_share'] * 100) for k, v in sorted(nc.items(), key=lambda kv: -kv[1]['share'])}
            if dom[0] == 'tail' and dom[1]['share'] > 0.4:
                out.append({'code': 'PN_TAIL', 'detail': {'contributors_1m': table},
                            'suggestion': 'tail current source dominates PN@1MHz' + (' (mostly flicker)' if dom[1]['flicker_share'] > 0.5 else '') + ': add/enlarge the tail filter cap (CB), lengthen the tail device (legal L), or use a tail-filtered family'})
            elif dom[0] == 'bank' and dom[1]['share'] > 0.3:
                out.append({'code': 'PN_BANK', 'detail': {'contributors_1m': table},
                            'suggestion': 'switched-bank devices/bias resistors dominate: larger bias resistors, smaller switch Ron*C, fewer active units (check with bank-ac); for the aux/tail bank re-tune the aux code'})
            elif dom[0] == 'core' and dom[1]['share'] > 0.5:
                out.append({'code': 'PN_CORE', 'detail': {'contributors_1m': table},
                            'suggestion': 'core devices dominate' + (' with large flicker share: longer-L core devices or a harmonic-shaping family' if dom[1]['flicker_share'] > 0.4 else ': raise tank swing/Q (L up), avoid deep triode, check gm margin is not excessive')})
            elif dom[0] == 'tank_loss' and dom[1]['share'] > 0.4:
                out.append({'code': 'PN_TANK', 'detail': {'contributors_1m': table},
                            'suggestion': 'tank loss dominates: magnetics Q is the limit (Q <= 30 rule at f0); raise swing or move to a higher-FoM family'})
        if pw < 0.7 * pmax:
            out.append({'code': 'P_HEADROOM', 'detail': 'P=%.3f mW uses only %.0f %% of the budget while PN fails' % (pw, pw / pmax * 100),
                        'suggestion': {'current_scale_up_to': round(pmax / pw * 0.95, 3)}})
    gen = (result.get('receipt') or {}).get('generated_dir')
    if gen and (Path(gen) / 'graph.json').is_file():
        try:
            from .magnetics import check_q
            qc = check_q(read_json(Path(gen) / 'graph.json'), m['frequency_hz'])
            if not qc['ok']:
                out.append({'code': 'MAG_Q_HIGH', 'detail': 'magnetic mode Q %.1f at f0 (includes the 1+|k| enhancement, k=%.2f) exceeds %.0f' % (qc['q_at_f0'], qc['k_max'], qc['limit']),
                            'suggestion': 'raise loss_scale (Q ~ 1/loss_scale) until Q <= limit; results above the limit do not qualify'})
        except Exception:
            pass
    for w in result.get('warnings', []) or []:
        if 'steady state' in w.lower():
            out.append({'code': 'STEADY_WARN', 'detail': w, 'suggestion': 'startup not proven by HB; for delivery run one transient startup check (simulation.save_startup_waveform=true) on the worst code'})
        elif 'model warning' in w.lower():
            out.append({'code': 'MODEL_WARN', 'detail': w, 'suggestion': 'inspect spectre.log CMI warnings: device near/outside model domain'})
    return out


def _latest_results(candidate):
    rows = []
    for file in candidate.get('results', []):
        try:
            rows.append((file, read_json(file)))
        except (OSError, ValueError):
            continue
    return rows


def next_action(workspace, state, launch='launch'):
    """Decide the next step. Returns a dict with action, commands, why, diagnosis, knowledge."""
    from .flow import budget_usage
    root = Path(workspace).resolve()
    spec = state['spec']
    W = str(root)
    usage = budget_usage(state)
    remaining = usage.get('solver_processes_remaining')
    lo, hi = spec['frequency_band_hz']
    ctx = {'pdk': spec.get('pdk'), 'frequency_ghz': (lo + hi) / 2e9, 'power_mw': spec.get('power_max_mw')}
    lock = ''
    if state.get('completed'):
        return _pack(root, 'deliver', 'task is frozen; build the delivery package',
                     ['%s deliver %s' % (launch, W)], [], dict(ctx, stage='delivery'), usage)
    if remaining is not None and remaining <= 1:
        return _pack(root, 'wrap_up', 'solver budget nearly exhausted (%s left): freeze what is qualified or deliver the gap report' % remaining,
                     ['%s flow-status %s --brief' % (launch, W), '%s deliver %s --provisional' % (launch, W)], [], dict(ctx, stage='delivery'), usage)
    active = [c for c in state['candidates'] if c.get('disposition') != 'rejected']
    if not spec.get('budget', {}).get('solver_processes'):
        ctx['budget_warning'] = True
    if not active:
        return _pack(root, 'select', 'no active candidate: rank structures from measured data, then create a project with --spec',
                     ['%s select --spec %s/spec.json' % (launch, W) if (root / 'spec.json').exists() else '%s select --pdk %s --frequency-ghz %.3f --power-mw %s' % (launch, spec.get('pdk'), (lo + hi) / 2e9, spec.get('power_max_mw')),
                      '%s seed --task %s --family <family> --output <proj.json>   # sizing from the nearest measured design' % (launch, W),
                      '%s flow-add %s --project <proj.json> --reason "<evidence -> hypothesis>" --evidence <card/evidence id>' % (launch, W)],
                     [], dict(ctx, stage='selection'), usage)
    cand = active[-1]
    rows = _latest_results(cand)
    project = read_json(Path(cand['directory']) / 'project.json')
    cctx = dict(ctx, family=cand.get('family'))
    if not rows:
        return _pack(root, 'measure', 'candidate %s has no measurement yet' % cand['id'],
                     [lock + '%s flow-measure %s %s' % (launch, W, cand['id'])], [], dict(cctx, stage='characterization'), usage)
    file, last = rows[-1]
    band = sorted(r['metrics']['frequency_hz'] for _, r in rows if r.get('status') == 'measured')
    diag = diagnose(last, spec, project, band=band)
    ok_rows = [r for _, r in rows if r.get('status') == 'measured']
    if len(ok_rows) > 1:
        # constraints apply to every adopted code: also diagnose the worst-power and worst-PN codes
        worst_p = max(ok_rows, key=lambda r: r['metrics']['power_mw'])
        worst_pn = max(ok_rows, key=lambda r: r['metrics'].get('pn_1000000_dbc_hz', -1e9))
        seen = {d['code'] for d in diag}
        for tag, wr in (('worst power code', worst_p), ('worst PN code', worst_pn)):
            if wr is last:
                continue
            for d in diagnose(wr, spec, project, band=band):
                if d['code'] not in seen and not d['code'].startswith('F_'):
                    d = dict(d, from_code=tag, controls={k: v.get('value') for k, v in (wr.get('registers') or {}).items()})
                    diag.append(d)
                    seen.add(d['code'])
    codes = [d['code'] for d in diag]
    measured = [r for _, r in rows if r.get('status') == 'measured']
    if last.get('status') != 'measured':
        return _pack(root, 'fix_failure', 'last run failed (%s)' % ', '.join(codes),
                     ['inspect %s' % str(Path(file).parent / 'spectre.log'),
                      '%s project %s --variant %s --spec <spec> --set <param>=<value> --output <new.json>  then flow-add' % (launch, cand['family'], cand.get('variant'))],
                     diag, dict(cctx, stage='characterization', symptoms=codes), usage)
    fs = sorted(r['metrics']['frequency_hz'] for r in measured)
    if spec.get('band_mode') == 'center_tr' and len(fs) >= 2:
        c = (fs[0] + fs[-1]) / 2
        covers = abs(c / spec['center_hz'] - 1) <= spec.get('center_tolerance', 0.02) and (fs[-1] - fs[0]) / c * 100 >= spec['tr_min_pct']
    else:
        covers = fs[0] <= lo and fs[-1] >= hi
    hard = [c for c in codes if c in ('F_HIGH', 'F_LOW', 'P_HIGH', 'PN_FLICKER', 'PN_THERMAL', 'AMP_LOW')]
    upgrade = _upgrade_hint(spec, cand, last) if any(c.startswith('PN_') for c in codes) else None
    if upgrade:
        diag = diag + [upgrade]
    if hard:
        return _pack(root, 'improve', 'measured violations: %s' % ', '.join(codes),
                     ['apply the suggestion numbers below in a NEW project (launch project ... --spec ... --set k=v), flow-add it with --reason, then measure',
                      '%s select --task %s   # if the PN gap is larger than tuning can close, compare structures' % (launch, W),
                      '%s flow-mark %s %s rejected --reason "<why>"   # when abandoning this candidate' % (launch, W, cand['id'])],
                     diag, dict(cctx, stage='tuning', symptoms=codes), usage)
    if not covers:
        bits = project['registers'].get('main', {}).get('bits', 4)
        return _pack(root, 'sweep_endpoints', 'single-point pass; measure the band endpoints (main=0 and main=%d) to check coverage' % (2 ** bits - 1),
                     ['echo \'[{"main":0},{"main":%d}]\' > %s/ends.json' % (2 ** bits - 1, W),
                      lock + '%s flow-sweep %s %s --codes %s/ends.json' % (launch, W, cand['id'], W)],
                     diag, dict(cctx, stage='coverage', symptoms=codes or ['PASS_POINT']), usage)
    if not any(e.get('action') == 'convergence_check' for e in state.get('events', [])):
        return _pack(root, 'converge', 'band covered and constraints met at the measured codes; add an intermediate-code sweep if max_step matters, then re-check worst points with different HB settings',
                     ['write the adopted result.json paths to %s/adopted.json and the worst 1-2 of them to %s/picked.json' % (W, W),
                      lock + '%s flow-converge %s %s --results %s/picked.json' % (launch, W, cand['id'], W)],
                     diag, dict(cctx, stage='convergence'), usage)
    return _pack(root, 'freeze', 'convergence evidence exists; qualify the adopted grid',
                 ['%s flow-freeze %s %s --results %s/adopted.json --convergence %s/conv.json' % (launch, W, cand['id'], W, W),
                  '%s deliver %s' % (launch, W)],
                 diag, dict(cctx, stage='delivery'), usage)


def _pack(root, action, why, commands, diagnosis, ctx, usage):
    from .knowledge import consult
    stage = ctx.pop('stage', action)
    warn = ctx.get('budget_warning')
    k = consult(root, stage, {kk: vv for kk, vv in ctx.items() if kk != 'budget_warning'})
    ctx.pop('budget_warning', None)
    out_warn = ['no solver budget in the spec: add "budget": {"solver_processes": N} so the flow can pace the work'] if warn else []
    return {'action': action, 'why': why, 'commands': commands, 'diagnosis': diagnosis, 'warnings': out_warn,
            'knowledge': k['cards'], 'budget': {k2: usage.get(k2) for k2 in ('solver_processes', 'solver_processes_remaining')},
            'note': 'Cards are pushed for this stage/symptom and recorded in knowledge/consults.jsonl; cite the card ids you act on in flow-add --evidence and DECISIONS.md.'}


def _upgrade_hint(spec, cand, result):
    """Compare the measured FoM with the lookup table: tune further or change family?"""
    try:
        from .select import rank
    except Exception:
        return None
    m = result.get('metrics') or {}
    targets = {int(float(k)): v for k, v in (spec.get('pn_targets_dbc_hz') or {}).items()}
    if 1000000 not in targets or 'fom_1000000_db' not in m:
        return None
    lo, hi = spec['frequency_band_hz']
    f0, p = (lo + hi) / 2, spec['power_max_mw']
    need_fom = -targets[1000000] + 20 * math.log10(f0 / 1e6) - 10 * math.log10(p)
    ranking = [r for r in rank(spec.get('pdk'), f0, p, targets[1000000], allow_aux=spec.get('allow_aux', True)) if r.get('fom_1m_worst')]
    if not ranking:
        return {'code': 'UPGRADE_UNKNOWN', 'detail': 'no lookup-table cells for this PDK', 'suggestion': 'use evidence-query near_misses and the architecture cards'}
    mine = next((r for r in ranking if r['family'] == cand.get('family')), None)
    best = ranking[0]
    detail = {'needed_fom_at_full_power': round(need_fom, 2), 'measured_fom': round(m['fom_1000000_db'], 2),
              'table_best': {k: best.get(k) for k in ('family', 'variant', 'fom_1m_worst', 'nearest_cell', 'margin_db')},
              'table_current_family': {k: mine.get(k) for k in ('fom_1m_worst', 'nearest_cell', 'margin_db')} if mine else None}
    reach = lambda r: r['fom_1m_worst'] >= need_fom - 0.5
    if mine and reach(mine) and m['fom_1000000_db'] < mine['fom_1m_worst'] - 1.0:
        sug = 'keep the family: the table shows %s reaching %.1f dB FoM near this cell (need %.1f, measured %.1f); move toward the table design (`launch seed --task <task> --family %s`)' % (cand.get('family'), mine['fom_1m_worst'], need_fom, m['fom_1000000_db'], cand.get('family'))
    elif reach(best) and best['family'] != cand.get('family'):
        sug = 'structure upgrade: %s/%s reaches %.1f dB FoM near this cell (need %.1f; this family %s); seed it with `launch seed --task <task> --family %s --variant %s`' % (
            best['family'], best['variant'], best['fom_1m_worst'], need_fom, ('%.1f in the table' % mine['fom_1m_worst']) if mine else 'not in the table', best['family'], best['variant'])
    elif mine and m['fom_1000000_db'] < mine['fom_1m_worst'] - 1.0:
        sug = 'tune toward the table design (%.1f dB possible vs measured %.1f), but no family reaches the needed %.1f dB near this cell: agree a relaxed PN/power with the requester or report the gap' % (mine['fom_1m_worst'], m['fom_1000000_db'], need_fom)
    else:
        sug = 'no family in the table reaches the needed FoM (%.1f dB) near this cell: relax PN or power with the requester, or spend budget on the best family (%s, %.1f dB) and report the gap' % (need_fom, best['family'], best['fom_1m_worst'])
    return {'code': 'UPGRADE_CHECK', 'detail': detail, 'suggestion': sug}
