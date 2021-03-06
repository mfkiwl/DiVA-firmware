#!/usr/bin/env python3

# This file is Copyright (c) 2020 Gregory Davill <greg.davill@gmail.com>
# License: BSD

import sys
import argparse
import optparse
import subprocess

from migen import *
import bosonHDMI_r0d3
import bosonHDMI_r0d2


from math import log2, ceil

import os
import shutil
from hdmi import HDMI
from terminal import Terminal



#from litex.soc.cores.uart import WishboneStreamingBridge
from litex.soc.cores.uart import Stream2Wishbone

from litescope import LiteScopeAnalyzer

#from file_helper import package_file

#from migen import *
from migen.genlib.resetsync import AsyncResetSynchronizer

from litex.build.generic_platform import *
from litex.boards.platforms import versa_ecp5

from litex.soc.cores.clock import *
from ecp5_dynamic_pll import ECP5PLL, period_ns
from litex.soc.integration.soc_core import *
from litex.soc.integration.soc import SoCRegion
from litex.soc.integration.builder import *


from litex.soc.interconnect import wishbone

from litex.soc.cores.gpio import GPIOOut, GPIOIn
from rgb_led import RGB
from reboot import Reboot

from streamable_hyperram import StreamableHyperRAM

from wishbone_stream import StreamReader, StreamWriter, dummySink, dummySource



from litex.soc.interconnect.stream import Endpoint, EndpointDescription, SyncFIFO, AsyncFIFO, Monitor


from migen.genlib.cdc import MultiReg, PulseSynchronizer

from boson import Boson
from YCrCb import YCrCbConvert

from sw_i2c import I2C

from litex.soc.interconnect import stream

from migen.genlib.misc import timeline


from sim import Platform

from edge_detect import EdgeDetect
from prbs_stream import PRBSSink, PRBSSource

from simulated_video import SimulatedVideo
from video_debug import VideoDebug
from video_stream import VideoStream
from framer import Framer
from scaler import ScalerWidth
from scaler import ScalerHeight

#from hyperRAM.hyperbus_fast import HyperRAM
#from dma.dma import StreamWriter, StreamReader, dummySink, dummySource

#from litex.soc.interconnect.stream import BufferizeEndpoints, DIR_SOURCE, PulseSynchronizer

from litex.soc.interconnect.csr import *

