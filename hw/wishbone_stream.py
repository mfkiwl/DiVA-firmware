# This file is Copyright (c) 2020 Gregory Davill <greg.davill@gmail.com>
# License: BSD

import unittest

from migen import *

from litex.soc.interconnect import wishbone
from litex.soc.interconnect.stream import SyncFIFO,EndpointDescription, Endpoint, AsyncFIFO
from litex.soc.interconnect import stream_sim

from litex.soc.interconnect.csr import *

import random

def data_stream_description(dw):
    payload_layout = [("data", dw)]
    return EndpointDescription(payload_layout)

class dummySource(Module):
    def __init__(self):
        self.source = source = Endpoint(data_stream_description(32))
        counter = Signal(32)

        self.clr = Signal()

        frame = Signal(32)
        v_ctr = Signal(32)
        h_ctr = Signal(32)
        r = Signal(8)
        g = Signal(8)
        b = Signal(8)


        self.comb += [
            source.valid.eq(1),
            source.data.eq(Cat(r,g,b,Signal(8))),   
        ]

        self.sync += [
            If(source.ready,
                h_ctr.eq(h_ctr + 1),
                If(h_ctr >= 800-1,
                    h_ctr.eq(0),
                    v_ctr.eq(v_ctr + 1),
                    If(v_ctr >= 600-1,
                        v_ctr.eq(0),
                        frame.eq(frame + 1)
                    )
                )
            ),

            If(self.clr, 
                v_ctr.eq(0),
                h_ctr.eq(0)
            )
        ]

        speed = 1

        frame_tri = (Mux(frame[8], ~frame[:8], frame[:8]))
        frame_tri2 = (Mux(frame[9], ~frame[1:9], frame[1:9]))

        X = Mux(v_ctr[6], h_ctr + frame[speed:], h_ctr - frame[speed:])
        Y = v_ctr
        self.sync += [
            r.eq(frame_tri[1:]),
            g.eq(v_ctr * Mux(X & Y, 255, 0)),
            b.eq(~(frame_tri2 + (X ^ Y)) * 255)
        ]

class dummySink(Module):
    def __init__(self):
        self.sink = sink = Endpoint(data_stream_description(32))
        
        self.comb += [
            sink.ready.eq(1)
        ]

class StreamWriter(Module, AutoCSR):
    def __init__(self, external_sync=False):
        self.bus  = bus = wishbone.Interface()
        self.source = source = Endpoint(data_stream_description(32))

        tx_cnt = Signal(32)
        last_address = Signal()
        busy = Signal()
        done = Signal()
        evt_done = Signal()
        active = Signal()
        burst_end = Signal()
        burst_cnt = Signal(32)
        
        self.start_address = CSRStorage(32)
        self.transfer_size = CSRStorage(32)
        self.burst_size = CSRStorage(32, reset=256)

        self.done = CSRStatus()

        self.enable = CSR()
        self.reset = CSR()


        self.start = Signal()

        enabled = Signal()
        overflow = Signal()
        underflow = Signal()
        self.comb += [
            overflow.eq(source.ready & ~source.valid),
            underflow.eq(~source.ready & source.valid),

            self.done.status.eq(done)
        ]

        self.comb += [
            bus.sel.eq(0xF),
            bus.we.eq(0),
            bus.cyc.eq(active),
            bus.stb.eq(active),
            bus.adr.eq(self.start_address.storage[:-2] + tx_cnt),

            source.data.eq(bus.dat_r),
            source.valid.eq(bus.ack & active),

            If(~active,
                bus.cti.eq(0b000) # CLASSIC_CYCLE
            ).Elif(burst_end,
                bus.cti.eq(0b111), # END-OF-BURST
            ).Else(
                bus.cti.eq(0b010), # LINEAR_BURST
            )
        ]

        self.comb += [
            burst_end.eq(last_address | (burst_cnt == self.burst_size.storage - 1)),
            last_address.eq(tx_cnt == self.transfer_size.storage - 1),
        ]

        self.sync += [
            If(bus.ack & active,
                If(last_address,
                    tx_cnt.eq(0)
                ).Else(
                    tx_cnt.eq(tx_cnt + 1)
                )
            ),
            # Burst Counter
            If(~active,
                burst_cnt.eq(0)
            ).Else(
                If(bus.ack & active, 
                    burst_cnt.eq(burst_cnt + 1)
                )
            ),
            If(self.enable.re,
                enabled.eq(self.enable.r[0])
            ),
            
            If(self.reset.re,
                done.eq(0),
            ),
            If(evt_done,
                done.eq(1),
            )

        ]

        # Main FSM
        self.submodules.fsm = fsm = FSM(reset_state="IDLE")
        fsm.act("IDLE",
            If(busy & source.ready,
                NextState("ACTIVE"),
            ),
            If((self.start & enabled & external_sync) | (~external_sync & self.enable.re),
                NextValue(busy,1),
            )
        )
        fsm.act("ACTIVE",
            If(~source.ready,
                NextState("IDLE")
            ),
            If(burst_end & bus.ack & active,
                NextState("IDLE"),
                If(last_address,
                    evt_done.eq(1),
                    NextValue(busy,0),
                )
            ),
            If(self.reset.re,
                NextValue(busy, 0),
                NextState("IDLE")
            )
        )

        self.comb += active.eq(fsm.ongoing("ACTIVE") & source.ready)

