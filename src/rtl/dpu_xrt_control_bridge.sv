// AP_CTRL_HS/XRT kernel control to the PG338 DPUCZDX8G core-0 register map.
// One outstanding AXI4-Lite transaction per side. All addresses are byte offsets.
module dpu_xrt_control_bridge (
    input  logic        clk_i,
    input  logic        resetn_i,
    input  logic        dpu_irq_i,
    output logic        xrt_irq_o,

    input  logic [23:0] s_axi_awaddr_i,
    input  logic        s_axi_awvalid_i,
    output logic        s_axi_awready_o,
    input  logic [31:0] s_axi_wdata_i,
    input  logic [3:0]  s_axi_wstrb_i,
    input  logic        s_axi_wvalid_i,
    output logic        s_axi_wready_o,
    output logic [1:0]  s_axi_bresp_o,
    output logic        s_axi_bvalid_o,
    input  logic        s_axi_bready_i,
    input  logic [23:0] s_axi_araddr_i,
    input  logic        s_axi_arvalid_i,
    output logic        s_axi_arready_o,
    output logic [31:0] s_axi_rdata_o,
    output logic [1:0]  s_axi_rresp_o,
    output logic        s_axi_rvalid_o,
    input  logic        s_axi_rready_i,

    output logic [31:0] m_axi_awaddr_o,
    output logic [15:0] m_axi_awid_o,
    output logic [7:0]  m_axi_awlen_o,
    output logic [2:0]  m_axi_awsize_o,
    output logic [1:0]  m_axi_awburst_o,
    output logic        m_axi_awlock_o,
    output logic [3:0]  m_axi_awcache_o,
    output logic [2:0]  m_axi_awprot_o,
    output logic [3:0]  m_axi_awqos_o,
    output logic [3:0]  m_axi_awregion_o,
    output logic        m_axi_awvalid_o,
    input  logic        m_axi_awready_i,
    output logic [31:0] m_axi_wdata_o,
    output logic [3:0]  m_axi_wstrb_o,
    output logic        m_axi_wlast_o,
    output logic        m_axi_wvalid_o,
    input  logic        m_axi_wready_i,
    input  logic [1:0]  m_axi_bresp_i,
    input  logic [15:0] m_axi_bid_i,
    input  logic        m_axi_bvalid_i,
    output logic        m_axi_bready_o,
    output logic [31:0] m_axi_araddr_o,
    output logic [15:0] m_axi_arid_o,
    output logic [7:0]  m_axi_arlen_o,
    output logic [2:0]  m_axi_arsize_o,
    output logic [1:0]  m_axi_arburst_o,
    output logic        m_axi_arlock_o,
    output logic [3:0]  m_axi_arcache_o,
    output logic [2:0]  m_axi_arprot_o,
    output logic [3:0]  m_axi_arqos_o,
    output logic [3:0]  m_axi_arregion_o,
    output logic        m_axi_arvalid_o,
    input  logic        m_axi_arready_i,
    input  logic [31:0] m_axi_rdata_i,
    input  logic [1:0]  m_axi_rresp_i,
    input  logic [15:0] m_axi_rid_i,
    input  logic        m_axi_rlast_i,
    input  logic        m_axi_rvalid_i,
    output logic        m_axi_rready_o
);
    typedef enum logic [2:0] {
        IDLE, WRITE_SEND, WRITE_RESP, RUNNING, READ_SEND, READ_RESP,
        CLEAR_WAIT, CLEAR_FAULT
    } state_t;
    state_t state_q;
    localparam logic [4:0] NATIVE_START_INDEX = 5'd21;
    localparam logic [4:0] NATIVE_BASE_FIRST_INDEX = 5'd5;
    localparam logic [31:0] NATIVE_HPBUS_DEFAULT = 32'h07070f0f;

    logic [31:0] args_q [0:23];
    logic [23:0] args_valid_q;
    // Diagnostic visibility survives start, unlike next-command readiness.
    logic [23:0] args_written_q;
    logic [23:0] awaddr_q;
    logic [31:0] wdata_q;
    logic [3:0]  wstrb_q;
    logic aw_held_q, w_held_q;
    logic write_busy_q;
    logic m_aw_sent_q, m_w_sent_q;
    logic [4:0] write_index_q;
    logic [23:0] read_addr_q;
    logic gie_q, ier_q, isr_q, done_q, fault_q, native_fault_q;
    logic irq_meta_q, irq_sync_q, irq_prev_q;
    logic early_done_q;
    logic [31:0] identity_q [0:1];
    logic [1:0] identity_valid_q;
    logic [31:0] native_aw_count_q, native_w_count_q, native_b_count_q;
    logic [31:0] command_count_q, irq_count_q, command_cycles_q;
    logic [31:0] last_native_aw_q, last_native_w_q, last_native_b_q;
    logic [31:0] last_native_r_q, last_native_rdata_q;
    logic command_irq_seen_q, start_sent_q, start_ok_q;
    logic completion_pending_q, clear_active_q, clear_wait_irq_q;
    logic [1:0] clear_index_q;
    logic [8:0] clear_wait_cycles_q;
    // 100 MHz control clock: at least 1 us between START=0 acknowledgement
    // and ICR deassertion, as required by the native linux-xlnx driver.
    localparam logic [8:0] CLEAR_HOLD_CYCLES = 9'd100;
    localparam logic [8:0] CLEAR_IRQ_WAIT_CYCLES = 9'd256;
    logic [7:0] reject_reason_q;
    logic [31:0] diagnostic_data;
    logic [1:0] diagnostic_resp;
    wire diagnostic_selected = s_axi_araddr_i[23:8] == 16'h0010;
    wire diagnostic_handshake = diagnostic_selected &&
                                s_axi_arvalid_i && s_axi_arready_o;

    function automatic logic [31:0] saturating_increment(input logic [31:0] value);
        return value == 32'hffffffff ? value : value + 32'd1;
    endfunction
    wire [4:0] write_arg_index = 5'(awaddr_q[7:2] - 6'd16);
    wire [4:0] read_arg_index = 5'(s_axi_araddr_i[7:2] - 6'd16);

    // PG338 can address 40 bits, but this XRT image advertises only HP0 low
    // DDR [0, 2 GiB). Do not start the DPU with an address outside that bank.
    wire args_complete = (&args_valid_q[23:8]) &&
                         args_valid_q[5] && args_valid_q[4];
    wire addresses_valid = (args_q[4][11:0] == 12'd0) &&
                           !args_q[4][31] && (args_q[5] == 32'd0);
    logic bases_valid;
    logic [7:0] unused_base_mask;
    always_comb begin
        bases_valid = 1'b1;
        unused_base_mask = '0;
        for (int unsigned n = 0; n < 8; n++) begin
            // VART marks unused high-numbered base slots with all ones.
            // They are not 40-bit DPU addresses and must not reach PG338.
            if (n >= 4 && args_q[8 + 2*n] == 32'hffffffff &&
                args_q[9 + 2*n] == 32'hffffffff)
                unused_base_mask[n] = 1'b1;
            else if (args_q[8 + 2*n][31] || args_q[9 + 2*n] != 32'd0)
                bases_valid = 1'b0;
        end
    end

    assign s_axi_awready_o = (state_q == IDLE || state_q == RUNNING || state_q == CLEAR_FAULT) && !aw_held_q &&
                             !s_axi_bvalid_o && !s_axi_rvalid_o;
    assign s_axi_wready_o = (state_q == IDLE || state_q == RUNNING || state_q == CLEAR_FAULT) && !w_held_q &&
                            !s_axi_bvalid_o && !s_axi_rvalid_o;
    // A failed native command must not prevent XRT's timeout diagnostics.
    // RUNNING reads are entirely local, including cached real identity words.
    // Local diagnostics can pass a stuck WRITE_SEND/WRITE_RESP and a held B.
    // Never pass an outstanding native read: AXI-Lite has no response IDs.
    assign s_axi_arready_o = !s_axi_rvalid_o &&
        (diagnostic_selected
         ? (state_q != READ_SEND && state_q != READ_RESP)
         : ((state_q == IDLE || state_q == RUNNING || state_q == CLEAR_FAULT) &&
            !aw_held_q && !w_held_q && !s_axi_bvalid_o));
    assign xrt_irq_o = gie_q && ier_q && isr_q;

    assign m_axi_awid_o = 16'd0;
    assign m_axi_awlen_o = 8'd0;
    assign m_axi_awsize_o = 3'd2;
    assign m_axi_awburst_o = 2'b01;
    assign m_axi_awlock_o = 1'b0;
    assign m_axi_awcache_o = 4'd0;
    assign m_axi_awprot_o = 3'd0;
    assign m_axi_awqos_o = 4'd0;
    assign m_axi_awregion_o = 4'd0;
    assign m_axi_arid_o = 16'd0;
    assign m_axi_arlen_o = 8'd0;
    assign m_axi_arsize_o = 3'd2;
    assign m_axi_arburst_o = 2'b01;
    assign m_axi_arlock_o = 1'b0;
    assign m_axi_arcache_o = 4'd0;
    assign m_axi_arprot_o = 3'd0;
    assign m_axi_arqos_o = 4'd0;
    assign m_axi_arregion_o = 4'd0;
    assign m_axi_awvalid_o = (state_q == WRITE_SEND) && !m_aw_sent_q;
    assign m_axi_wvalid_o = (state_q == WRITE_SEND) && !m_w_sent_q;
    assign m_axi_wstrb_o = 4'hf;
    assign m_axi_wlast_o = 1'b1;
    assign m_axi_bready_o = (state_q == WRITE_RESP);
    assign m_axi_araddr_o = {8'd0, read_addr_q};
    assign m_axi_arvalid_o = (state_q == READ_SEND);
    assign m_axi_rready_o = (state_q == READ_RESP);

    always_comb begin
        diagnostic_data = 32'd0;
        diagnostic_resp = 2'b00;
        case (s_axi_araddr_i)
            24'h001000: diagnostic_data = 32'h50444247; // PDBG ABI, not DPU identity
            24'h001004: diagnostic_data = 32'd4;
            24'h001008: diagnostic_data = {16'd0, args_complete, start_ok_q,
                start_sent_q, m_w_sent_q, m_aw_sent_q, early_done_q, xrt_irq_o,
                isr_q, done_q, command_irq_seen_q, irq_sync_q, native_fault_q,
                fault_q, state_q};
            24'h00100c: diagnostic_data = {27'd0, write_index_q};
            24'h001010: diagnostic_data = last_native_aw_q;
            24'h001014: diagnostic_data = last_native_w_q;
            24'h001018: diagnostic_data = last_native_b_q;
            24'h00101c: diagnostic_data = last_native_r_q;
            24'h001020: diagnostic_data = native_aw_count_q;
            24'h001024: diagnostic_data = native_w_count_q;
            24'h001028: diagnostic_data = native_b_count_q;
            24'h00102c: diagnostic_data = command_count_q;
            24'h001030: diagnostic_data = irq_count_q;
            24'h001034: diagnostic_data = {18'd0, s_axi_rvalid_o, s_axi_bvalid_o,
                w_held_q, aw_held_q, m_axi_rready_o, m_axi_rvalid_i,
                m_axi_arready_i, m_axi_arvalid_o, m_axi_bready_o, m_axi_bvalid_i,
                m_axi_wready_i, m_axi_wvalid_o, m_axi_awready_i, m_axi_awvalid_o};
            24'h001038: diagnostic_data = {8'd0, args_written_q};
            24'h00103c: diagnostic_data = {8'd0, args_valid_q};
            24'h001040: diagnostic_data = command_cycles_q;
            24'h001044: diagnostic_data = {24'd0, reject_reason_q};
            24'h001048: diagnostic_data = last_native_rdata_q;
            24'h00104c: diagnostic_data = {18'd0, clear_wait_cycles_q,
                clear_index_q, clear_wait_irq_q, clear_active_q, completion_pending_q};
            default: diagnostic_resp = 2'b10;
        endcase
    end

    always_comb begin
        m_axi_awaddr_o = 32'h00000004;
        m_axi_wdata_o = 32'd0;
        if (write_index_q == 5'd1) begin
            // Official linux-xlnx native initialization for core0. XRT cmd
            // shadow alone does not program this register. Default mode only.
            m_axi_awaddr_o = 32'h00000200;
            m_axi_wdata_o = NATIVE_HPBUS_DEFAULT;
        end else if (write_index_q == 5'd2) begin
            m_axi_awaddr_o = 32'h00000220;
            m_axi_wdata_o = 32'd0;
        end else if (write_index_q == 5'd3) begin
            // linux-xlnx xlnx_dpu_regs_init: release after HPBUS/START=0.
            // PG338 prose disagrees on polarity; this driver sequence is the
            // next isolated board experiment, not proof of native execution.
            m_axi_awaddr_o = 32'h00000004;
            m_axi_wdata_o = 32'h0000000f;
        end else if (write_index_q == 5'd4) begin
            m_axi_awaddr_o = 32'h0000020c;
            m_axi_wdata_o = {4'd0, args_q[5][7:0], args_q[4][31:12]};
        end else if (write_index_q >= NATIVE_BASE_FIRST_INDEX && write_index_q < NATIVE_START_INDEX) begin
            m_axi_awaddr_o = 32'h00000224 +
                            {25'd0, write_index_q - NATIVE_BASE_FIRST_INDEX, 2'b00};
            m_axi_wdata_o = unused_base_mask[3'((write_index_q - NATIVE_BASE_FIRST_INDEX) >> 1)]
                            ? 32'd0 : args_q[8 + write_index_q - NATIVE_BASE_FIRST_INDEX];
        end else if (write_index_q == NATIVE_START_INDEX) begin
            m_axi_awaddr_o = 32'h00000220;
            m_axi_wdata_o = 32'd1;
        end
        if (clear_active_q) begin
            m_axi_awaddr_o = clear_index_q == 2'd1 ? 32'h00000220 : 32'h0000060c;
            m_axi_wdata_o = clear_index_q == 2'd0 ? 32'd1 : 32'd0;
        end
    end

    always_ff @(posedge clk_i) begin
        if (!resetn_i) begin
            state_q <= IDLE;
            args_valid_q <= '0;
            args_written_q <= '0;
            aw_held_q <= 1'b0;
            w_held_q <= 1'b0;
            write_busy_q <= 1'b0;
            m_aw_sent_q <= 1'b0;
            m_w_sent_q <= 1'b0;
            write_index_q <= '0;
            gie_q <= 1'b0;
            ier_q <= 1'b0;
            isr_q <= 1'b0;
            done_q <= 1'b0;
            fault_q <= 1'b0;
            native_fault_q <= 1'b0;
            irq_meta_q <= 1'b0;
            irq_sync_q <= 1'b0;
            irq_prev_q <= 1'b0;
            early_done_q <= 1'b0;
            identity_valid_q <= '0;
            native_aw_count_q <= '0;
            native_w_count_q <= '0;
            native_b_count_q <= '0;
            command_count_q <= '0;
            irq_count_q <= '0;
            command_cycles_q <= '0;
            last_native_aw_q <= '0;
            last_native_w_q <= '0;
            last_native_b_q <= '0;
            last_native_r_q <= '0;
            last_native_rdata_q <= '0;
            command_irq_seen_q <= 1'b0;
            start_sent_q <= 1'b0;
            start_ok_q <= 1'b0;
            reject_reason_q <= '0;
            completion_pending_q <= 1'b0;
            clear_active_q <= 1'b0;
            clear_wait_irq_q <= 1'b0;
            clear_index_q <= '0;
            clear_wait_cycles_q <= '0;
            s_axi_bvalid_o <= 1'b0;
            s_axi_bresp_o <= 2'b00;
            s_axi_rvalid_o <= 1'b0;
            s_axi_rdata_o <= '0;
            s_axi_rresp_o <= 2'b00;
        end else begin
            irq_meta_q <= dpu_irq_i;
            irq_sync_q <= irq_meta_q;
            irq_prev_q <= irq_sync_q;
            if (s_axi_bvalid_o && s_axi_bready_i) s_axi_bvalid_o <= 1'b0;
            if (s_axi_rvalid_o && s_axi_rready_i) s_axi_rvalid_o <= 1'b0;

            // Observation only: never acknowledge, cancel or restart the DPU.
            if (diagnostic_handshake) begin
                s_axi_rdata_o <= diagnostic_data;
                s_axi_rresp_o <= diagnostic_resp;
                s_axi_rvalid_o <= 1'b1;
            end
            if (state_q == WRITE_SEND || state_q == WRITE_RESP || state_q == RUNNING || state_q == CLEAR_WAIT)
                command_cycles_q <= saturating_increment(command_cycles_q);
            if (irq_sync_q && !irq_prev_q) begin
                irq_count_q <= saturating_increment(irq_count_q);
                if (state_q == WRITE_SEND || state_q == WRITE_RESP || state_q == RUNNING)
                    command_irq_seen_q <= 1'b1;
            end
            if (m_axi_awvalid_o && m_axi_awready_i) begin
                native_aw_count_q <= saturating_increment(native_aw_count_q);
                last_native_aw_q <= m_axi_awaddr_o;
            end
            if (m_axi_wvalid_o && m_axi_wready_i) begin
                native_w_count_q <= saturating_increment(native_w_count_q);
                last_native_w_q <= m_axi_wdata_o;
            end
            if (m_axi_bvalid_i && m_axi_bready_o) begin
                native_b_count_q <= saturating_increment(native_b_count_q);
                last_native_b_q <= {1'b1, 13'd0, m_axi_bid_i, m_axi_bresp_i};
            end
            if (m_axi_rvalid_i && m_axi_rready_o) begin
                last_native_r_q <= {1'b1, 12'd0, m_axi_rlast_i,
                                    m_axi_rid_i, m_axi_rresp_i};
                last_native_rdata_q <= m_axi_rdata_i;
            end

            if (s_axi_awready_o && s_axi_awvalid_i) begin
                awaddr_q <= s_axi_awaddr_i;
                aw_held_q <= 1'b1;
                if (state_q == RUNNING) write_busy_q <= 1'b1;
            end
            if (s_axi_wready_o && s_axi_wvalid_i) begin
                wdata_q <= s_axi_wdata_i;
                wstrb_q <= s_axi_wstrb_i;
                w_held_q <= 1'b1;
                if (state_q == RUNNING) write_busy_q <= 1'b1;
            end

            case (state_q)
                IDLE, RUNNING, CLEAR_FAULT: begin
                    if (aw_held_q && w_held_q) begin
                        aw_held_q <= 1'b0;
                        w_held_q <= 1'b0;
                        write_busy_q <= 1'b0;
                        s_axi_bresp_o <= 2'b00;
                        if (state_q == RUNNING || write_busy_q ||
                            wstrb_q != 4'hf ||
                            awaddr_q[23:12] != 12'd0 || awaddr_q[1:0] != 2'b00) begin
                            s_axi_bresp_o <= 2'b10;
                            s_axi_bvalid_o <= 1'b1;
                            if (awaddr_q == 24'd0 && wdata_q[0])
                                reject_reason_q <= 8'b01000000;
                        end else if (awaddr_q == 24'h000000 && wdata_q[0]) begin
                            if (native_fault_q || completion_pending_q || wdata_q[7] || !args_complete || !addresses_valid ||
                                !bases_valid || irq_sync_q) begin
                                s_axi_bresp_o <= 2'b10;
                                s_axi_bvalid_o <= 1'b1;
                                fault_q <= 1'b1;
                                reject_reason_q <= {completion_pending_q, 1'b0, irq_sync_q, !bases_valid,
                                    !addresses_valid, !args_complete, wdata_q[7], native_fault_q};
                            end else begin
                                done_q <= 1'b0;
                                isr_q <= 1'b0;
                                fault_q <= 1'b0;
                                args_valid_q <= '0;
                                write_index_q <= 5'd0;
                                early_done_q <= 1'b0;
                                command_count_q <= saturating_increment(command_count_q);
                                command_cycles_q <= '0;
                                command_irq_seen_q <= 1'b0;
                                start_sent_q <= 1'b0;
                                start_ok_q <= 1'b0;
                                reject_reason_q <= '0;
                                state_q <= WRITE_SEND;
                            end
                        end else if (awaddr_q == 24'h000004) begin
                            gie_q <= wdata_q[0];
                            s_axi_bvalid_o <= 1'b1;
                        end else if (awaddr_q == 24'h000008) begin
                            ier_q <= wdata_q[0];
                            s_axi_bvalid_o <= 1'b1;
                        end else if (awaddr_q == 24'h00000c) begin
                            if (wdata_q[0]) isr_q <= !isr_q;
                            s_axi_bvalid_o <= 1'b1;
                        end else if (awaddr_q == 24'h000040) begin
                            // Only a successfully completed command permits
                            // native acknowledgement. No clearing a running
                            // DPU or issuing a reset as a timeout recovery.
                            args_q[0] <= wdata_q;
                            args_valid_q[0] <= 1'b1;
                            args_written_q[0] <= 1'b1;
                            if (wdata_q[0]) begin
                                if (completion_pending_q && !native_fault_q) begin
                                    clear_active_q <= 1'b1;
                                    clear_index_q <= 2'd0;
                                    clear_wait_cycles_q <= '0;
                                    clear_wait_irq_q <= 1'b0;
                                    m_aw_sent_q <= 1'b0;
                                    m_w_sent_q <= 1'b0;
                                    state_q <= WRITE_SEND;
                                end else if (native_fault_q) begin
                                    s_axi_bresp_o <= 2'b10;
                                    s_axi_bvalid_o <= 1'b1;
                                end else begin
                                    done_q <= 1'b0;
                                    isr_q <= 1'b0;
                                    s_axi_bvalid_o <= 1'b1;
                                end
                            end else begin
                                s_axi_bvalid_o <= 1'b1;
                            end
                        end else if (awaddr_q >= 24'h000040 && awaddr_q <= 24'h00009c) begin
                            args_q[write_arg_index] <= wdata_q;
                            args_valid_q[write_arg_index] <= 1'b1;
                            args_written_q[write_arg_index] <= 1'b1;
                            s_axi_bvalid_o <= 1'b1;
                        end else begin
                            s_axi_bresp_o <= 2'b10;
                            s_axi_bvalid_o <= 1'b1;
                        end
                    end else if (!diagnostic_selected && s_axi_arready_o && s_axi_arvalid_i) begin
                        s_axi_rresp_o <= 2'b00;
                        if (s_axi_araddr_i == 24'h000000) begin
                            s_axi_rdata_o <= {27'd0, fault_q,
                                              (state_q == IDLE),
                                              (state_q == IDLE), done_q, 1'b0};
                            s_axi_rvalid_o <= 1'b1;
                            done_q <= 1'b0;
                        end else if (s_axi_araddr_i == 24'h000004) begin
                            s_axi_rdata_o <= {31'd0, gie_q};
                            s_axi_rvalid_o <= 1'b1;
                        end else if (s_axi_araddr_i == 24'h000008) begin
                            s_axi_rdata_o <= {31'd0, ier_q};
                            s_axi_rvalid_o <= 1'b1;
                        end else if (s_axi_araddr_i == 24'h00000c) begin
                            s_axi_rdata_o <= {31'd0, isr_q};
                            s_axi_rvalid_o <= 1'b1;
                        end else if (s_axi_araddr_i >= 24'h000040 && s_axi_araddr_i <= 24'h00009c) begin
                            s_axi_rdata_o <= args_written_q[read_arg_index]
                                             ? args_q[read_arg_index] : 32'd0;
                            s_axi_rvalid_o <= 1'b1;
                        end else if (s_axi_araddr_i == 24'h0001f0 ||
                                     s_axi_araddr_i == 24'h0001f4) begin
                            if (state_q == IDLE) begin
                                read_addr_q <= s_axi_araddr_i;
                                state_q <= READ_SEND;
                            end else begin
                                // Never access the busy native core for a
                                // timeout snapshot. Cache only successful real
                                // reads; missing identity is an error, not zero.
                                s_axi_rdata_o <= identity_valid_q[s_axi_araddr_i[2]]
                                    ? identity_q[s_axi_araddr_i[2]] : 32'd0;
                                s_axi_rresp_o <= identity_valid_q[s_axi_araddr_i[2]]
                                    ? 2'b00 : 2'b10;
                                s_axi_rvalid_o <= 1'b1;
                            end
                        end else if (s_axi_araddr_i < 24'h000fa0 &&
                                     s_axi_araddr_i[1:0] == 2'b00) begin
                            // VART snapshots the complete 0x000..0xf9c XRT
                            // register window before its first command. Only
                            // identity words are read from native DPU; all
                            // undefined AP_CTRL_HS words acknowledge as zero.
                            s_axi_rdata_o <= 32'd0;
                            s_axi_rvalid_o <= 1'b1;
                        end else begin
                            s_axi_rdata_o <= 32'd0;
                            s_axi_rresp_o <= 2'b10;
                            s_axi_rvalid_o <= 1'b1;
                        end
                    end
                    if (state_q == RUNNING && irq_sync_q && !irq_prev_q) begin
                        done_q <= 1'b1;
                        isr_q <= 1'b1;
                        completion_pending_q <= 1'b1;
                        state_q <= IDLE;
                    end
                end
                WRITE_SEND: begin
                    if (m_axi_awvalid_o && m_axi_awready_i) m_aw_sent_q <= 1'b1;
                    if (m_axi_wvalid_o && m_axi_wready_i) m_w_sent_q <= 1'b1;
                    if ((m_aw_sent_q || m_axi_awready_i) &&
                        (m_w_sent_q || m_axi_wready_i)) begin
                        state_q <= WRITE_RESP;
                        if (!clear_active_q && write_index_q == NATIVE_START_INDEX) start_sent_q <= 1'b1;
                    end
                end
                WRITE_RESP: begin
                    // Native completion may precede the start write response.
                    // Hold it until that response succeeds; never report a
                    // command as done if the native write itself failed.
                    if (!clear_active_q && write_index_q == NATIVE_START_INDEX && irq_sync_q && !irq_prev_q)
                        early_done_q <= 1'b1;
                    if (m_axi_bvalid_i) begin
                        m_aw_sent_q <= 1'b0;
                        m_w_sent_q <= 1'b0;
                        if (m_axi_bresp_i != 2'b00 || m_axi_bid_i != 16'd0) begin
                            fault_q <= 1'b1;
                            // The DPU may have accepted a write despite a
                            // failed response. Do not issue another command
                            // until the surrounding system resets the bridge.
                            native_fault_q <= 1'b1;
                            s_axi_bresp_o <= 2'b10;
                            s_axi_bvalid_o <= 1'b1;
                            state_q <= clear_active_q ? CLEAR_FAULT : IDLE;
                        end else if (clear_active_q) begin
                            if (clear_index_q == 2'd0) begin
                                clear_index_q <= 2'd1;
                                state_q <= WRITE_SEND;
                            end else begin
                                clear_wait_cycles_q <= '0;
                                clear_wait_irq_q <= clear_index_q == 2'd2;
                                state_q <= CLEAR_WAIT;
                            end
                        end else if (write_index_q == NATIVE_START_INDEX) begin
                            start_ok_q <= 1'b1;
                            s_axi_bresp_o <= 2'b00;
                            s_axi_bvalid_o <= 1'b1;
                            if (early_done_q || (irq_sync_q && !irq_prev_q)) begin
                                done_q <= 1'b1;
                                isr_q <= 1'b1;
                                completion_pending_q <= 1'b1;
                                state_q <= IDLE;
                            end else state_q <= RUNNING;
                        end else begin
                            write_index_q <= write_index_q + 5'd1;
                            state_q <= WRITE_SEND;
                        end
                    end
                end
                CLEAR_WAIT: begin
                    if (!clear_wait_irq_q) begin
                        if (clear_wait_cycles_q == CLEAR_HOLD_CYCLES - 9'd1) begin
                            clear_index_q <= 2'd2;
                            state_q <= WRITE_SEND;
                        end else clear_wait_cycles_q <= clear_wait_cycles_q + 9'd1;
                    end else if (!irq_sync_q) begin
                        clear_active_q <= 1'b0;
                        clear_wait_irq_q <= 1'b0;
                        completion_pending_q <= 1'b0;
                        done_q <= 1'b0;
                        isr_q <= 1'b0;
                        s_axi_bresp_o <= 2'b00;
                        s_axi_bvalid_o <= 1'b1;
                        state_q <= IDLE;
                    end else if (clear_wait_cycles_q == CLEAR_IRQ_WAIT_CYCLES - 9'd1) begin
                        // All bus writes have completed, but the device has
                        // not withdrawn IRQ. Do not pretend it can restart.
                        native_fault_q <= 1'b1;
                        fault_q <= 1'b1;
                        s_axi_bresp_o <= 2'b10;
                        s_axi_bvalid_o <= 1'b1;
                        state_q <= CLEAR_FAULT;
                    end else clear_wait_cycles_q <= clear_wait_cycles_q + 9'd1;
                end
                READ_SEND: if (m_axi_arready_i) state_q <= READ_RESP;
                READ_RESP: if (m_axi_rvalid_i) begin
                    s_axi_rdata_o <= m_axi_rdata_i;
                    s_axi_rresp_o <= (m_axi_rid_i == 16'd0 && m_axi_rlast_i)
                                     ? m_axi_rresp_i : 2'b10;
                    s_axi_rvalid_o <= 1'b1;
                    identity_valid_q[read_addr_q[2]] <=
                        m_axi_rresp_i == 2'b00 && m_axi_rid_i == 16'd0 && m_axi_rlast_i;
                    if (m_axi_rresp_i == 2'b00 && m_axi_rid_i == 16'd0 && m_axi_rlast_i)
                        identity_q[read_addr_q[2]] <= m_axi_rdata_i;
                    state_q <= IDLE;
                end
                default: state_q <= IDLE;
            endcase
        end
    end
endmodule
