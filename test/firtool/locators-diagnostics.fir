; RUN: firtool %s -parse-only -mlir-print-debuginfo --mlir-print-local-scope | FileCheck %s --check-prefixes=COMMON,PREFER
; RUN: firtool %s -parse-only -mlir-print-debuginfo --mlir-print-local-scope -fuse-info-locators | FileCheck %s --check-prefixes=COMMON,FUSE
; RUN: firtool %s -parse-only -mlir-print-debuginfo --mlir-print-local-scope -prefer-info-locators | FileCheck %s --check-prefixes=COMMON,PREFER
; RUN: firtool %s -parse-only -mlir-print-debuginfo --mlir-print-local-scope -ignore-info-locators | FileCheck %s --check-prefixes=COMMON,IGNORE

; RUN: not firtool %s -fuse-info-locators 2>&1 | FileCheck %s --check-prefixes=DIAG
; DIAG: path/to/Foo.scala:273:69: error:
; DIAG: locators-diagnostics.fir:21:5: note: additional location here
; DIAG: locators-diagnostics.fir:24:5: error:
; DIAG-NOT: note
FIRRTL version 4.0.0
circuit Error:
  public module Error:
    output xa: UInt<1>
    output ya: UInt<1>

    ; COMMON: %x = firrtl.wire
    ; FUSE-SAME: loc(fused["path/to/Foo.scala":273:69, "{{.+}}.fir":21:5])
    ; PREFER-SAME: loc("path/to/Foo.scala":273:69)
    ; IGNORE-SAME: loc("{{.+}}.fir":21:5)
    wire x : { boop: { foo: { a: UInt<1>, b: UInt<2>}, bar: { a: UInt<1> }}} @[path/to/Foo.scala 273:69]
    ; COMMON: %y = firrtl.wire
    ; COMMON-SAME: loc("{{.+}}.fir":24:5)
    wire y : { boop: { foo: { a: UInt<1>, b: UInt<2>}, bar: { a: UInt<1> }}} ; (no locator)

    ; invalidate all but .boop.foo.a
    invalidate x.boop.bar
    invalidate x.boop.foo.b
    invalidate y.boop.bar
    invalidate y.boop.foo.b

    ; Connects
    connect xa, x.boop.foo.a
    connect ya, y.boop.foo.a
