// Passive external AXI observation only. No observed bus is driven by this block.
module dpu_axi_observer #(
    parameter int unsigned COUNTER_BITS = 64
) (
    (* X_INTERFACE_INFO = "xilinx.com:signal:clock:1.0 aclk CLK" *)
    (* X_INTERFACE_PARAMETER = "XIL_INTERFACENAME aclk, ASSOCIATED_BUSIF s_axi, ASSOCIATED_RESET aresetn" *)
    input logic aclk,
    (* X_INTERFACE_INFO = "xilinx.com:signal:reset:1.0 aresetn RST" *)
    (* X_INTERFACE_PARAMETER = "XIL_INTERFACENAME aresetn, POLARITY ACTIVE_LOW" *)
    input logic aresetn,
    input logic [39:0] observed_araddr_i, observed_awaddr_i,
    input logic observed_arvalid_i, observed_arready_i,
    input logic observed_rvalid_i, observed_rready_i, observed_rlast_i,
    input logic [1:0] observed_rresp_i, observed_bresp_i,
    input logic observed_awvalid_i, observed_awready_i,
    input logic observed_wvalid_i, observed_wready_i, observed_wlast_i,
    input logic observed_bvalid_i, observed_bready_i,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi AWADDR" *)
    (* X_INTERFACE_PARAMETER = "XIL_INTERFACENAME s_axi, PROTOCOL AXI4LITE, DATA_WIDTH 32, ADDR_WIDTH 12" *)
    input logic [11:0] s_axi_awaddr,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi AWVALID" *) input logic s_axi_awvalid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi AWREADY" *) output logic s_axi_awready,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi WDATA" *) input logic [31:0] s_axi_wdata,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi WSTRB" *) input logic [3:0] s_axi_wstrb,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi WVALID" *) input logic s_axi_wvalid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi WREADY" *) output logic s_axi_wready,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi BRESP" *) output logic [1:0] s_axi_bresp,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi BVALID" *) output logic s_axi_bvalid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi BREADY" *) input logic s_axi_bready,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi ARADDR" *) input logic [11:0] s_axi_araddr,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi ARVALID" *) input logic s_axi_arvalid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi ARREADY" *) output logic s_axi_arready,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi RDATA" *) output logic [31:0] s_axi_rdata,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi RRESP" *) output logic [1:0] s_axi_rresp,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi RVALID" *) output logic s_axi_rvalid,
    (* X_INTERFACE_INFO = "xilinx.com:interface:aximm:1.0 s_axi RREADY" *) input logic s_axi_rready
);
    // Write payload is intentionally unused: every address/strobe is rejected.
    logic aw_held_q, w_held_q;
    // Only the legacy 32-bit ABI and the wide 64-bit ABI are supported.
    generate
        if (COUNTER_BITS != 32 && COUNTER_BITS != 64) begin : g_invalid_counter_bits
            initial $error("COUNTER_BITS must be 32 or 64");
        end
    endgenerate
    logic [COUNTER_BITS-1:0] counters_q [0:14];
    logic [14:0] increment;
    logic [COUNTER_BITS-1:0] out_of_bank_q;
    logic [31:0] last_rresp_q, last_bresp_q;
    logic [39:0] last_ar_q, last_aw_q, first_ar_q;
    logic [31:0] read_data;
    logic [1:0] read_resp, bank_increment;
    wire ar_fire = observed_arvalid_i && observed_arready_i;
    wire r_fire = observed_rvalid_i && observed_rready_i;
    wire aw_fire = observed_awvalid_i && observed_awready_i;
    wire w_fire = observed_wvalid_i && observed_wready_i;
    wire b_fire = observed_bvalid_i && observed_bready_i;

    function automatic logic [COUNTER_BITS-1:0] saturating_add(
        input logic [COUNTER_BITS-1:0] value, input logic [1:0] amount
    );
        logic [COUNTER_BITS:0] sum;
        sum = {1'b0, value} + {{(COUNTER_BITS-1){1'b0}}, amount};
        return sum[COUNTER_BITS] ? '1 : sum[COUNTER_BITS-1:0];
    endfunction

    always_comb begin
        increment = '0;
        increment[0] = 1'b1; // Cycles since reset.
        increment[1] = ar_fire;
        increment[2] = r_fire;
        increment[3] = r_fire && observed_rlast_i;
        increment[4] = r_fire && observed_rresp_i[1];
        increment[5] = observed_arvalid_i && !observed_arready_i;
        increment[6] = observed_rvalid_i && !observed_rready_i;
        increment[7] = aw_fire;
        increment[8] = w_fire;
        increment[9] = w_fire && observed_wlast_i;
        increment[10] = b_fire;
        increment[11] = b_fire && observed_bresp_i[1];
        increment[12] = observed_awvalid_i && !observed_awready_i;
        increment[13] = observed_wvalid_i && !observed_wready_i;
        increment[14] = observed_bvalid_i && !observed_bready_i;
        bank_increment = {1'b0, (ar_fire && |observed_araddr_i[39:31])} +
                         {1'b0, (aw_fire && |observed_awaddr_i[39:31])};
    end

    always_ff @(posedge aclk) begin
        if (!aresetn) begin
            for (int n = 0; n < 15; n++) counters_q[n] <= '0;
            out_of_bank_q <= '0;
            last_ar_q <= '0;
            last_aw_q <= '0;
            first_ar_q <= '0;
            last_rresp_q <= '0;
            last_bresp_q <= '0;
        end else begin
            for (int n = 0; n < 15; n++)
                counters_q[n] <= saturating_add(counters_q[n], {1'b0, increment[n]});
            out_of_bank_q <= saturating_add(out_of_bank_q, bank_increment);
            if (ar_fire) begin
                last_ar_q <= observed_araddr_i;
                if (counters_q[1] == 0) first_ar_q <= observed_araddr_i;
            end
            if (aw_fire) last_aw_q <= observed_awaddr_i;
            if (r_fire) last_rresp_q <= {1'b1, 29'b0, observed_rresp_i};
            if (b_fire) last_bresp_q <= {1'b1, 29'b0, observed_bresp_i};
        end
    end

    always_comb begin
        read_data = '0;
        read_resp = 2'b00;
        case (s_axi_araddr)
            12'h000: read_data = 32'h41584f42;
            12'h004: read_data = (COUNTER_BITS == 64) ? 32'd2 : 32'd1;
            12'h008: read_data = {20'b0, observed_bready_i, observed_bvalid_i,
                observed_wlast_i, observed_wready_i, observed_wvalid_i,
                observed_awready_i, observed_awvalid_i, observed_rlast_i,
                observed_rready_i, observed_rvalid_i, observed_arready_i, observed_arvalid_i};
            12'h00c: read_data = counters_q[0][31:0];
            12'h010: read_data = counters_q[1][31:0];
            12'h014: read_data = counters_q[2][31:0];
            12'h018: read_data = counters_q[3][31:0];
            12'h01c: read_data = counters_q[4][31:0];
            12'h020: read_data = counters_q[5][31:0];
            12'h024: read_data = counters_q[6][31:0];
            12'h028: read_data = counters_q[7][31:0];
            12'h02c: read_data = counters_q[8][31:0];
            12'h030: read_data = counters_q[9][31:0];
            12'h034: read_data = counters_q[10][31:0];
            12'h038: read_data = counters_q[11][31:0];
            12'h03c: read_data = counters_q[12][31:0];
            12'h040: read_data = counters_q[13][31:0];
            12'h044: read_data = counters_q[14][31:0];
            12'h048: read_data = last_ar_q[31:0];
            12'h04c: read_data = {24'b0, last_ar_q[39:32]};
            12'h050: read_data = last_aw_q[31:0];
            12'h054: read_data = {24'b0, last_aw_q[39:32]};
            12'h058: read_data = last_rresp_q;
            12'h05c: read_data = last_bresp_q;
            12'h060: read_data = out_of_bank_q[31:0];
            12'h064: read_data = first_ar_q[31:0];
            12'h068: read_data = {24'b0, first_ar_q[39:32]};
            12'h080, 12'h084, 12'h088, 12'h08c, 12'h090, 12'h094,
            12'h098, 12'h09c, 12'h0a0, 12'h0a4, 12'h0a8, 12'h0ac,
            12'h0b0, 12'h0b4, 12'h0b8, 12'h0bc: begin
                if (COUNTER_BITS == 64) begin
                    if (s_axi_araddr == 12'h0bc)
                        read_data = 32'(out_of_bank_q >> 32);
                    else
                        read_data = 32'(counters_q[s_axi_araddr[5:2]] >> 32);
                end else read_resp = 2'b10;
            end
            default: read_resp = 2'b10;
        endcase
    end

    assign s_axi_arready = aresetn && !s_axi_rvalid;
    assign s_axi_awready = aresetn && !aw_held_q && !s_axi_bvalid;
    assign s_axi_wready = aresetn && !w_held_q && !s_axi_bvalid;
    assign s_axi_bresp = 2'b10;
    always_ff @(posedge aclk) begin
        if (!aresetn) begin
            s_axi_rvalid <= 1'b0;
            s_axi_rdata <= '0;
            s_axi_rresp <= '0;
            s_axi_bvalid <= 1'b0;
            aw_held_q <= 1'b0;
            w_held_q <= 1'b0;
        end else begin
            if (s_axi_rvalid && s_axi_rready) s_axi_rvalid <= 1'b0;
            if (s_axi_arvalid && s_axi_arready) begin
                s_axi_rvalid <= 1'b1;
                s_axi_rdata <= read_data;
                s_axi_rresp <= read_resp;
            end
            if (s_axi_bvalid && s_axi_bready) s_axi_bvalid <= 1'b0;
            if (s_axi_awvalid && s_axi_awready) aw_held_q <= 1'b1;
            if (s_axi_wvalid && s_axi_wready) w_held_q <= 1'b1;
            if (!s_axi_bvalid &&
                (aw_held_q || (s_axi_awvalid && s_axi_awready)) &&
                (w_held_q || (s_axi_wvalid && s_axi_wready))) begin
                aw_held_q <= 1'b0;
                w_held_q <= 1'b0;
                s_axi_bvalid <= 1'b1;
            end
        end
    end
endmodule
