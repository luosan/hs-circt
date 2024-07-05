#  Part of the LLVM Project, under the Apache License v2.0 with LLVM Exceptions.
#  See https://llvm.org/LICENSE.txt for license information.
#  SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception

from ..common import Clock, Input, Output
from ..constructs import ControlReg, Mux, NamedWire, Wire
from ..module import Module, generator
from ..signals import BundleSignal
from ..system import System
from ..types import Array, Bits
from .. import esi

from .common import (ESI_Manifest_ROM, MagicNumberHi, MagicNumberLo,
                     VersionNumber)

import glob
import pathlib
import shutil

from typing import Dict, Tuple

__dir__ = pathlib.Path(__file__).parent


class AxiMMIO(esi.ServiceImplementation):
  """MMIO service implementation with an AXI-lite protocol. This assumes a 20
  bit address bus for 1MB of addressable MMIO space. Which should be fine for
  now, though nothing should assume this limit. It also only supports 32-bit
  aligned accesses and just throws away the lower two bits of address.

  Only allows for one outstanding request at a time. If a client doesn't return
  a response, the MMIO service will hang. TODO: add some kind of timeout.

  Implementation-defined MMIO layout:
    - 0x0: 0 constant
    - 0x4: 0 constant
    - 0x8: Magic number low (0xE5100E51)
    - 0xC: Magic number high (random constant: 0x207D98E5)
    - 0x10: ESI version number (0)
    - 0x14: Location of the manifest ROM (absolute address)

    - 0x100: Start of MMIO space for requests. Mapping is contained in the
             manifest so can be dynamically queried.

    - addr(Manifest ROM) + 0: Size of compressed manifest
    - addr(Manifest ROM) + 4: Start of compressed manifest

  This layout _should_ be pretty standard, but different BSPs may have various
  different restrictions.
  """

  # Moved from bsp/common.py. TODO: adapt this to use ChannelMMIO.

  clk = Clock()
  rst = Input(Bits(1))

  # MMIO read: address channel.
  arvalid = Input(Bits(1))
  arready = Output(Bits(1))
  araddr = Input(Bits(20))

  # MMIO read: data response channel.
  rvalid = Output(Bits(1))
  rready = Input(Bits(1))
  rdata = Output(Bits(32))
  rresp = Output(Bits(2))

  # MMIO write: address channel.
  awvalid = Input(Bits(1))
  awready = Output(Bits(1))
  awaddr = Input(Bits(20))

  # MMIO write: data channel.
  wvalid = Input(Bits(1))
  wready = Output(Bits(1))
  wdata = Input(Bits(32))

  # MMIO write: write response channel.
  bvalid = Output(Bits(1))
  bready = Input(Bits(1))
  bresp = Output(Bits(2))

  # Start at this address for assigning MMIO addresses to service requests.
  initial_offset: int = 0x100

  @generator
  def generate(self, bundles: esi._ServiceGeneratorBundles):
    read_table, write_table, manifest_loc = AxiMMIO.build_table(self, bundles)
    AxiMMIO.build_read(self, manifest_loc, read_table)
    AxiMMIO.build_write(self, write_table)
    return True

  def build_table(
      self,
      bundles) -> Tuple[Dict[int, BundleSignal], Dict[int, BundleSignal], int]:
    """Build a table of read and write addresses to BundleSignals."""
    offset = AxiMMIO.initial_offset
    read_table = {}
    write_table = {}
    for bundle in bundles.to_client_reqs:
      if bundle.direction == ChannelDirection.Input:
        read_table[offset] = bundle
        offset += 4
      elif bundle.direction == ChannelDirection.Output:
        write_table[offset] = bundle
        offset += 4

    manifest_loc = 1 << offset.bit_length()
    return read_table, write_table, manifest_loc

  def build_read(self, manifest_loc: int, bundles):
    """Builds the read side of the MMIO service."""

    # Currently just exposes the header and manifest. Not any of the possible
    # service requests.

    i32 = Bits(32)
    i2 = Bits(2)
    i1 = Bits(1)

    address_written = NamedWire(i1, "address_written")
    response_written = NamedWire(i1, "response_written")

    # Only allow one outstanding request at a time. Don't clear it until the
    # output has been transmitted. This way, we don't have to deal with
    # backpressure.
    req_outstanding = ControlReg(self.clk,
                                 self.rst, [address_written],
                                 [response_written],
                                 name="req_outstanding")
    self.arready = ~req_outstanding

    # Capture the address if a the bus transaction occured.
    address_written.assign(self.arvalid & ~req_outstanding)
    address = self.araddr.reg(self.clk, ce=address_written, name="address")
    address_valid = address_written.reg(name="address_valid")
    address_words = address[2:]  # Lop off the lower two bits.

    # Set up the output of the data response pipeline. `data_pipeline*` are to
    # be connected below.
    data_pipeline_valid = NamedWire(i1, "data_pipeline_valid")
    data_pipeline = NamedWire(i32, "data_pipeline")
    data_pipeline_rresp = NamedWire(i2, "data_pipeline_rresp")
    data_out_valid = ControlReg(self.clk,
                                self.rst, [data_pipeline_valid],
                                [response_written],
                                name="data_out_valid")
    self.rvalid = data_out_valid
    self.rdata = data_pipeline.reg(self.clk,
                                   self.rst,
                                   ce=data_pipeline_valid,
                                   name="data_pipeline_reg")
    self.rresp = data_pipeline_rresp.reg(self.clk,
                                         self.rst,
                                         ce=data_pipeline_valid,
                                         name="data_pipeline_rresp_reg")
    # Clear the `req_outstanding` flag when the response has been transmitted.
    response_written.assign(data_out_valid & self.rready)

    # Handle reads from the header (< 0x100).
    header_upper = address_words[AxiMMIO.initial_offset.bit_length() - 2:]
    # Is the address in the header?
    header_sel = (header_upper == header_upper.type(0))
    header_sel.name = "header_sel"
    # Layout the header as an array.
    header = Array(Bits(32), 6)(
        [0, 0, MagicNumberLo, MagicNumberHi, VersionNumber, manifest_loc])
    header.name = "header"
    header_response_valid = address_valid  # Zero latency read.
    header_out = header[address[2:5]]
    header_out.name = "header_out"
    header_rresp = i2(0)

    # Handle reads from the manifest.
    rom_address = NamedWire(
        (address_words.as_uint() - (manifest_loc >> 2)).as_bits(30),
        "rom_address")
    mani_rom = ESI_Manifest_ROM(clk=self.clk, address=rom_address)
    mani_valid = address_valid.reg(
        self.clk,
        self.rst,
        rst_value=i1(0),
        cycles=2,  # Two cycle read to match the ROM latency.
        name="mani_valid_reg")
    mani_rresp = i2(0)
    # mani_sel = (address.as_uint() >= manifest_loc)

    # Mux the output depending on whether or not the address is in the header.
    sel = NamedWire(~header_sel, "sel")
    data_mux_inputs = [header_out, mani_rom.data]
    data_pipeline.assign(Mux(sel, *data_mux_inputs))
    data_valid_mux_inputs = [header_response_valid, mani_valid]
    data_pipeline_valid.assign(Mux(sel, *data_valid_mux_inputs))
    rresp_mux_inputs = [header_rresp, mani_rresp]
    data_pipeline_rresp.assign(Mux(sel, *rresp_mux_inputs))

  def build_write(self, bundles):
    # TODO: this.

    # So that we don't wedge the AXI-lite for writes, just ack all of them.
    write_happened = Wire(Bits(1))
    latched_aw = ControlReg(self.clk, self.rst, [self.awvalid],
                            [write_happened])
    latched_w = ControlReg(self.clk, self.rst, [self.wvalid], [write_happened])
    write_happened.assign(latched_aw & latched_w)

    self.awready = 1
    self.wready = 1
    self.bvalid = write_happened
    self.bresp = 0


