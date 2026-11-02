module affine_transform_3d_core (
    input  logic                 clk_i,
    input  logic                 rst_i,

    input  logic                 in_valid_i,
    output logic                 in_ready_o,
    input  logic signed [15:0]   point_x_i,
    input  logic signed [15:0]   point_y_i,
    input  logic signed [15:0]   point_z_i,
    input  logic signed [15:0]   matrix_00_i,
    input  logic signed [15:0]   matrix_01_i,
    input  logic signed [15:0]   matrix_02_i,
    input  logic signed [15:0]   matrix_03_i,
    input  logic signed [15:0]   matrix_10_i,
    input  logic signed [15:0]   matrix_11_i,
    input  logic signed [15:0]   matrix_12_i,
    input  logic signed [15:0]   matrix_13_i,
    input  logic signed [15:0]   matrix_20_i,
    input  logic signed [15:0]   matrix_21_i,
    input  logic signed [15:0]   matrix_22_i,
    input  logic signed [15:0]   matrix_23_i,

    output logic                 out_valid_o,
    input  logic                 out_ready_i,
    output logic signed [33:0]   point_x_o,
    output logic signed [33:0]   point_y_o,
    output logic signed [33:0]   point_z_o
);

    localparam int unsigned DATA_W = 16;
    localparam int unsigned FRAC_W = 14;
    localparam int unsigned PRODUCT_W = 2 * DATA_W;
    localparam int unsigned ACC_W = PRODUCT_W + 2;

    logic signed [PRODUCT_W-1:0] product_00;
    logic signed [PRODUCT_W-1:0] product_01;
    logic signed [PRODUCT_W-1:0] product_02;
    logic signed [PRODUCT_W-1:0] product_10;
    logic signed [PRODUCT_W-1:0] product_11;
    logic signed [PRODUCT_W-1:0] product_12;
    logic signed [PRODUCT_W-1:0] product_20;
    logic signed [PRODUCT_W-1:0] product_21;
    logic signed [PRODUCT_W-1:0] product_22;

    logic signed [ACC_W-1:0] product_00_ext;
    logic signed [ACC_W-1:0] product_01_ext;
    logic signed [ACC_W-1:0] product_02_ext;
    logic signed [ACC_W-1:0] product_10_ext;
    logic signed [ACC_W-1:0] product_11_ext;
    logic signed [ACC_W-1:0] product_12_ext;
    logic signed [ACC_W-1:0] product_20_ext;
    logic signed [ACC_W-1:0] product_21_ext;
    logic signed [ACC_W-1:0] product_22_ext;
    logic signed [ACC_W-1:0] translation_x_scaled;
    logic signed [ACC_W-1:0] translation_y_scaled;
    logic signed [ACC_W-1:0] translation_z_scaled;
    logic signed [ACC_W-1:0] transformed_x;
    logic signed [ACC_W-1:0] transformed_y;
    logic signed [ACC_W-1:0] transformed_z;

    assign in_ready_o = ~out_valid_o | out_ready_i;

    assign product_00 = matrix_00_i * point_x_i;
    assign product_01 = matrix_01_i * point_y_i;
    assign product_02 = matrix_02_i * point_z_i;
    assign product_10 = matrix_10_i * point_x_i;
    assign product_11 = matrix_11_i * point_y_i;
    assign product_12 = matrix_12_i * point_z_i;
    assign product_20 = matrix_20_i * point_x_i;
    assign product_21 = matrix_21_i * point_y_i;
    assign product_22 = matrix_22_i * point_z_i;

    assign product_00_ext = {{(ACC_W-PRODUCT_W){product_00[PRODUCT_W-1]}}, product_00};
    assign product_01_ext = {{(ACC_W-PRODUCT_W){product_01[PRODUCT_W-1]}}, product_01};
    assign product_02_ext = {{(ACC_W-PRODUCT_W){product_02[PRODUCT_W-1]}}, product_02};
    assign product_10_ext = {{(ACC_W-PRODUCT_W){product_10[PRODUCT_W-1]}}, product_10};
    assign product_11_ext = {{(ACC_W-PRODUCT_W){product_11[PRODUCT_W-1]}}, product_11};
    assign product_12_ext = {{(ACC_W-PRODUCT_W){product_12[PRODUCT_W-1]}}, product_12};
    assign product_20_ext = {{(ACC_W-PRODUCT_W){product_20[PRODUCT_W-1]}}, product_20};
    assign product_21_ext = {{(ACC_W-PRODUCT_W){product_21[PRODUCT_W-1]}}, product_21};
    assign product_22_ext = {{(ACC_W-PRODUCT_W){product_22[PRODUCT_W-1]}}, product_22};

    assign translation_x_scaled =
        $signed({{(ACC_W-DATA_W){matrix_03_i[DATA_W-1]}}, matrix_03_i}) <<< FRAC_W;
    assign translation_y_scaled =
        $signed({{(ACC_W-DATA_W){matrix_13_i[DATA_W-1]}}, matrix_13_i}) <<< FRAC_W;
    assign translation_z_scaled =
        $signed({{(ACC_W-DATA_W){matrix_23_i[DATA_W-1]}}, matrix_23_i}) <<< FRAC_W;

    assign transformed_x = product_00_ext + product_01_ext + product_02_ext + translation_x_scaled;
    assign transformed_y = product_10_ext + product_11_ext + product_12_ext + translation_y_scaled;
    assign transformed_z = product_20_ext + product_21_ext + product_22_ext + translation_z_scaled;

    always_ff @(posedge clk_i) begin
        if (rst_i) begin
            out_valid_o <= 1'b0;
        end else if (in_ready_o) begin
            out_valid_o <= in_valid_i;
            if (in_valid_i) begin
                point_x_o <= transformed_x;
                point_y_o <= transformed_y;
                point_z_o <= transformed_z;
            end
        end
    end

endmodule
