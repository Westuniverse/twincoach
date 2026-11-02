// Project-generated: see decisions/0066. Four-stage stallable texture pipeline.
(* use_dsp = "yes" *) module avatar_bilinear_rgb8_axis (
    input  logic clk_i,
    input  logic rst_i,
    input  logic in_valid_i,
    output logic in_ready_o,
    input  logic [23:0] p00_rgb_i, p10_rgb_i, p01_rgb_i, p11_rgb_i,
    input  logic [7:0] u_i, v_i,
    input  logic [8:0] shade_i,
    input  logic [31:0] tag_i,
    input  logic last_i,
    output logic out_valid_o,
    input  logic out_ready_i,
    output logic [23:0] rgb_o,
    output logic [31:0] tag_o,
    output logic last_o,
    output logic error_o
);
    logic [3:0] valid_q;
    logic advance;
    logic [15:0] top_q [3], bottom_q [3];
    logic [23:0] vertical_q [3];
    logic [32:0] shaded_q [3];
    logic [7:0] v_q;
    logic [8:0] shade_q [2];
    logic [31:0] tag_q [4];
    logic [3:0] last_q, error_q;
    assign advance = !valid_q[3] || out_ready_i;
    assign in_ready_o = advance && !rst_i;
    assign out_valid_o = valid_q[3] && !rst_i;
    assign tag_o = tag_q[3];
    assign last_o = last_q[3];
    assign error_o = error_q[3];

    function automatic logic [15:0] horizontal(
        input logic [7:0] a, b, fraction
    );
        logic signed [8:0] difference;
        logic signed [17:0] product, sum;
        begin
            difference = $signed({1'b0,b}) - $signed({1'b0,a});
            product = difference * $signed({1'b0,fraction});
            sum = $signed({2'b0,a,8'b0}) + product;
            horizontal = sum[15:0];
        end
    endfunction
    function automatic logic [23:0] vertical(
        input logic [15:0] a, b, input logic [7:0] fraction
    );
        logic signed [16:0] difference;
        logic signed [25:0] product, sum;
        begin
            difference = $signed({1'b0,b}) - $signed({1'b0,a});
            product = difference * $signed({1'b0,fraction});
            sum = $signed({2'b0,a,8'b0}) + product;
            vertical = sum[23:0];
        end
    endfunction

    always_ff @(posedge clk_i) begin
        if (rst_i) begin
            valid_q <= '0;
        end else if (advance) begin
            valid_q <= {valid_q[2:0], in_valid_i};
            if (in_valid_i) begin
                v_q <= v_i;
                shade_q[0] <= shade_i > 9'd256 ? 9'd0 : shade_i;
                tag_q[0] <= tag_i;
                last_q[0] <= last_i;
                error_q[0] <= shade_i > 9'd256;
                for (int c=0; c<3; c++) begin
                    top_q[c] <= horizontal(p00_rgb_i[c*8+:8], p10_rgb_i[c*8+:8], u_i);
                    bottom_q[c] <= horizontal(p01_rgb_i[c*8+:8], p11_rgb_i[c*8+:8], u_i);
                end
            end
            if (valid_q[0]) begin
                for (int c=0; c<3; c++)
                    vertical_q[c] <= vertical(top_q[c], bottom_q[c], v_q);
                shade_q[1] <= shade_q[0];
            end
            if (valid_q[1]) begin
                for (int c=0; c<3; c++)
                    shaded_q[c] <= vertical_q[c] * shade_q[1];
            end
            if (valid_q[2]) begin
                for (int c=0; c<3; c++)
                    rgb_o[c*8+:8] <= 8'((shaded_q[c] + 33'd8388608) >> 24);
            end
            for (int s=1; s<4; s++) begin
                if (valid_q[s-1]) begin
                    tag_q[s] <= tag_q[s-1];
                    last_q[s] <= last_q[s-1];
                    error_q[s] <= error_q[s-1];
                end
            end
        end
    end
endmodule
