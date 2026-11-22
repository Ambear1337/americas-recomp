import importlib.util
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parent.parent

spec = importlib.util.spec_from_file_location("xbox_dxil_lower", TOOLS / "xbox" / "platform" / "xbox_dxil_lower.py")
lower_module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = lower_module
spec.loader.exec_module(lower_module)


def module(declarations):
    lines = [
        'source_filename = "test"',
        'target triple = "dxil-ms-xb2"',
        '%dx.types.Handle = type { i8* }',
        '',
    ]
    lines.extend(declarations)
    lines.extend([
        '',
        'define void @main() {',
        '  %0 = call i32 @xbox.Med3_U32(i32 0, i32 1, i32 2, i32 3)',
        '  ret void',
        '}',
        '',
        'attributes #0 = { nounwind readnone }',
        '',
    ])
    return "\n".join(lines)


class XboxDxilLowerCliTests(unittest.TestCase):
    def run_cli(self, source):
        with tempfile.TemporaryDirectory(prefix="dxil_cli_") as td:
            src = Path(td) / "input.ll"
            dst = Path(td) / "output.ll"
            src.write_text(source, encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(TOOLS / "xbox" / "platform" / "xbox_dxil_lower.py"), str(src), str(dst)],
                capture_output=True, text=True, timeout=30,
            )
            return result, dst.read_text(encoding="utf-8")

    def test_unknown_intrinsic_returns_failure_and_preserves_input(self):
        source = module(['declare i32 @xbox.Med3_U32(i32, i32, i32, i32) #0'])
        source = source.replace("xbox.Med3_U32", "xbox.synthetic_never_lowered")
        result, output = self.run_cli(source)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("xbox.synthetic_never_lowered", result.stdout)
        self.assertIn("lowered 0 call(s)", result.stdout)
        self.assertIn("call i32 @xbox.synthetic_never_lowered(", output)
        self.assertIn("declare i32 @xbox.synthetic_never_lowered(", output)

    def test_supported_intrinsic_returns_success(self):
        source = module(['declare i32 @xbox.Med3_U32(i32, i32, i32, i32) #0'])
        result, output = self.run_cli(source)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("lowered 1 call(s)", result.stdout)
        self.assertIn("intrinsics still called: none", result.stdout)
        self.assertNotIn("@xbox.", output)
        self.assertIn("@dx.op.binary.i32(", output)


class XboxDxilLowerDeclarationTests(unittest.TestCase):
    def lowered(self, declarations):
        result, count, left = lower_module.lower(module(declarations))
        self.assertEqual(count, 1)
        self.assertEqual(left, set())
        return result

    def assert_calls_declared(self, result):
        called = set(re.findall(r"call [^@\n]*@(dx\.op\.[\w.]+)\(", result))
        declared = set(re.findall(r"^declare [^\n]*@(dx\.op\.[\w.]+)\(", result, re.M))
        self.assertTrue(called)
        self.assertLessEqual(called, declared, "called DXIL operations must be declared")

    def assert_decls_before_first_define(self, result):
        self.assertLess(result.index("declare i32 @dx.op.binary.i32("), result.index("define void @main()"))

    def test_surviving_declaration_is_kept(self):
        result = self.lowered([
            'declare i32 @keep(i32) #0',
            'declare i32 @xbox.Med3_U32(i32, i32, i32, i32) #0',
        ])
        self.assert_calls_declared(result)
        self.assert_decls_before_first_define(result)
        self.assertIn("declare i32 @keep(i32) #0", result)
        self.assertNotIn("@xbox.", result)

    def test_removed_intrinsic_was_the_only_declaration(self):
        result = self.lowered([
            'declare i32 @xbox.Med3_U32(i32, i32, i32, i32) #0',
        ])
        self.assert_calls_declared(result)
        self.assert_decls_before_first_define(result)

    def test_no_declarations_at_all(self):
        result = self.lowered([])
        self.assert_calls_declared(result)
        self.assert_decls_before_first_define(result)

    def test_wave_max_u32_lowering(self):
        text = "\n".join([
            'target triple = "dxil-ms-xb2"',
            '%dx.types.Handle = type { i8* }',
            'define void @main() {',
            '  %1 = call i32 @xbox.WaveMax_U32(i32 1, i32 42)',
            '  ret void',
            '}',
            'declare i32 @xbox.WaveMax_U32(i32, i32) #0',
            'attributes #0 = { nounwind readnone }',
        ])
        result, count, left = lower_module.lower(text)
        self.assertEqual(count, 1)
        self.assertEqual(left, set())
        self.assertIn("declare i32 @dx.op.waveActiveOp.i32(i32, i32, i8, i8)", result)
        self.assertRegex(result, r"call i32 @dx\.op\.waveActiveOp\.i32\(i32 119, i32 42, i8 3, i8 1\)")
        self.assertNotIn("@xbox.", result)