def XrtBSP(user_module):
  """Use the Xilinx RunTime (XRT) shell to implement ESI services and build an
  image or emulation package.
  How to use this BSP:
  - Wrap your top PyCDE module in `XrtBSP`.
  - Run your script. This BSP will write a 'build package' to the output dir.
  This package contains a Makefile.xrt.mk which (given a proper Vitis dev
  environment) will compile a hw image or hw_emu image. It is a free-standing
  build package -- you do not need PyCDE installed on the same machine as you
  want to do the image build.
  - To build the `hw` image, run 'make -f Makefile.xrt TARGET=hw'. If you want
  an image which runs on an Azure NP-series instance, run the 'azure' target
  (requires an Azure subscription set up with as per
  https://learn.microsoft.com/en-us/azure/virtual-machines/field-programmable-gate-arrays-attestation).
  This target requires a few environment variables to be set (which the Makefile
  will tell you about).
  - To build a hw emulation image, run with TARGET=hw_emu.
  - Validated ONLY on Vitis 2023.1. Known to NOT work with Vitis <2022.1.
  """

  class XrtTop(Module):
    ap_clk = Clock()
    ap_resetn = Input(Bits(1))

    # AXI4-Lite slave interface
    s_axi_control_AWVALID = Input(Bits(1))
    s_axi_control_AWREADY = Output(Bits(1))
    s_axi_control_AWADDR = Input(Bits(20))
    s_axi_control_WVALID = Input(Bits(1))
    s_axi_control_WREADY = Output(Bits(1))
    s_axi_control_WDATA = Input(Bits(32))
    s_axi_control_WSTRB = Input(Bits(32 // 8))
    s_axi_control_ARVALID = Input(Bits(1))
    s_axi_control_ARREADY = Output(Bits(1))
    s_axi_control_ARADDR = Input(Bits(20))
    s_axi_control_RVALID = Output(Bits(1))
    s_axi_control_RREADY = Input(Bits(1))
    s_axi_control_RDATA = Output(Bits(32))
    s_axi_control_RRESP = Output(Bits(2))
    s_axi_control_BVALID = Output(Bits(1))
    s_axi_control_BREADY = Input(Bits(1))
    s_axi_control_BRESP = Output(Bits(2))

    @generator
    def construct(ports):
      System.current().platform = "fpga"

      rst = ~ports.ap_resetn

      xrt = AxiMMIO(
          esi.MMIO,
          appid=esi.AppID("xrt_mmio"),
          clk=ports.ap_clk,
          rst=rst,
          awvalid=ports.s_axi_control_AWVALID,
          awaddr=ports.s_axi_control_AWADDR,
          wvalid=ports.s_axi_control_WVALID,
          wdata=ports.s_axi_control_WDATA,
          wstrb=ports.s_axi_control_WSTRB,
          arvalid=ports.s_axi_control_ARVALID,
          araddr=ports.s_axi_control_ARADDR,
          rready=ports.s_axi_control_RREADY,
          bready=ports.s_axi_control_BREADY,
      )

      # AXI-Lite control
      ports.s_axi_control_AWREADY = xrt.awready
      ports.s_axi_control_WREADY = xrt.wready
      ports.s_axi_control_ARREADY = xrt.arready
      ports.s_axi_control_RVALID = xrt.rvalid
      ports.s_axi_control_RDATA = xrt.rdata
      ports.s_axi_control_RRESP = xrt.rresp
      ports.s_axi_control_BVALID = xrt.bvalid
      ports.s_axi_control_BRESP = xrt.bresp

      user_module(clk=ports.ap_clk, rst=rst)

      # Copy additional sources
      sys: System = System.current()
      sys.add_packaging_step(esi.package)
      sys.add_packaging_step(XrtTop.package)

    @staticmethod
    def package(sys: System):
      """Assemble a 'build' package which includes all the necessary build
      collateral (about which we are aware), build/debug scripts, and the
      generated runtime."""

      sv_sources = glob.glob(str(__dir__ / '*.sv'))
      tcl_sources = glob.glob(str(__dir__ / '*.tcl'))
      for source in sv_sources + tcl_sources:
        shutil.copy(source, sys.hw_output_dir)

      shutil.copy(__dir__ / "Makefile.xrt.mk",
                  sys.output_directory / "Makefile.xrt.mk")
      shutil.copy(__dir__ / "xrt_package.tcl",
                  sys.output_directory / "xrt_package.mk")
      shutil.copy(__dir__ / "xrt.ini", sys.output_directory / "xrt.ini")
      shutil.copy(__dir__ / "xsim.tcl", sys.output_directory / "xsim.tcl")

  return XrtTop