class _CRG(Module, AutoCSR):
    def __init__(self, platform, sys_clk_freq):
        self.clock_domains.cd_sys = ClockDomain()
        self.clock_domains.cd_video = ClockDomain()
        self.clock_domains.cd_video_shift = ClockDomain()
        


        self.clock_domains.cd_hr = ClockDomain()
        self.clock_domains.cd_hr_90 = ClockDomain()
        self.clock_domains.cd_hr2x = ClockDomain()
        self.clock_domains.cd_hr2x_90 = ClockDomain()
        self.clock_domains.cd_hr2x_ = ClockDomain()
        self.clock_domains.cd_hr2x_90_ = ClockDomain()

        self.clock_domains.cd_init = ClockDomain()
        

        # # #

        # clk / rst
        clk48 = platform.request("clk48")

        self.submodules.pll = pll = ECP5PLL()
        pll.register_clkin(clk48, 48e6)
        pll.create_clkout(self.cd_hr2x, sys_clk_freq*2, margin=0)
        pll.create_clkout(self.cd_hr2x_90, sys_clk_freq*2, phase=1, margin=0) # SW tunes this phase during init
        
        self.specials += [
        ]

        self.comb += self.cd_sys.clk.eq(self.cd_hr.clk)
        self.comb += self.cd_init.clk.eq(clk48)

        pixel_clk = 40e6
        #pixel_clk = sys_clk_freq

        self.clock_domains.cd_usb_12 = ClockDomain()
        self.clock_domains.cd_usb_48 = ClockDomain()

        self.submodules.video_pll = video_pll = ECP5PLL()
        video_pll.register_clkin(clk48, 48e6)
        video_pll.create_clkout(self.cd_video,    pixel_clk,  margin=0)
        video_pll.create_clkout(self.cd_video_shift,  pixel_clk*5, margin=0)
        video_pll.create_clkout(self.cd_usb_12,    12e6,  margin=0)


        self.comb += self.cd_usb_48.clk.eq(clk48)


        platform.add_period_constraint(self.cd_usb_12.clk, period_ns(12e6))
        platform.add_period_constraint(self.cd_usb_48.clk, period_ns(48e6))
        platform.add_period_constraint(self.cd_sys.clk, period_ns(sys_clk_freq))
        platform.add_period_constraint(clk48, period_ns(48e6))
        platform.add_period_constraint(self.cd_video.clk, period_ns(pixel_clk))
        platform.add_period_constraint(self.cd_video_shift.clk, period_ns(pixel_clk * 5))

        self._slip_hr2x = CSRStorage()
        self._slip_hr2x90 = CSRStorage()

        # ECLK stuff 
        self.specials += [
            Instance("CLKDIVF",
                p_DIV     = "2.0",
                i_ALIGNWD = self._slip_hr2x.storage,
                i_CLKI    = self.cd_hr2x.clk,
                i_RST     = ~pll.locked,
                o_CDIVX   = self.cd_hr.clk),

            Instance("CLKDIVF",
                p_DIV     = "2.0",
                i_ALIGNWD = self._slip_hr2x90.storage,
                i_CLKI    = self.cd_hr2x_90.clk,
                i_RST     = ~pll.locked,
                o_CDIVX   = self.cd_hr_90.clk),
            #AsyncResetSynchronizer(self.cd_hr, reset),
            #AsyncResetSynchronizer(self.cd_sys, reset)
        ]


        self._phase_sel = CSRStorage(2)
        self._phase_dir = CSRStorage()
        self._phase_step = CSRStorage()
        self._phase_load = CSRStorage()

        self.comb += [
            self.pll.phase_sel.eq(self._phase_sel.storage),
            self.pll.phase_dir.eq(self._phase_dir.storage),
            self.pll.phase_step.eq(self._phase_step.storage),
            self.pll.phase_load.eq(self._phase_load.storage),
        ]

        # OSC-G for simulated video streams
        oscg_clk = Signal()
        self.clock_domains.cd_oscg_38M = ClockDomain()
        self.specials += Instance("OSCG",
            p_DIV=8,
            o_OSC=oscg_clk
        )
        self.comb += self.cd_oscg_38M.clk.eq(oscg_clk)


       