RENUMBER_MODULE = """source_filename = "test"
target triple = "dxil-ms-dx"
%dx.types.Handle = type { i8* }

define void @main() {
  %1 = add i32 1, 2
  %7 = add i32 %1, 3
  ret void
}

define i32 @second(i32 %0, i32 %3) {
  %5 = add i32 %0, %3
  %sum = add i32 %5, 7
  %9 = add i32 %sum, 1
  ret i32 %9
}

define void @branches() {
; <label>:7
  br label %9
; <label>:9
  ret void
}

define i32 @phi_switch(i32 %0) {
; <label>:4
  %6 = phi i32 [ %0, %4 ], [ 1, %8 ]
  switch i32 %6, label %8 [ i32 1, label %9
                            i32 2, label %4 ]
; <label>:8
  %11 = add i32 %6, 1
  ret i32 %11
; <label>:9
  ret i32 %0
}

define void @branch_entry() {
  br label %5
; <label>:5
  %7 = add i32 1, 2
  ret void
}

define i32 @omitted_args(i32, i32 %4, i32) {
  %8 = add i32 %0, %4
  %9 = add i32 %8, %5
  ret i32 %9
}

define i32 @attr_align() alignstack(16) {
  %1 = add i32 1, 2
  ret i32 %1
}

define void @typed_param(%dx.types.Handle %4) {
  %7 = add i32 1, 2
  ret void
}

define void @bare_labels() {
  br label %5
5:
  %7 = add i32 1, 2
  ret void
}

define i32 @implicit_entry_phi(i32 %4) {
  %7 = phi i32 [ %4, %5 ]
  ret i32 %7
}

define void @indented_labels() {
  br label %8
  8:
  %9 = add i32 1, 2
  ret void
}

"""


def function_body(text, name):
    m = re.search(r"^define [^\n]*@" + re.escape(name) + r"\([^\n]*\)[^\n]*\{", text, re.M)
    start = m.end()
    return text[start:text.index("\n}\n", start)]


def function_ids(text, name):
    m = re.search(r"^define [^\n]*@" + re.escape(name) + r"\(([^\n]*)\)[^\n]*\{", text, re.M)
    ids = [x[1:] for x in re.findall(r"%\d+\b", m.group(1))]
    lines = [line for line in function_body(text, name).split("\n") if line]
    if lines and not re.match(r"^(; <label>:|%?\d+:)", lines[0]):
        ids.append(str(len(ids)))
    for line in lines:
        label = re.match(r"^; <label>:(\d+)", line)
        value = re.match(r"^\s*%(\d+) = ", line)
        numbered_label = re.match(r"^%?(\d+):", line)
        if label:
            ids.append(label.group(1))
        elif value:
            ids.append(value.group(1))
        elif numbered_label:
            ids.append(numbered_label.group(1))
    return ids


