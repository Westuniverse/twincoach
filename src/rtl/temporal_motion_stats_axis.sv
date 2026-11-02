// Aggregate one temporal-motion result batch into a compact telemetry beat.
//
// Input uses the output layout of temporal_motion_axis:
//   [34:0]   velocity_t1_sq
//   [69:35]  velocity_t2_sq
//   [104:70] acceleration_sq
//   [112:105] joint_id
//   [113]    view_id
//   TLAST    closes one selected-joint batch
//
// Output intentionally keeps only the fields consumed by anonymous motion
// telemetry.  It is not a replacement for the per-joint result stream used by
// debugging or future classifiers.
// With the default METRIC_W=35 and COUNT_W=16:
//   [50:0]   sum_acceleration_sq
//   [85:51]  max_acceleration_sq
//   [101:86] sample_count
//   [109:102] first_joint_id
//   [110]    view_id
//   [127:111] reserved zero
//   TLAST    always asserted for the one-beat summary
module temporal_motion_stats_axis #(
    parameter int unsigned METRIC_W = 35,
    parameter int unsigned COUNT_W = 16
) (
    input  logic                         clk_i,
    input  logic                         rst_i,

    input  logic                         s_axis_tvalid_i,
    output logic                         s_axis_tready_o,
    input  logic [127:0]                  s_axis_tdata_i,
    input  logic                         s_axis_tlast_i,

    output logic                         m_axis_tvalid_o,
    input  logic                         m_axis_tready_i,
    output logic [127:0]                  m_axis_tdata_o,
    output logic                         m_axis_tlast_o
);

    localparam int unsigned SUM_W = METRIC_W + COUNT_W;
    localparam int unsigned MAX_LSB = SUM_W;
    localparam int unsigned COUNT_LSB = MAX_LSB + METRIC_W;
    localparam int unsigned JOINT_LSB = COUNT_LSB + COUNT_W;
    localparam int unsigned VIEW_LSB = JOINT_LSB + 8;

    logic [SUM_W-1:0] acceleration_sum_q;
    logic [METRIC_W-1:0] acceleration_max_q;
    logic [COUNT_W-1:0] sample_count_q;
    logic [7:0] first_joint_id_q;
    logic view_id_q;
    logic metadata_valid_q;

    logic [METRIC_W-1:0] acceleration_input;
    logic [SUM_W-1:0] acceleration_sum_next;
    logic [METRIC_W-1:0] acceleration_max_next;
    logic [COUNT_W-1:0] sample_count_next;
    logic input_fire;
    logic output_ready;
    logic [127:0] output_data_q;
    logic [127:0] summary_data_next;

    assign acceleration_input = s_axis_tdata_i[70 +: METRIC_W];
    // Explicitly extend the metric by COUNT_W bits before accumulation.
    // This keeps the carry width visible to both Verilator and Vivado instead
    // of relying on expression sizing rules.
    assign acceleration_sum_next =
        acceleration_sum_q + {{COUNT_W{1'b0}}, acceleration_input};
    assign acceleration_max_next =
        (acceleration_input > acceleration_max_q)
            ? acceleration_input
            : acceleration_max_q;
    assign sample_count_next = sample_count_q + 1'b1;

    assign output_ready = ~m_axis_tvalid_o | m_axis_tready_i;
    assign s_axis_tready_o = output_ready;
    assign input_fire = s_axis_tvalid_i && s_axis_tready_o;

    always_comb begin
        summary_data_next = '0;
        summary_data_next[SUM_W-1:0] = acceleration_sum_next;
        summary_data_next[MAX_LSB +: METRIC_W] = acceleration_max_next;
        summary_data_next[COUNT_LSB +: COUNT_W] = sample_count_next;
        summary_data_next[JOINT_LSB +: 8] =
            metadata_valid_q ? first_joint_id_q : s_axis_tdata_i[112:105];
        summary_data_next[VIEW_LSB] =
            metadata_valid_q ? view_id_q : s_axis_tdata_i[113];
        m_axis_tdata_o = output_data_q;
        m_axis_tlast_o = m_axis_tvalid_o;
    end

    always_ff @(posedge clk_i) begin
        if (rst_i) begin
            m_axis_tvalid_o <= 1'b0;
            output_data_q <= '0;
            acceleration_sum_q <= '0;
            acceleration_max_q <= '0;
            sample_count_q <= '0;
            first_joint_id_q <= '0;
            view_id_q <= 1'b0;
            metadata_valid_q <= 1'b0;
        end else begin
            if (output_ready) begin
                m_axis_tvalid_o <= 1'b0;
            end

            if (input_fire) begin
                if (!metadata_valid_q) begin
                    first_joint_id_q <= s_axis_tdata_i[112:105];
                    view_id_q <= s_axis_tdata_i[113];
                end

                if (s_axis_tlast_i) begin
                    // Include the current input beat in the summary before
                    // clearing the accumulators for the next batch.
                    output_data_q <= summary_data_next;
                    m_axis_tvalid_o <= 1'b1;
                    acceleration_sum_q <= '0;
                    acceleration_max_q <= '0;
                    sample_count_q <= '0;
                    metadata_valid_q <= 1'b0;
                end else begin
                    acceleration_sum_q <= acceleration_sum_next;
                    acceleration_max_q <= acceleration_max_next;
                    sample_count_q <= sample_count_next;
                    metadata_valid_q <= 1'b1;
                end
            end
        end
    end

endmodule
