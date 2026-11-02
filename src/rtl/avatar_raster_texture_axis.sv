// Standalone v1 pixel chain. Marker last is per triangle, not a DMA/video ABI.
module avatar_raster_texture_axis (
    input logic clk_i,rst_i,
    input logic upload_begin_valid_i,output logic upload_begin_ready_o,
    input logic tex_valid_i,output logic tex_ready_o,
    input logic [23:0] tex_rgb_i,input logic tex_last_i,
    output logic texture_valid_o,upload_error_o,
    input logic cmd_valid_i,output logic cmd_ready_o,
    input logic [47:0] bbox_i,
    input logic [95:0] edge_seed_i,edge_dx_i,edge_dy_i,
    input logic [79:0] uv_seed_i,input logic [127:0] uv_steps_i,
    input logic [8:0] shade_i,input logic empty_i,
    output logic out_valid_o,input logic out_ready_i,
    output logic [23:0] rgb_o,output logic [31:0] tag_o,
    output logic last_o,error_o
);
    logic busy_q,upload_active,core_ready,core_valid;
    logic frag_valid,frag_ready,frag_last;
    logic [15:0] frag_u;logic [16:0] frag_v;
    logic [8:0] frag_shade;logic [31:0] frag_tag;
    logic cache_valid,cache_ready,cache_last;
    logic [23:0] corners[4];logic [7:0] fraction_u,fraction_v;
    logic [8:0] cache_shade;logic [31:0] cache_tag;
    assign cmd_ready_o=!rst_i && !busy_q && texture_valid_o && !upload_active && !upload_begin_valid_i && core_ready;
    assign core_valid=cmd_valid_i && cmd_ready_o;
    always_ff @(posedge clk_i) begin
        if(rst_i) busy_q<=0;
        else if(core_valid) busy_q<=1;
        else if(out_valid_o && out_ready_i && last_o) busy_q<=0;
    end
    avatar_raster_fragments raster(
        .clk_i,.rst_i,.cmd_valid_i(core_valid),.cmd_ready_o(core_ready),
        .bbox_i,.edge_seed_i,.edge_dx_i,.edge_dy_i,.uv_seed_i,.uv_steps_i,.shade_i,.empty_i,
        .out_valid_o(frag_valid),.out_ready_i(frag_ready),.u_q8_o(frag_u),.v_q8_o(frag_v),
        .shade_o(frag_shade),.tag_o(frag_tag),.last_o(frag_last));
    avatar_texture_cache cache(
        .clk_i,.rst_i,.idle_i(!busy_q),.upload_begin_valid_i,.upload_begin_ready_o,
        .tex_valid_i,.tex_ready_o,.tex_rgb_i,.tex_last_i,.texture_valid_o,.upload_active_o(upload_active),.upload_error_o,
        .in_valid_i(frag_valid),.in_ready_o(frag_ready),.u_q8_i(frag_u),.v_q8_i(frag_v),
        .shade_i(frag_shade),.tag_i(frag_tag),.last_i(frag_last),
        .out_valid_o(cache_valid),.out_ready_i(cache_ready),
        .p00_o(corners[0]),.p10_o(corners[1]),.p01_o(corners[2]),.p11_o(corners[3]),
        .u_fraction_o(fraction_u),.v_fraction_o(fraction_v),.shade_o(cache_shade),.tag_o(cache_tag),.last_o(cache_last));
    avatar_bilinear_rgb8_axis painter(
        .clk_i,.rst_i,.in_valid_i(cache_valid),.in_ready_o(cache_ready),
        .p00_rgb_i(corners[0]),.p10_rgb_i(corners[1]),.p01_rgb_i(corners[2]),.p11_rgb_i(corners[3]),
        .u_i(fraction_u),.v_i(fraction_v),.shade_i(cache_shade),.tag_i(cache_tag),.last_i(cache_last),
        .out_valid_o,.out_ready_i,.rgb_o,.tag_o,.last_o,.error_o);
endmodule