class XboxDxilLowerRenumberTests(unittest.TestCase):
    def test_single_function_numbering_unchanged(self):
        result = lower_module.renumber(RENUMBER_MODULE)
        self.assertEqual(function_ids(result, "main"), ["0", "1", "2"])
        self.assertNotIn("%7", function_body(result, "main"))
        self.assertIn("%2 = add i32 %1, 3", function_body(result, "main"))

    def test_second_function_arguments_and_holes_renumbered(self):
        result = lower_module.renumber(RENUMBER_MODULE)
        self.assertEqual(function_ids(result, "second"), ["0", "1", "2", "3", "4"])
        self.assertIn("define i32 @second(i32 %0, i32 %1) {", result)
        body = function_body(result, "second")
        self.assertIn("%3 = add i32 %0, %1", body)
        self.assertIn("%sum = add i32 %3, 7", body)
        self.assertIn("%4 = add i32 %sum, 1", body)
        self.assertIn("ret i32 %4", body)
        self.assertNotIn("%9", body)

    def test_numeric_labels_and_branch_targets_renumbered(self):
        result = lower_module.renumber(RENUMBER_MODULE)
        self.assertEqual(function_ids(result, "branches"), ["0", "1"])
        body = function_body(result, "branches")
        self.assertIn("; <label>:0", body)
        self.assertIn("br label %1", body)
        self.assertIn("; <label>:1", body)

    def test_phi_incoming_blocks_and_switch_targets_renumbered(self):
        result = lower_module.renumber(RENUMBER_MODULE)
        self.assertEqual(function_ids(result, "phi_switch"), ["0", "1", "2", "3", "4", "5"])
        body = function_body(result, "phi_switch")
        self.assertIn("%2 = phi i32 [ %0, %1 ], [ 1, %3 ]", body)
        self.assertIn("switch i32 %2, label %3 [ i32 1, label %5", body)
        self.assertIn("i32 2, label %1 ]", body)
        self.assertIn("%4 = add i32 %2, 1", body)
        self.assertIn("ret i32 %4", body)

    def test_entry_branch_reserves_its_slot(self):
        result = lower_module.renumber(RENUMBER_MODULE)
        self.assertEqual(function_ids(result, "branch_entry"), ["0", "1", "2"])
        body = function_body(result, "branch_entry")
        self.assertIn("br label %1", body)
        self.assertIn("; <label>:1", body)
        self.assertIn("%2 = add i32 1, 2", body)

    def test_omitted_parameter_ids_are_counted(self):
        result = lower_module.renumber(RENUMBER_MODULE)
        self.assertIn("define i32 @omitted_args(i32, i32 %1, i32) {", result)
        body = function_body(result, "omitted_args")
        self.assertIn("%4 = add i32 %0, %1", body)
        self.assertIn("%5 = add i32 %4, %2", body)
        self.assertIn("ret i32 %5", body)

    def test_trailing_function_attribute_is_not_a_parameter(self):
        result = lower_module.renumber(RENUMBER_MODULE)
        self.assertEqual(function_ids(result, "attr_align"), ["0", "1"])
        body = function_body(result, "attr_align")
        self.assertIn("%1 = add i32 1, 2", body)
        self.assertIn("ret i32 %1", body)

    def test_named_struct_type_parameter_counts_its_value_id(self):
        result = lower_module.renumber(RENUMBER_MODULE)
        self.assertIn("define void @typed_param(%dx.types.Handle %0) {", result)
        self.assertEqual(function_ids(result, "typed_param"), ["0", "1", "2"])
        self.assertIn("%2 = add i32 1, 2", function_body(result, "typed_param"))

    def test_bare_numeric_block_labels_renumbered(self):
        result = lower_module.renumber(RENUMBER_MODULE)
        self.assertEqual(function_ids(result, "bare_labels"), ["0", "1", "2"])
        body = function_body(result, "bare_labels")
        self.assertIn("br label %1", body)
        self.assertIn("1:", body)
        self.assertIn("%2 = add i32 1, 2", body)

    def test_implicit_entry_id_is_mapped(self):
        result = lower_module.renumber(RENUMBER_MODULE)
        self.assertIn("define i32 @implicit_entry_phi(i32 %0) {", result)
        body = function_body(result, "implicit_entry_phi")
        self.assertIn("phi i32 [ %0, %1 ]", body)
        self.assertIn("ret i32 %2", body)

    def test_indented_numeric_label_is_rewritten(self):
        result = lower_module.renumber(RENUMBER_MODULE)
        body = function_body(result, "indented_labels")
        self.assertIn("br label %1", body)
        self.assertIn("  1:", body)
        self.assertIn("%2 = add i32 1, 2", body)

    def test_references_only_defined_ids(self):
        result = lower_module.renumber(RENUMBER_MODULE)
        for name in ("main", "second", "branches", "phi_switch"):
            with self.subTest(function=name):
                defined = set(function_ids(result, name))
                referenced = set(re.findall(r"%(\d+)\b", function_body(result, name)))
                self.assertLessEqual(referenced, defined)


