# SPDX-License-Identifier: GPL-3.0-or-later
"""Stdlib admission controls; native geometry is explicitly opt-in."""
import copy
import hashlib
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import types

spec = importlib.util.spec_from_file_location('carrier_composition', Path(__file__).parents[1] / 'compose_existing_carrier.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class Admission(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        path = self.root / 'source.gds'
        path.write_bytes(b'not native: admission only')
        self.carrier = dict(path=str(path), sha256=module._digest(path), top_cell='SOURCE', dbu_um=.001)
        self.die = dict(self.carrier, id='U1', transform=dict(rotation_degrees=180, mirror_x=True, translation_dbu=[4500000, -2600000]))
        self.out = self.root / 'complete.gds'

    def test_valid_recipe_is_independent(self):
        original = copy.deepcopy(self.die)
        result = module.validate_recipe(self.carrier, [self.die], self.out)
        result['dies'][0]['transform']['translation_dbu'][0] = 1
        self.assertEqual(self.die, original)
        self.assertFalse(self.out.exists())

    def test_invalid_transform_and_duplicates(self):
        for field, value in [('rotation_degrees', 45), ('rotation_degrees', True), ('mirror_x', 1), ('translation_dbu', [1.5, 2]), ('translation_dbu', [True, 2]), ('magnification', 2)]:
            with self.subTest(field=field, value=value):
                die = copy.deepcopy(self.die)
                die['transform'][field] = value
                with self.assertRaises(module.CompositionRefused):
                    module.validate_recipe(self.carrier, [die], self.out)
        with self.assertRaises(module.CompositionRefused):
            module.validate_recipe(self.carrier, [self.die, self.die], self.out)

    def test_missing_wrong_hash_and_dbu(self):
        for field, value in [('path', str(self.root/'missing')), ('sha256', '0'*64), ('dbu_um', .002), ('top_cell', '')]:
            with self.subTest(field=field):
                source = dict(self.carrier, **{field: value})
                with self.assertRaises(module.CompositionRefused):
                    module.validate_recipe(source, [self.die], self.out)

    def test_existing_and_dangling_output_refused(self):
        self.out.write_bytes(b'KEEP')
        with self.assertRaises(module.CompositionRefused):
            module.validate_recipe(self.carrier, [self.die], self.out)
        self.assertEqual(self.out.read_bytes(), b'KEEP')
        self.out.unlink()
        self.out.symlink_to(self.root/'missing')
        with self.assertRaises(module.CompositionRefused):
            module.validate_recipe(self.carrier, [self.die], self.out)

    def fake_native(self, on_write):
        """Mock only native serialization; filesystem publication remains real."""
        db = types.ModuleType('klayout.db')
        def layout():
            obj = MagicMock()
            obj.dbu = .001
            def cell(name):
                item = MagicMock(); item.name = name
                return item
            obj.create_cell.side_effect = cell
            obj.cell.side_effect = cell
            obj.top_cells.return_value = [cell('COMPLETE')]
            obj.write.side_effect = on_write
            return obj
        db.Layout = layout
        db.Trans = MagicMock()
        db.CellInstArray = MagicMock()
        db.SaveLayoutOptions = MagicMock()
        package = types.ModuleType('klayout'); package.db = db
        return patch.dict('sys.modules', {'klayout': package, 'klayout.db': db})

    def test_mocked_late_occupant_is_preserved(self):
        def write(path, options):
            Path(path).write_bytes(b'composed')
        original_link = os.link
        def occupied_link(source, destination):
            Path(destination).write_bytes(b'LATE OCCUPANT')
            return original_link(source, destination)
        with self.fake_native(write), patch.object(module.os, 'link', side_effect=occupied_link):
            with self.assertRaises(FileExistsError):
                module.compose_existing_carrier(self.carrier, [self.die], self.out)
        self.assertEqual(self.out.read_bytes(), b'LATE OCCUPANT')
        self.assertEqual(list(self.root.glob('.compose-*')), [])

    def test_mocked_source_changed_after_admission_refuses(self):
        def write(path, options):
            Path(path).write_bytes(b'composed')
            Path(self.carrier['path']).write_bytes(b'CHANGED')
        with self.fake_native(write), patch.object(module.os, 'link') as publish:
            with self.assertRaisesRegex(module.CompositionRefused, 'changed during'):
                module.compose_existing_carrier(self.carrier, [self.die], self.out)
            publish.assert_not_called()
        self.assertFalse(self.out.exists())
        self.assertEqual(list(self.root.glob('.compose-*')), [])


@unittest.skipUnless(os.environ.get('CHIPLET_COMPOSITION_NATIVE_TESTS') == '1', 'explicit native allocation required')
class NativeComposition(unittest.TestCase):
    def test_all_eight_transforms_multilayer_hierarchy(self):
        import klayout.db as db
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            layout = db.Layout(); layout.dbu = .001
            top = layout.create_cell('SOURCE')
            middle = layout.create_cell('MIDDLE')
            leaf = layout.create_cell('LEAF')
            unrelated = layout.create_cell('UNRELATED')
            boxes = {126: (11, 23, 47, 89), 134: (-29, 7, -3, 19)}
            for layer, coords in boxes.items():
                leaf.shapes(layout.layer(layer, 0)).insert(db.Box(*coords))
            unrelated.shapes(layout.layer(999, 0)).insert(db.Box(0, 0, 1000, 1000))
            middle.insert(db.CellInstArray(leaf.cell_index(), db.Trans(0, False, 5, 9)))
            top.insert(db.CellInstArray(middle.cell_index(), db.Trans(0, False, 13, -2)))
            path = root/'source.gds'; layout.write(str(path))
            source = dict(path=str(path), sha256=module._digest(path), top_cell='SOURCE', dbu_um=.001)
            for rotation in (0, 90, 180, 270):
                for mirror in (False, True):
                    with self.subTest(rotation=rotation, mirror=mirror):
                        transform = dict(rotation_degrees=rotation, mirror_x=mirror, translation_dbu=[300, -400])
                        die = dict(source, id='U1', transform=transform)
                        result = module.compose_existing_carrier(source, [die], root/f'{rotation}-{mirror}.gds')
                        actual = db.Layout(); actual.read(result['output_path'])
                        full = actual.cell('COMPLETE')
                        instances = {actual.cell(i.cell_index).name: i.trans for i in full.each_inst()}
                        self.assertEqual(len(instances), 2)
                        die_trans = instances[result['sources'][1]['cell']]
                        def expected(x, y):
                            if mirror: y = -y
                            x, y = ((x, y), (-y, x), (-x, -y), (y, -x))[rotation // 90]
                            return (x + 300, y - 400)
                        # Serialized instance action, not just its declared recipe.
                        for x, y in [(0, 0), (17, 31)]:
                            point = die_trans * db.Point(x, y)
                            self.assertEqual((point.x, point.y), expected(x, y))
                        for layer, (left, bottom, right, upper) in boxes.items():
                            coords = [(left+18,bottom+7),(right+18,bottom+7),(right+18,upper+7),(left+18,upper+7)]
                            carrier_region = db.Region(db.Polygon([db.Point(*p) for p in coords]))
                            die_region = db.Region(db.Polygon([db.Point(*expected(*p)) for p in coords]))
                            li = actual.layer(layer, 0)
                            self.assertTrue((db.Region(full.begin_shapes_rec(li)) ^ (carrier_region | die_region)).is_empty())
                            self.assertTrue((db.Region(actual.cell(result['carrier_cell']).begin_shapes_rec(li)) ^ carrier_region).is_empty())
                        self.assertTrue(db.Region(full.begin_shapes_rec(actual.layer(999, 0))).is_empty())
                        self.assertEqual(module._digest(path), source['sha256'])

    def test_actual_dbu_mismatch_refuses(self):
        import klayout.db as db
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            layout = db.Layout(); layout.dbu = .002
            top = layout.create_cell('SOURCE')
            top.shapes(layout.layer(126, 0)).insert(db.Box(0, 0, 10, 20))
            path = root/'source.gds'; layout.write(str(path))
            source = dict(path=str(path), sha256=module._digest(path), top_cell='SOURCE', dbu_um=.001)
            die = dict(source, id='U1', transform=dict(rotation_degrees=0, mirror_x=False, translation_dbu=[0, 0]))
            with self.assertRaisesRegex(module.CompositionRefused, 'Actual DBU'):
                module.compose_existing_carrier(source, [die], root/'out.gds')
            self.assertFalse((root/'out.gds').exists())

    def test_hierarchy_overlap_and_selected_cell(self):
        import klayout.db as db
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            layout = db.Layout(); layout.dbu = .001
            top = layout.create_cell('SOURCE')
            child = layout.create_cell('COMPOSE_SOURCE_1')
            child.shapes(layout.layer(126, 0)).insert(db.Box(0, 0, 100, 100))
            top.insert(db.CellInstArray(child.cell_index(), db.Trans()))
            path = root/'input.gds'; layout.write(str(path))
            source = dict(path=str(path), sha256=module._digest(path), top_cell='SOURCE', dbu_um=.001)
            die = dict(source, id='U1', transform=dict(rotation_degrees=180, mirror_x=True, translation_dbu=[200, 0]))
            result = module.compose_existing_carrier(source, [die], root/'complete.gds')
            output = db.Layout(); output.read(result['output_path'])
            layer = output.layer(126, 0)
            carrier = db.Region(output.cell(result['carrier_cell']).begin_shapes_rec(layer))
            whole = db.Region(output.cell('COMPLETE').begin_shapes_rec(layer))
            self.assertTrue((carrier ^ db.Region(db.Box(0, 0, 100, 100))).is_empty())
            self.assertTrue((whole ^ db.Region(db.Box(0, 0, 200, 100))).is_empty())
            self.assertEqual(module._digest(path), source['sha256'])
            with self.assertRaises(module.CompositionRefused):
                module.compose_existing_carrier(dict(source, top_cell='MISSING'), [die], root/'missing.gds')
            self.assertFalse((root/'missing.gds').exists())


if __name__ == '__main__':
    unittest.main()