class DiVA_SoC(SoCCore):
    csr_map = {
        "rgb"        :  10, 
        "crg"        :  11, 
        "hyperram"   :  12,
        "terminal"   :  13,
        "analyzer"   :  14,
        "hdmi_i2c"   :  15,
        "i2c0"        :  16,
        "btn"        :  18,
        "reader"     :  19,
        "writer"     :  20,
        "reader1"    :  21,
        "writer1"    :  22,
        "prbs_sink"  :  23,
        "prbs_source":  24,
        "reboot"     :  25,
        "video_debug":  26,
        "framer"     :  27,
        "scaler"     :  28,
        "boson"      :  29,
    }
    csr_map.update(SoCCore.csr_map)

    mem_map = {
        "hyperram"  : 0x10000000,
        "terminal"  : 0x30000000,
    }
    mem_map.update(SoCCore.mem_map)

    interrupt_map = {

    }
    interrupt_map.update(SoCCore.interrupt_map)

    def __init__(self, sim=False):

        if sim:
            self.platform = platform = Platform()
        else:
            self.platform = platform = bosonHDMI_r0d3.Platform()
        
        sys_clk_freq = 82.5e6
        SoCCore.__init__(self, platform, clk_freq=sys_clk_freq,
                          cpu_type='serv', with_uart=True, uart_name='stream',
                          csr_data_width=32,
                          ident="HyperRAM Test SoC", ident_version=True, wishbone_timeout_cycles=512,
                          integrated_rom_size=16*1024)

        self.platform.toolchain.build_template[1] += f" --log {platform.name}.log"

        # Fake a UART stream, to enable easy firmware reuse.
        self.comb += self.uart.source.ready.eq(1)
    
        # crg
        if sim:
            clk = platform.request("clk")
            rst = platform.request("rst")
            self.clock_domains.cd_sys = ClockDomain()
            self.comb += self.cd_sys.clk.eq(clk)

            self.comb += self.cd_sys.rst.eq(rst)            

        else:
            self.submodules.crg = _CRG(platform, sys_clk_freq)
     
        ## Create VGA terminal
        self.submodules.terminal = terminal = ClockDomainsRenamer({'vga':'video'})(Terminal())
        self.register_mem("terminal", self.mem_map["terminal"], terminal.bus, size=0x100000)

        # User inputs
        btn = platform.request("button")
        self.submodules.btn = GPIOIn(Cat(btn.a, btn.b))

        if not sim:
            self.submodules.rgb = RGB(platform.request("rgb_led"))
        
        # HyperRAM
        hyperram_pads = None if sim else platform.request("hyperRAM")
        self.submodules.writer = writer = StreamWriter(external_sync=True)
        self.submodules.reader = reader = StreamReader(external_sync=True)

        self.submodules.writer1 = writer1 = StreamWriter()
        self.submodules.reader1 = reader1 = StreamReader()

        self.submodules.hyperram = hyperram = StreamableHyperRAM(hyperram_pads, devices=[reader, writer, reader1, writer1], sim=sim)
        self.register_mem("hyperram", self.mem_map['hyperram'], hyperram.bus, size=0x800000)

        # Dummy video stream
        #self.submodules.simulated_video = simulated_video = ClockDomainsRenamer({"pixel":"oscg_38M"})(SimulatedVideo())
        # Boson video stream
        self.submodules.boson = boson = Boson(platform, platform.request("boson"), sys_clk_freq)
        self.submodules.YCrCb = ycrcb = ClockDomainsRenamer({"sys":"boson_rx"})(YCrCbConvert())
        
        fifo = AsyncFIFO([("data", 32)], depth=512)
        fifo = ResetInserter(["read","write"])(fifo)
        fifo = ClockDomainsRenamer({"read":"sys","write":"boson_rx"})(fifo)
        
        self.submodules.video_debug = video_debug = ClockDomainsRenamer({"pixel":"boson_rx"})(VideoDebug(int(self.clk_freq)))
        #self.submodules.video_stream = video_stream = ClockDomainsRenamer({"pixel":"boson_rx"})(VideoStream())
        
        scaler_enable = Signal()

        self.submodules.framer = framer = Framer()

        self.submodules.scaler = scaler = ClockDomainsRenamer({"sys":"video"})((ScalerWidth()))
        self.submodules.fifo2 = fifo2 = ClockDomainsRenamer({"sys":"video"})(ResetInserter()(SyncFIFO([("data", 32)], depth=16)))
        self.submodules.scaler0 = scaler0 = ClockDomainsRenamer({"sys":"video"})(ScalerHeight(800))


        self.comb += [
            video_debug.vsync.eq(boson.vsync),
            video_debug.hsync.eq(boson.hsync),

            boson.source.connect(ycrcb.sink),

            #fifo.reset_write.eq(boson.vsync),



            #video_stream.data_valid.eq(boson.data_valid),
            #video_stream.red.eq(boson.red),
            #video_stream.green.eq(boson.green),
            #video_stream.blue.eq(boson.blue),

            boson.next_mode.eq(btn.b)
        ]

        # connect something to these streams
        #ds = dummySource()
        #fifo = ClockDomainsRenamer({"read":"sys","write":"sys"})(SyncFIFO([("data", 32)], depth=512))
        #fifo = ResetInserter()(SyncFIFO([("data", 32)], depth=4))
        #self.submodules += ds
        self.submodules += fifo
        self.comb += [
        
            ycrcb.source.connect(fifo.sink),
        #    ds.source.connect(fifo.sink),
            fifo.source.connect(reader.sink),
        ]



        ## HDMI output 
        if not sim:
            hdmi_pins = platform.request('hdmi')
            self.submodules.hdmi = hdmi =  HDMI(platform, hdmi_pins)
            self.submodules.hdmi_i2c = I2C(platform.request("hdmi_i2c"))


            # I2C
            self.submodules.i2c0 = I2C(platform.request("i2c"))

        self.submodules.reboot = Reboot(platform.request("rst_n"), ext_rst=~btn.a)




        fifo0 = ClockDomainsRenamer({"read":"video","write":"sys"})(AsyncFIFO([("data", 32)], depth=512))
        self.submodules += fifo0
        self.comb += [
            writer.source.connect(fifo0.sink),


            If(scaler_enable,
                fifo0.source.connect(scaler.sink),
                scaler.source.connect(fifo2.sink),
                fifo2.source.connect(scaler0.sink),
                scaler0.source.connect(framer.sink)
            ).Else(

                fifo0.source.connect(framer.sink),
            )
        ]


        # prbs tester
        self.submodules.prbs_sink = PRBSSink()
        self.submodules.prbs_source = PRBSSource()

        self.comb += [
            self.prbs_source.source.connect(self.reader1.sink),
            self.writer1.source.connect(self.prbs_sink.sink),
        ]

        # enable
        self.submodules.vsync_rise = vsync_rise = EdgeDetect(mode="rise", input_cd="video", output_cd="sys")
        self.comb += vsync_rise.i.eq(terminal.vsync)

        self.submodules.vsync_rise_term = vsync_rise_term = EdgeDetect(mode="rise", input_cd="video", output_cd="video")
        self.comb += vsync_rise_term.i.eq(terminal.vsync)


        self.submodules.vsync_boson = vsync_boson = EdgeDetect(mode="fall", input_cd="boson_rx", output_cd="sys")
        self.comb += vsync_boson.i.eq(boson.vsync)


        self.comb += [
            writer.start.eq(vsync_rise.o),
            scaler.reset.eq(vsync_rise_term.o),
            fifo2.reset.eq(vsync_rise_term.o),
            scaler0.reset.eq(vsync_rise_term.o),
        ]
        #self.comb += reader.start.eq(vsync_boson.o)
        
        #self.comb += ds.clr.eq(vsync_rise.o)
        #self.comb += fifo.reset.eq(vsync_rise.o)



        # delay vsync pulse from boson by 500 clocks, then use it to reset the fifo
        fifo_rst = Signal()
        self.sync += [
             timeline(vsync_boson.o, [
                (501,  [fifo_rst.eq(1)]),   # Reset FIFO
                (550,  [fifo_rst.eq(0), scaler_enable.eq(scaler.enable.storage)]),  # Clear Reset
                (621,  [reader.start.eq(1)]),
                (622,  [reader.start.eq(0)])
            ])
        ]
        self.specials += MultiReg(fifo_rst, fifo.reset_write, odomain="boson_rx")
        self.comb += fifo.reset_read.eq(fifo_rst)
       
        #self.comb += writer.start.eq(vsync_rise.o)

        ## Connect VGA pins
        if not sim:
            self.comb += [
                fifo.reset_read.eq(vsync_boson.o),

                # attach framer to video generator
                framer.vsync.eq(terminal.vsync),
                framer.hsync.eq(terminal.hsync),
                

                hdmi.vsync.eq(terminal.vsync),
                hdmi.hsync.eq(terminal.hsync),
                hdmi.blank.eq(terminal.blank),
                If(framer.data_valid,
                    hdmi.r.eq(framer.red),
                    hdmi.g.eq(framer.green),
                    hdmi.b.eq(framer.blue),
                ).Else(
                    hdmi.r.eq(terminal.red),
                    hdmi.g.eq(terminal.green),
                    hdmi.b.eq(terminal.blue),  
                )
            ]

        

        analyser = False
        if analyser and not sim:
            # USB with Clock-Domain-Crossing support
            import os
            import sys
            os.system("git clone https://github.com/gregdavill/valentyusb -b hw_cdc_eptri")
            sys.path.append("valentyusb")

            import valentyusb.usbcore.io as usbio
            import valentyusb.usbcore.cpu.cdc_eptri as cdc_eptri
            usb_pads = self.platform.request("usb")
            usb_iobuf = usbio.IoBuf(usb_pads.d_p, usb_pads.d_n, usb_pads.pullup)
            self.submodules.uart_usb = cdc_eptri.CDCUsb(usb_iobuf)

            # Select ECP5 as USB target
            if hasattr(usb_pads, "sw_sel"):
                self.comb += usb_pads.sw_sel.eq(1)
            
            # Enable USB
            if hasattr(usb_pads, "sw_oe"):
                self.comb += usb_pads.sw_oe.eq(0)
            

            self.submodules.bridge = Stream2Wishbone(self.uart_usb, sys_clk_freq)
            self.add_wb_master(self.bridge.wishbone)

            self.submodules.analyzer = LiteScopeAnalyzer(hyperram.dbg, 64)

        # Add git version into firmware 
        def get_git_revision():
            try:
                r = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"],
                        stderr=subprocess.DEVNULL)[:-1].decode("utf-8")
            except:
                r = "--------"
            return r
        self.add_constant("DIVA_GIT_SHA1", get_git_revision())

    def do_exit(self, vns):
        if hasattr(self, "analyzer"):
            self.analyzer.export_csv(vns, "test/analyzer.csv")


    def PackageFirmware(self, builder):  
        self.finalize()

        os.makedirs(builder.output_dir, exist_ok=True)

        src_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "sw", "DiVA-fw"))
        builder.add_software_package("DiVA-fw", src_dir)

        builder._prepare_rom_software()
        builder._generate_includes()
        builder._generate_rom_software(compile_bios=False)

        firmware_file = os.path.join(builder.output_dir, "software", "DiVA-fw","DiVA-fw.bin")
        firmware_data = get_mem_data(firmware_file, self.cpu.endianness)
        self.initialize_rom(firmware_data)

        # lock out compiling firmware during build steps
        builder.compile_software = False


