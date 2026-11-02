// One selected operator per DMA packet. Configuration is locked from the first
// input handshake through the final OUTPUT handshake, including backpressure.
// Default PPS2 payload ABIs are unchanged. Optional PPS3 op4 adds the PXT1 renderer.
// See docs/rtl/shared_pose_accel_axis.md for reset and host validation contracts.
module shared_pose_accel_axis #(
    parameter bit INCLUDE_RASTER = 1'b0,
    parameter bit INCLUDE_SKINNING = 1'b0,
    parameter bit SKINNING_RESOURCE_SHARED = 1'b1
) (
    input  logic         clk_i,
    input  logic         rst_i,
    input  logic         cfg_valid_i,
    output logic         cfg_ready_o,
    input  logic [2:0]   cfg_operator_i,
    output logic         cfg_error_o,
    output logic         busy_o,
    output logic         texture_valid_o,
    input  logic         s_axis_tvalid_i,
    output logic         s_axis_tready_o,
    input  logic [127:0] s_axis_tdata_i,
    input  logic         s_axis_tlast_i,
    output logic         m_axis_tvalid_o,
    input  logic         m_axis_tready_i,
    output logic [127:0] m_axis_tdata_o,
    output logic [15:0]  m_axis_tkeep_o,
    output logic         m_axis_tlast_o
);
    localparam logic [2:0] OP_JOINT = 3'd0, OP_SUMMARY = 3'd1,
        OP_RECTIFIED = 3'd2, OP_LIGHTING = 3'd3, OP_RASTER = 3'd4, OP_SKINNING = 3'd5;
    logic [2:0] operator_q;
    logic busy_q, input_done_q, input_enabled;
    logic [5:0] input_valid, input_ready, output_valid, output_ready, output_last;
    logic [127:0] joint_data, summary_data, rectified_data, lighting_data, raster_data, skinning_data;
    logic [15:0] summary_keep, lighting_keep, raster_keep;
    logic input_fire, output_end;

    assign cfg_ready_o = !rst_i && !busy_q;
    assign busy_o = busy_q;
    // Configuration wins over a new packet, but a waiting configuration must
    // not stall the current packet while cfg_ready is low.
    assign input_enabled = !rst_i && !input_done_q && (busy_q || !cfg_valid_i);
    assign input_fire = s_axis_tvalid_i && s_axis_tready_o;
    assign output_end = m_axis_tvalid_o && m_axis_tready_i && m_axis_tlast_o;
    assign m_axis_tkeep_o = operator_q == OP_SUMMARY ? summary_keep :
        (operator_q == OP_LIGHTING ? lighting_keep :
        (INCLUDE_RASTER && operator_q == OP_RASTER ? raster_keep : 16'hffff));

    always_comb begin
        input_valid = '0;
        output_ready = '0;
        s_axis_tready_o = 1'b0;
        m_axis_tvalid_o = 1'b0;
        m_axis_tdata_o = '0;
        m_axis_tlast_o = 1'b0;
        case (operator_q)
            OP_JOINT: begin
                input_valid[0] = s_axis_tvalid_i && input_enabled;
                s_axis_tready_o = input_ready[0] && input_enabled;
                output_ready[0] = m_axis_tready_i && busy_q && !rst_i;
                m_axis_tvalid_o = output_valid[0] && busy_q && !rst_i;
                m_axis_tdata_o = joint_data;
                m_axis_tlast_o = output_last[0];
            end
            OP_SUMMARY: begin
                input_valid[1] = s_axis_tvalid_i && input_enabled;
                s_axis_tready_o = input_ready[1] && input_enabled;
                output_ready[1] = m_axis_tready_i && busy_q && !rst_i;
                m_axis_tvalid_o = output_valid[1] && busy_q && !rst_i;
                m_axis_tdata_o = summary_data;
                m_axis_tlast_o = output_last[1];
            end
            OP_RECTIFIED: begin
                input_valid[2] = s_axis_tvalid_i && input_enabled;
                s_axis_tready_o = input_ready[2] && input_enabled;
                output_ready[2] = m_axis_tready_i && busy_q && !rst_i;
                m_axis_tvalid_o = output_valid[2] && busy_q && !rst_i;
                m_axis_tdata_o = rectified_data;
                m_axis_tlast_o = output_last[2];
            end
            OP_LIGHTING: begin
                input_valid[3] = s_axis_tvalid_i && input_enabled;
                s_axis_tready_o = input_ready[3] && input_enabled;
                output_ready[3] = m_axis_tready_i && busy_q && !rst_i;
                m_axis_tvalid_o = output_valid[3] && busy_q && !rst_i;
                m_axis_tdata_o = lighting_data;
                m_axis_tlast_o = output_last[3];
            end
            OP_RASTER: if (INCLUDE_RASTER) begin
                input_valid[4] = s_axis_tvalid_i && input_enabled;
                s_axis_tready_o = input_ready[4] && input_enabled;
                output_ready[4] = m_axis_tready_i && busy_q && !rst_i;
                m_axis_tvalid_o = output_valid[4] && busy_q && !rst_i;
                m_axis_tdata_o = raster_data;
                m_axis_tlast_o = output_last[4];
            end
            OP_SKINNING: if (INCLUDE_SKINNING) begin
                input_valid[5] = s_axis_tvalid_i && input_enabled;
                s_axis_tready_o = input_ready[5] && input_enabled;
                output_ready[5] = m_axis_tready_i && busy_q && !rst_i;
                m_axis_tvalid_o = output_valid[5] && busy_q && !rst_i;
                m_axis_tdata_o = skinning_data;
                m_axis_tlast_o = output_last[5];
            end
            default: begin end
        endcase
    end

    always_ff @(posedge clk_i) begin
        if (rst_i) begin
            operator_q <= 3'd7;
            busy_q <= 1'b0;
            input_done_q <= 1'b0;
            cfg_error_o <= 1'b0;
        end else begin
            cfg_error_o <= 1'b0;
            if (cfg_valid_i && cfg_ready_o) begin
                operator_q <= cfg_operator_i;
                cfg_error_o <= !((cfg_operator_i <= OP_LIGHTING) ||
                    (INCLUDE_RASTER && cfg_operator_i == OP_RASTER) ||
                    (INCLUDE_SKINNING && cfg_operator_i == OP_SKINNING));
            end
            if (input_fire) begin
                busy_q <= 1'b1;
                if (s_axis_tlast_i) input_done_q <= 1'b1;
            end
            if (output_end) begin
                busy_q <= 1'b0;
                input_done_q <= 1'b0;
            end
        end
    end

    joint_metrics_axis joint_axis (
        .clk_i, .rst_i,
        .s_axis_tvalid_i(input_valid[0]), .s_axis_tready_o(input_ready[0]),
        .s_axis_tdata_i, .s_axis_tlast_i,
        .m_axis_tvalid_o(output_valid[0]), .m_axis_tready_i(output_ready[0]),
        .m_axis_tdata_o(joint_data), .m_axis_tlast_o(output_last[0])
    );
    temporal_motion_summary_axis_ip summary_axis (
        .axis_aclk(clk_i), .axis_aresetn(!rst_i),
        .s_axis_tvalid(input_valid[1]), .s_axis_tready(input_ready[1]),
        .s_axis_tdata(s_axis_tdata_i), .s_axis_tlast(s_axis_tlast_i),
        .m_axis_tvalid(output_valid[1]), .m_axis_tready(output_ready[1]),
        .m_axis_tdata(summary_data), .m_axis_tkeep(summary_keep), .m_axis_tlast(output_last[1])
    );
    rectified_triangulation_axis rectified_axis (
        .clk_i, .rst_i,
        .s_axis_tvalid_i(input_valid[2]), .s_axis_tready_o(input_ready[2]),
        .s_axis_tdata_i, .s_axis_tlast_i,
        .m_axis_tvalid_o(output_valid[2]), .m_axis_tready_i(output_ready[2]),
        .m_axis_tdata_o(rectified_data), .m_axis_tlast_o(output_last[2])
    );
    avatar_triangle_lighting_axis lighting_axis (
        .clk_i, .rst_i,
        .s_axis_tvalid_i(input_valid[3]), .s_axis_tready_o(input_ready[3]),
        .s_axis_tdata_i, .s_axis_tkeep_i(16'hffff), .s_axis_tlast_i,
        .m_axis_tvalid_o(output_valid[3]), .m_axis_tready_i(output_ready[3]),
        .m_axis_tdata_o(lighting_data), .m_axis_tkeep_o(lighting_keep),
        .m_axis_tlast_o(output_last[3]),
        // Packet errors are returned explicitly in result bit57, not hidden.
        .protocol_error_o()
    );
    generate if (INCLUDE_RASTER) begin : raster_enabled
        avatar_raster_packet_axis raster (
            .clk_i, .rst_i, .s_valid_i(input_valid[4]), .s_ready_o(input_ready[4]),
            .s_data_i(s_axis_tdata_i), .s_keep_i(16'hffff), .s_last_i(s_axis_tlast_i),
            .m_valid_o(output_valid[4]), .m_ready_i(output_ready[4]),
            .m_data_o(raster_data), .m_keep_o(raster_keep), .m_last_o(output_last[4]),
            .texture_valid_o
        );
    end else begin : raster_disabled
        assign input_ready[4]=1'b0;
        assign output_valid[4]=1'b0;
        assign output_last[4]=1'b0;
        assign raster_data='0;
        assign raster_keep=16'hffff;
        assign texture_valid_o=1'b0;
    end endgenerate
    generate if (INCLUDE_SKINNING) begin : skinning_enabled
        avatar_skinning_packet_axis #(.RESOURCE_SHARED(SKINNING_RESOURCE_SHARED)) skinning (
            .axis_aclk(clk_i), .axis_aresetn(!rst_i),
            .s_axis_tvalid(input_valid[5]), .s_axis_tready(input_ready[5]),
            .s_axis_tdata(s_axis_tdata_i), .s_axis_tlast(s_axis_tlast_i),
            .m_axis_tvalid(output_valid[5]), .m_axis_tready(output_ready[5]),
            .m_axis_tdata(skinning_data), .m_axis_tkeep(), .m_axis_tlast(output_last[5])
        );
    end else begin : skinning_disabled
        assign input_ready[5]=1'b0;
        assign output_valid[5]=1'b0;
        assign output_last[5]=1'b0;
        assign skinning_data='0;
    end endgenerate
endmodule