def wave_module(threads, body):
    return "\n".join([
        'define void @main() {',
        *body,
        '  ret void',
        '}',
        '!dx.entryPoints = !{!1}',
        '!1 = !{void ()* @main, !"main", null, null, !2}',
        '!2 = !{i32 0, i64 8388624, i32 4, !3}',
        '!3 = !{i32 %d, i32 1, i32 1}' % threads,
        '',
    ])


SWIZZLE = '  %1 = call i32 @xbox.LaneSwizzle.i32(i32 7, i32 %0, i32 30)'
EQUAL_MASK = '  %2 = call i32 @xbox.V_CNDMASK_B32(i32 8, { i32, i32 } { i32 -858993460, i32 -858993460 }, { i32, i32 } { i32 0, i32 1 })'
HIGH_HALF_MASK = '  %3 = call i32 @xbox.V_CNDMASK_B32(i32 9, { i32, i32 } { i32 0, i32 -1 }, { i32, i32 } { i32 0, i32 1 })'
READ_LANE = '  %4 = call i32 @xbox.ReadLane.i32(i32 10, i32 %1, i32 31)'
ACTIVE_SUM = '  %5 = call i32 @dx.op.waveActiveOp.i32(i32 119, i32 %1, i8 0, i8 1)'
LANE_COUNT = '  %6 = call i32 @dx.op.waveGetLaneCount(i32 112)'
WAVE_MAX = '  %7 = call i32 @xbox.WaveMax_U32(i32 11, i32 %1)'


class XboxDxilNeedsWave64Tests(unittest.TestCase):
    def needs(self, threads, *body):
        return lower_module.needs_wave64(wave_module(threads, list(body)))

    def test_lane_local_intrinsics_do_not_need_wave64(self):
        self.assertFalse(self.needs(512, SWIZZLE, EQUAL_MASK))

    def test_mask_with_different_halves_needs_wave64(self):
        self.assertTrue(self.needs(64, SWIZZLE, HIGH_HALF_MASK))

    def test_read_lane_needs_wave64(self):
        self.assertTrue(self.needs(64, SWIZZLE, READ_LANE))

    def test_standard_wave_operation_needs_wave64(self):
        self.assertTrue(self.needs(64, ACTIVE_SUM))

    def test_wave_max_needs_wave64(self):
        self.assertTrue(self.needs(64, SWIZZLE, WAVE_MAX))

    def test_group_of_32_does_not_need_wave64(self):
        self.assertFalse(self.needs(32, SWIZZLE, HIGH_HALF_MASK, READ_LANE, ACTIVE_SUM))

    def test_lane_count_needs_wave64_in_any_group(self):
        self.assertTrue(self.needs(32, LANE_COUNT))

    def test_no_wave_operation_does_not_need_wave64(self):
        self.assertFalse(self.needs(256, '  %7 = add i32 1, 2'))


if __name__ == "__main__":
    unittest.main()
