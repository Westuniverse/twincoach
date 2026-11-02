module joint_metrics_axis (
    input  logic          clk_i,
    input  logic          rst_i,

    input  logic          s_axis_tvalid_i,
    output logic          s_axis_tready_o,
    input  logic [127:0]  s_axis_tdata_i,
    input  logic          s_axis_tlast_i,

    output logic          m_axis_tvalid_o,
    input  logic          m_axis_tready_i,
    output logic [127:0]  m_axis_tdata_o,
    output logic          m_axis_tlast_o
);

    localparam int unsigned COORD_W  = 16;
    localparam int unsigned METRIC_W = 35;

    logic signed [METRIC_W-1:0] dot_product;
    logic        [METRIC_W-1:0] magnitude_sq_ab;
    logic        [METRIC_W-1:0] magnitude_sq_cb;

    logic       metadata_input_valid_q;
    logic [7:0] metadata_input_joint_id_q;
    logic       metadata_input_view_id_q;
    logic       metadata_input_last_q;
    logic [7:0] metadata_output_joint_id_q;
    logic       metadata_output_view_id_q;
    logic       metadata_output_last_q;
    logic       output_ready;

    assign output_ready = ~m_axis_tvalid_o | m_axis_tready_i;

    joint_metrics_core #(
        .COORD_W(COORD_W)
    ) metrics_core (
        .clk_i,
        .rst_i,
        .in_valid_i(s_axis_tvalid_i),
        .in_ready_o(s_axis_tready_o),
        .point_a_x_i($signed(s_axis_tdata_i[15:0])),
        .point_a_y_i($signed(s_axis_tdata_i[31:16])),
        .point_b_x_i($signed(s_axis_tdata_i[47:32])),
        .point_b_y_i($signed(s_axis_tdata_i[63:48])),
        .point_c_x_i($signed(s_axis_tdata_i[79:64])),
        .point_c_y_i($signed(s_axis_tdata_i[95:80])),
        .out_valid_o(m_axis_tvalid_o),
        .out_ready_i(m_axis_tready_i),
        .dot_product_o(dot_product),
        .magnitude_sq_ab_o(magnitude_sq_ab),
        .magnitude_sq_cb_o(magnitude_sq_cb)
    );

    always_comb begin
        m_axis_tdata_o = '0;
        m_axis_tdata_o[34:0]    = dot_product;
        m_axis_tdata_o[69:35]   = magnitude_sq_ab;
        m_axis_tdata_o[104:70]  = magnitude_sq_cb;
        m_axis_tdata_o[112:105] = metadata_output_joint_id_q;
        m_axis_tdata_o[113]     = metadata_output_view_id_q;
        m_axis_tlast_o          = metadata_output_last_q;
    end

    always_ff @(posedge clk_i) begin
        if (rst_i) begin
            metadata_input_valid_q <= 1'b0;
            metadata_output_last_q <= 1'b0;
        end else begin
            if (output_ready && metadata_input_valid_q) begin
                metadata_output_joint_id_q <= metadata_input_joint_id_q;
                metadata_output_view_id_q  <= metadata_input_view_id_q;
                metadata_output_last_q     <= metadata_input_last_q;
            end

            if (s_axis_tready_o) begin
                metadata_input_valid_q <= s_axis_tvalid_i;
                if (s_axis_tvalid_i) begin
                    metadata_input_joint_id_q <= s_axis_tdata_i[103:96];
                    metadata_input_view_id_q  <= s_axis_tdata_i[104];
                    metadata_input_last_q     <= s_axis_tlast_i;
                end
            end
        end
    end

endmodule