def CreateFirmwareInit(init, output_file):
    content = ""
    for d in init:
        content += "{:08x}\n".format(d)
    with open(output_file, "w") as o:
        o.write(content)    
     
def main():
    parser = argparse.ArgumentParser(
        description="Build DiVA Gateware")
    parser.add_argument(
        "--update-firmware", default=False, action='store_true',
        help="compile firmware and update existing gateware"
    )
    parser.add_argument(
        "--sim", default=False, action='store_true',
        help="simulate"
    )
    args = parser.parse_args()

    soc = DiVA_SoC()
    builder = Builder(soc, output_dir="build", csr_csv="build/csr.csv")

    # Build firmware
    soc.PackageFirmware(builder)
        

    if args.sim:
        ...
        builder.build(run=False)


        # Verilator build
        build_script = os.path.join(builder.output_dir, "gateware", "verilator_build.sh")
        with open(build_script, 'w') as f:
            print("""verilator --trace --compiler clang -j 4 --top-module sim -Os -I/home/greg/Projects/litex/pythondata-cpu-serv/pythondata_cpu_serv/verilog/rtl --cc /home/greg/Projects/litex/pythondata-cpu-serv/pythondata_cpu_serv/verilog/rtl/serv_shift.v --cc /home/greg/Projects/litex/pythondata-cpu-serv/pythondata_cpu_serv/verilog/rtl/serv_rf_top.v --cc /home/greg/Projects/litex/pythondata-cpu-serv/pythondata_cpu_serv/verilog/rtl/serv_params.vh --cc /home/greg/Projects/litex/pythondata-cpu-serv/pythondata_cpu_serv/verilog/rtl/serv_rf_ram.v --cc /home/greg/Projects/litex/pythondata-cpu-serv/pythondata_cpu_serv/verilog/rtl/serv_bufreg.v --cc /home/greg/Projects/litex/pythondata-cpu-serv/pythondata_cpu_serv/verilog/rtl/serv_alu.v --cc /home/greg/Projects/litex/pythondata-cpu-serv/pythondata_cpu_serv/verilog/rtl/serv_ctrl.v --cc /home/greg/Projects/litex/pythondata-cpu-serv/pythondata_cpu_serv/verilog/rtl/serv_decode.v --cc /home/greg/Projects/litex/pythondata-cpu-serv/pythondata_cpu_serv/verilog/rtl/serv_rf_ram_if.v --cc /home/greg/Projects/litex/pythondata-cpu-serv/pythondata_cpu_serv/verilog/rtl/serv_top.v --cc /home/greg/Projects/litex/pythondata-cpu-serv/pythondata_cpu_serv/verilog/rtl/serv_state.v --cc /home/greg/Projects/litex/pythondata-cpu-serv/pythondata_cpu_serv/verilog/rtl/serv_csr.v --cc /home/greg/Projects/litex/pythondata-cpu-serv/pythondata_cpu_serv/verilog/rtl/serv_rf_if.v --cc /home/greg/Projects/litex/pythondata-cpu-serv/pythondata_cpu_serv/verilog/rtl/serv_mem_if.v --cc /home/greg/Projects/DiVA-firmware/hw/build/gateware/sim.v
cd obj_dir 
make -f Vsim.mk
cd ..
clang++ -I obj_dir -flto -I/usr/local/share/verilator/include -I/usr/include/SDL2  /usr/local/share/verilator/include/verilated.cpp /usr/local/share/verilator/include/verilated_vcd_c.cpp ../../verilator_sim_driver.cc -O3 -lSDL2 obj_dir/Vsim__ALL.a -fno-exceptions -std=c++14 -o sim""",
                file=f)

        cwd = os.getcwd()
        os.chdir(os.path.join(builder.output_dir, "gateware"))

        if subprocess.call(['bash', build_script]) != 0:
            raise OSError("Subprocess failed")


    else:
        # Check if we have the correct files
        firmware_file = os.path.join(builder.output_dir, "software", "DiVA-fw", "DiVA-fw.bin")
        firmware_data = get_mem_data(firmware_file, soc.cpu.endianness)
        firmware_init = os.path.join(builder.output_dir, "software", "DiVA-fw", "DiVA-fw.init")
        CreateFirmwareInit(firmware_data, firmware_init)
        
        rand_rom = os.path.join(builder.output_dir, "gateware", "rand.data")
        
        input_config = os.path.join(builder.output_dir, "gateware", f"{soc.platform.name}.config")
        output_config = os.path.join(builder.output_dir, "gateware", f"{soc.platform.name}_patched.config")
        
        # If we don't have a random file, create one, and recompile gateware
        if (os.path.exists(rand_rom) == False) or (args.update_firmware == False):
            os.makedirs(os.path.join(builder.output_dir,'gateware'), exist_ok=True)
            os.makedirs(os.path.join(builder.output_dir,'software'), exist_ok=True)

            os.system(f"ecpbram  --generate {rand_rom} --seed {0} --width {32} --depth {soc.integrated_rom_size}")

            # patch random file into BRAM
            data = []
            with open(rand_rom, 'r') as inp:
                for d in inp.readlines():
                    data += [int(d, 16)]
            soc.initialize_rom(data)

            # Build gateware
            vns = builder.build(nowidelut=True)
            soc.do_exit(vns)    


        # Insert Firmware into Gateware
        os.system(f"ecpbram  --input {input_config} --output {output_config} --from {rand_rom} --to {firmware_init}")

        # create a compressed bitstream
        output_bit = os.path.join(builder.output_dir, "gateware", "DiVA.bit")
        os.system(f"ecppack --freq 38.8 --compress --input {output_config} --bit {output_bit}")

        # Add DFU suffix
        os.system(f"dfu-suffix -p 16d0 -d 0fad -a {output_bit}")

        print(
        f"""DiVA build complete!  Output files:
        
        Bitstream file. (Compressed, Higher CLK)  Load this into FLASH.
            {builder.output_dir}/gateware/DiVA.bit
        
        Source Verilog file.  Useful for debugging issues.
            {builder.output_dir}/gateware/top.v
        """)



if __name__ == "__main__":
    main()

