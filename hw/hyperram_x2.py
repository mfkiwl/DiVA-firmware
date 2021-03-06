# This file is Copyright (c) 2019 Antti Lukats <antti.lukats@gmail.com>
# This file is Copyright (c) 2019 Florent Kermarrec <florent@enjoy-digital.fr>
# This file is Copyright (c) 2020 Gregory Davill <greg.davill@gmail.com>
# License: BSD

from migen import Module, Record, Signal, If, Cat, TSTriple, Instance, ClockSignal, ResetSignal
from migen import  FSM, NextValue, NextState

from litex.soc.interconnect.wishbone import Interface
from migen.genlib.cdc import MultiReg

from ecp5hyperbusphy_x2 import HyperBusPHY

def delayf_pins():
    return Record([("loadn", 1),("move", 1),("direction", 1)])

# HyperRAMX2 -----------------------------------------------------------------------------------------

class HyperRAMX2(Module):
    """HyperRAMX2

    Provides a HyperRAM core that works at 2:1 system clock speeds
    - PHY is device dependent for DDRx2 primitives
      - ECP5 (done)
    - 90 deg phase shifted clock required from PLL
    - Burst R/W supported if bus is ready
    - Latency indepedent reads (uses RWDS pattern)

    This core favors performance over portability
    This core has only been tested on ECP5 platforms so far.

    TODO:
     - Handle R/W of config registers
     - Configure Latency
     - Handle variable latency writes
     - Add Litex automated tests
    """
    def __init__(self, pads):
        self.pads = pads
        self.bus  = bus = Interface(adr_width=22)


        self.dly_io = delayf_pins()
        self.dly_clk = delayf_pins()

        # # #

        clk           = Signal()
        cs            = Signal()
        ca            = Signal(48)
        sr_in         = Signal(64)
        sr_out        = Signal(64)
        sr_rwds_in    = Signal(8)
        sr_rwds_out   = Signal(8)

        timeout_counter = Signal(6)

        self.submodules.phy = phy = HyperBusPHY(pads)

        self.comb += [
            phy.dly_io.eq(self.dly_io),
            phy.dly_clk.eq(self.dly_clk),
        ]
    
        # Drive rst_n, from internal signals ---------------------------------------------
        if hasattr(pads, "rst_n"):
            self.comb += pads.rst_n.eq(1)
            
        self.comb += [
            phy.cs.eq(~cs),
            phy.clk_enable.eq(clk)
        ]
        
        # Data In/Out Shift Registers -------------------------------------------------
        self.sync += [
            sr_out.eq(Cat(Signal(32), sr_out[:32])),
            sr_in.eq(Cat(phy.dq.i, sr_in[:32])),
            sr_rwds_in.eq(Cat(phy.rwds.i, sr_rwds_in[:4])),
            sr_rwds_out.eq(Cat(phy.rwds.i, sr_rwds_out[:4])),
        ]

        self.comb += [
            bus.dat_r.eq(Cat(phy.dq.i[-16:], sr_in[:16])), # To Wishbone
            phy.dq.o.eq(sr_out[-32:]),  # To HyperRAM
            phy.rwds.o.eq(sr_rwds_out[-4:]) # To HyperRAM
        ]

        # Command generation -----------------------------------------------------------------------
        self.comb += [
            ca[47].eq(~self.bus.we),          # R/W#
            ca[45].eq(1),                     # Burst Type (Linear)
            ca[16:35].eq(self.bus.adr[2:21]), # Row & Upper Column Address
            ca[1:3].eq(self.bus.adr[0:2]),    # Lower Column Address
            ca[0].eq(0),                      # Lower Column Address
        ]

        # FSM Sequencer --------------------------------------------------------------------------------
        self.submodules.fsm = fsm = FSM(reset_state="IDLE")
        fsm.act("IDLE", If(bus.cyc & bus.stb, NextValue(cs, 1), NextState("CA-SEND")))
        fsm.act("CA-SEND", NextValue(clk, 1), NextValue(phy.dq.oe, 1), NextValue(sr_out,Cat(Signal(16),ca)), NextState("CA-WAIT"))
        fsm.act("CA-WAIT", NextValue(timeout_counter, 0),NextState("LATENCY"))
        fsm.act("LATENCY", NextValue(phy.dq.oe, 0), NextState("LATENCY-WAIT"))

        fsm.delayed_enter("LATENCY-WAIT", "READ-WRITE-SETUP", 3)
        
        fsm.act("READ-WRITE-SETUP", NextValue(phy.dq.oe, self.bus.we), NextValue(phy.rwds.oe,self.bus.we), NextState("READ-WRITE"))
        fsm.act("READ-WRITE", NextState("READ-ACK"),
                If(self.bus.we,
                    NextValue(phy.dq.oe,1),                 # Write Cycle
                    NextValue(sr_out[:32],0),
                    NextValue(sr_out[32:],self.bus.dat_w),
                    NextValue(sr_rwds_out[:4],0),
                    NextValue(sr_rwds_out[4:],~bus.sel[0:4]),
                    bus.ack.eq(1), # Get next byte
                    NextState("CLK-OFF"),
                    If(bus.cti == 0b010,
                        NextState("READ-WRITE"))),
                If(~self.bus.cyc, # We may have ended a cycle.
                    NextValue(clk, 0), NextState("CLEANUP")
                ))
        
        
        fsm.act("READ-ACK", 
            NextValue(timeout_counter, timeout_counter + 1),
            If(phy.rwds.i[3], 
                NextValue(timeout_counter, 0),
                bus.ack.eq(1),
                If(bus.cti != 0b010,
                    NextValue(clk, 0), NextState("CLEANUP"))),
            If(~self.bus.cyc | (timeout_counter > 20),
                NextState("CLK-OFF"),
                bus.err.eq(1), bus.ack.eq(1)
            ))
        
        fsm.act("CLK-OFF", NextValue(clk, 0), NextState("CLEANUP"))
        fsm.act("CLEANUP", NextValue(cs, 0), NextValue(phy.rwds.oe, 0), NextValue(phy.dq.oe, 0), NextState("HOLD-WAIT"))
        fsm.act("HOLD-WAIT", NextValue(sr_out, 0), NextValue(sr_rwds_out, 0), NextState("WAIT"))
        fsm.delayed_enter("WAIT", "IDLE", 4) 
        
        # Signals that can be an ILA for debugging
        self.dbg = [
            bus,
            sr_in,
            cs,
            clk,
        ]
