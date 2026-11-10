# SPDX-License-Identifier: GPL-3.0-or-later
"""Preservation-only GDS composition; no routing, healing or canonicalization.

Transforms reflect ABOUT the X axis first, rotate counter-clockwise, then
translate in integer nanometers. Only the selected cell hierarchy is imported.
This primitive does not establish process validity or electrical connectivity.
"""

import hashlib
import os
from pathlib import Path
import re
import secrets
import stat


class CompositionRefused(ValueError):
    """An explicit input or output obligation could not be established."""


def _digest(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _source(value):
    if not isinstance(value, dict):
        raise CompositionRefused('Source must be a mapping')
    path = value.get('path')
    if not isinstance(path, (str, Path)) or not str(path) or '\0' in str(path):
        raise CompositionRefused('Source path is empty or malformed')
    path = Path(path).absolute()
    try:
        if not stat.S_ISREG(path.lstat().st_mode):
            raise CompositionRefused('Source must be a regular non-symlink file')
    except OSError as exc:
        raise CompositionRefused(f'Cannot inspect source: {path}: {exc}') from exc
    digest = value.get('sha256')
    if not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest):
        raise CompositionRefused('Source requires a lowercase SHA256')
    top = value.get('top_cell')
    if not isinstance(top, str) or not top or '\0' in top:
        raise CompositionRefused('Source requires an explicit cell name')
    dbu = value.get('dbu_um')
    if type(dbu) not in (int, float) or dbu != .001:
        raise CompositionRefused('Only declared 1nm DBU is supported')
    if _digest(path) != digest:
        raise CompositionRefused(f'Source hash mismatch: {path}')
    return {'path': str(path), 'sha256': digest, 'top_cell': top, 'dbu_um': dbu}


def validate_recipe(carrier, dies, output_path, complete_top='COMPLETE'):
    """Pure/stdlib admission; return independent normalized records."""
    if not isinstance(complete_top, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', complete_top):
        raise CompositionRefused('Invalid complete top cell name')
    if not isinstance(output_path, (str, Path)) or not str(output_path) or '\0' in str(output_path):
        raise CompositionRefused('Invalid output path')
    output = Path(output_path).absolute()
    if os.path.lexists(output) or not output.parent.is_dir():
        raise CompositionRefused('Output must be absent in an existing directory')
    carrier = _source(carrier)
    if not isinstance(dies, list) or not dies:
        raise CompositionRefused('Require a nonempty explicit die list')
    normalized, identities = [], set()
    for die in dies:
        source = _source(die)
        identity = die.get('id')
        if not isinstance(identity, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', identity) or identity == 'carrier' or identity in identities:
            raise CompositionRefused('Invalid or repeated die identity')
        identities.add(identity)
        transform = die.get('transform')
        if not isinstance(transform, dict) or set(transform) != {'rotation_degrees', 'mirror_x', 'translation_dbu'}:
            raise CompositionRefused('Require exact orthogonal unit-magnification transform fields')
        rotation, mirror, xy = (transform[k] for k in ('rotation_degrees', 'mirror_x', 'translation_dbu'))
        if type(rotation) is not int or rotation not in (0, 90, 180, 270) or type(mirror) is not bool:
            raise CompositionRefused('Unsupported rotation or mirror')
        if not isinstance(xy, (list, tuple)) or len(xy) != 2 or any(type(x) is not int or abs(x) > 2147483647 for x in xy):
            raise CompositionRefused('Translation requires two signed integer DBU coordinates')
        source.update(id=identity, transform={'rotation_degrees': rotation, 'mirror_x': mirror, 'translation_dbu': list(xy)})
        normalized.append(source)
    return {'carrier': carrier, 'dies': normalized, 'output_path': str(output), 'complete_top': complete_top}


def compose_existing_carrier(carrier, dies, output_path, *, complete_top='COMPLETE'):
    """Compose actual selected hierarchies and publish an exclusive fresh GDS.

    Returns source hashes and explicit destination carrier/die wrapper names.
    Source layouts stay alive through serialization. No sidecars are invented.
    Callers own canonical bundle integration and independent geometry checks.
    Any exception, including library/I/O exceptions, means no success receipt;
    callers must not catch only CompositionRefused and infer success otherwise.
    """
    recipe = validate_recipe(carrier, dies, output_path, complete_top)
    import klayout.db as db

    result = db.Layout()
    result.dbu = .001
    top = result.create_cell(complete_top)
    owners, records = [], []
    sources = [recipe['carrier']] + recipe['dies']
    # Reserve every wrapper before copying any foreign cell names.
    names = [f'COMPOSE_SOURCE_{i}' for i in range(len(sources))]
    if complete_top in names:
        raise CompositionRefused('Complete top collides with reserved source cell')
    wrappers = [result.create_cell(name) for name in names]
    for index, source in enumerate(sources):
        layout = db.Layout()
        owners.append(layout)
        layout.read(source['path'])
        selected = layout.cell(source['top_cell'])
        if layout.dbu != .001 or selected is None:
            raise CompositionRefused('Actual DBU or selected cell differs from recipe')
        wrapper = wrappers[index]
        # copy_tree imports the full selected hierarchy and maps its layers.
        wrapper.copy_tree(selected)
        if index == 0:
            transform = db.Trans()
        else:
            spec = source['transform']
            transform = db.Trans(spec['rotation_degrees'] // 90, spec['mirror_x'], *spec['translation_dbu'])
        top.insert(db.CellInstArray(wrapper.cell_index(), transform))
        records.append({'id': 'carrier' if index == 0 else source['id'],
                        'cell': wrapper.name, 'source': source})
    destination = Path(recipe['output_path'])
    # Keep this file-loadable primitive self-contained. Normal creation mode
    # survives exclusive publication and lets the kernel apply umask.
    flags = os.O_CREAT | os.O_EXCL | os.O_RDWR | getattr(os, 'O_BINARY', 0)
    for _ in range(100):
        temporary = destination.parent / f'.compose-{secrets.token_hex(8)}.gds'
        try:
            fd = os.open(temporary, flags, 0o666)
            break
        except FileExistsError:
            continue
    else:
        raise FileExistsError('Could not allocate an exclusive composition temporary file')
    os.close(fd)
    try:
        options = db.SaveLayoutOptions()
        options.write_context_info = False
        result.write(str(temporary), options)
        check = db.Layout()
        check.read(str(temporary))
        if check.dbu != .001 or [cell.name for cell in check.top_cells()] != [complete_top]:
            raise CompositionRefused('Serialized composition has unexpected top or DBU')
        if any(check.cell(record['cell']) is None for record in records):
            raise CompositionRefused('Serialized composition lost an explicit source cell')
        for source in sources:
            if _digest(Path(source['path'])) != source['sha256']:
                raise CompositionRefused('Source changed during composition')
        digest = _digest(temporary)
        # Atomic no-replace publication on the same filesystem; a late occupant
        # (including a dangling symlink) is never overwritten.
        os.link(temporary, destination)
        return {'output_path': str(destination), 'output_sha256': digest,
                'top_cell': complete_top, 'dbu_um': .001,
                'carrier_cell': records[0]['cell'], 'sources': records,
                'scope': 'selected hierarchy composition, not process or LVS qualification'}
    finally:
        temporary.unlink()