class StreamReader(Module, AutoCSR):
    def __init__(self, external_sync=False):
        self.bus  = bus = wishbone.Interface()
        self.sink = sink = Endpoint(data_stream_description(32))


        tx_cnt = Signal(32)
        last_address = Signal()
        busy = Signal()
        done = Signal()
        evt_done = Signal()
        active = Signal()
        burst_end = Signal()
        burst_cnt = Signal(32)
        
        self.start_address = CSRStorage(32)
        self.transfer_size = CSRStorage(32)
        self.burst_size = CSRStorage(32, reset=256)

        self.done = CSRStatus()

        self.enable = CSR()
        self.reset = CSR()

        self.start = Signal()

        enabled = Signal()
        overflow = Signal()
        underflow = Signal()
        self.comb += [
            overflow.eq(sink.ready & ~sink.valid),
            underflow.eq(~sink.ready & sink.valid),

            self.done.status.eq(done)
        ]

        self.dbg = [
            tx_cnt,
            last_address,
            busy,
            active,
            burst_cnt,
            burst_end,
            self.start,
            sink.valid,
            sink.ready,    
            sink.data,
            overflow,
            underflow,
        ]

        self.comb += [
            bus.sel.eq(0xF),
            bus.we.eq(active),
            bus.cyc.eq(active),
            bus.stb.eq(active),
            bus.adr.eq(self.start_address.storage[:-2] + tx_cnt),
            bus.dat_w.eq(sink.data),
            sink.ready.eq(bus.ack & active),

            If(~active,
                bus.cti.eq(0b000) # CLASSIC_CYCLE
            ).Elif(burst_end,
                bus.cti.eq(0b111), # END-OF-BURST
            ).Else(
                bus.cti.eq(0b010), # LINEAR_BURST
            )
        ]

        self.comb += [
            #If(self._burst_size.storage == 1,
            #    burst_end.eq(1),
            #).Else(
                burst_end.eq(last_address | (burst_cnt == self.burst_size.storage - 1)),
                last_address.eq(tx_cnt == self.transfer_size.storage - 1),
            #)
        ]

        self.sync += [

            If(bus.ack & active,
                If(last_address,
                    tx_cnt.eq(0)
                ).Else(
                    tx_cnt.eq(tx_cnt + 1)
                )
            ),
            # Burst Counter
            If(~active,
                burst_cnt.eq(0)
            ).Else(
                If(bus.ack & active,
                    burst_cnt.eq(burst_cnt + 1)
                )
            ),
            If(self.enable.re,
                enabled.eq(self.enable.r[0])
            ),

            If(self.reset.re,
                done.eq(0),
            ),
            If(evt_done,
                done.eq(1),
            )
        ]

        # Main FSM
        self.submodules.fsm = fsm = FSM(reset_state="IDLE")
        fsm.act("IDLE",
            If(busy & sink.valid,
                NextState("ACTIVE"),
            ),
            If((self.start & enabled & external_sync) | (~external_sync & self.enable.re),
                NextValue(busy,1),
            )
        )
        fsm.act("ACTIVE",
            If(~sink.valid,
                NextState("IDLE")
            ),
            If(burst_end & bus.ack & active,
                NextState("IDLE"),
                If(last_address,
                    NextValue(busy,0),
                    evt_done.eq(1),
                )
            ),
            If(self.reset.re,
                NextValue(busy, 0),
                NextState("IDLE")
            )
        )

        self.comb += active.eq(fsm.ongoing("ACTIVE") & sink.valid)


# -=-=-=-= tests -=-=-=-=

def write_stream(stream, dat):
    yield stream.data.eq(dat)
    yield stream.valid.eq(1)
    yield
    yield stream.data.eq(0)
    yield stream.valid.eq(0)

class TestWriter(unittest.TestCase):

    def test_dma_write(self):
        def write(dut):
            dut = dut.reader
            yield from dut.start_address.write(0x0)
            yield from dut.transfer_size.write(4)
            yield from dut.burst_size.write(2)
            yield from dut.enable.write(1)
            yield
            for _ in range(64):
                yield
            yield

        def logger(dut):
            yield dut.reader.sink.valid.eq(1)
            for j in range(2):
                while (yield dut.reader.bus.cyc == 0):
                    yield
                for _ in range(4):
                    yield
                for i in range(2):
                    yield dut.reader.bus.ack.eq(1)
                    yield dut.reader.sink.valid.eq(~((j == 1) & (i == 0)))
                    yield
                    #yield
                yield dut.reader.bus.ack.eq(0)
                yield
                    

        class test(Module):
            def __init__(self):    
                self.submodules.reader = StreamReader()
                self.submodules.dummySource = ds = dummySource()

                #self.comb += ds.source.connect(self.reader.sink)
                
        dut = test()
        

        run_simulation(dut, [write(dut), logger(dut)], vcd_name='write.vcd')
    


if __name__ == '__main__':
    unittest.main()