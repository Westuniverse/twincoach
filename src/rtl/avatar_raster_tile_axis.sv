// Standalone v1 textured tile renderer. No assignment to the deployed DMA opcodes.
module avatar_raster_tile_axis (
    input logic clk_i,rst_i,
    input logic upload_begin_valid_i,output logic upload_begin_ready_o,
    input logic tex_valid_i,output logic tex_ready_o,
    input logic [23:0] tex_rgb_i,input logic tex_last_i,
    output logic texture_valid_o,upload_error_o,
    input logic tile_begin_valid_i,output logic tile_begin_ready_o,
    input logic [11:0] origin_x_i,origin_y_i,input logic [23:0] background_i,
    input logic tile_finish_valid_i,output logic tile_finish_ready_o,
    input logic cmd_valid_i,output logic cmd_ready_o,
    input logic [47:0] bbox_i,input logic [95:0] edge_seed_i,edge_dx_i,edge_dy_i,
    input logic [79:0] uv_seed_i,input logic [127:0] uv_steps_i,
    input logic [8:0] shade_i,input logic empty_i,
    output logic out_valid_o,input logic out_ready_i,
    output logic [127:0] out_data_o,output logic out_last_o,out_error_o
);
    logic tile_active,tile_drawing,buffer_begin_ready,buffer_finish_ready;
    logic buffer_begin_valid,buffer_finish_valid;
    logic upload_ready,chain_cmd_ready,chain_cmd_valid,triangle_busy_q;
    logic frag_valid,frag_ready,frag_last,frag_error;
    logic [23:0] frag_rgb;logic [31:0] frag_tag;
    assign upload_begin_ready_o=upload_ready && !tile_active;
    assign tile_begin_ready_o=buffer_begin_ready && texture_valid_o && !upload_begin_valid_i;
    assign buffer_begin_valid=tile_begin_valid_i && tile_begin_ready_o;
    assign tile_finish_ready_o=buffer_finish_ready && !triangle_busy_q;
    assign buffer_finish_valid=tile_finish_valid_i && !triangle_busy_q;
    assign cmd_ready_o=tile_drawing && !triangle_busy_q && !tile_finish_valid_i && chain_cmd_ready;
    assign chain_cmd_valid=cmd_valid_i && cmd_ready_o;
    always_ff @(posedge clk_i) begin
        if(rst_i) triangle_busy_q<=0;
        else if(chain_cmd_valid) triangle_busy_q<=1;
        else if(frag_valid && frag_ready && frag_last) triangle_busy_q<=0;
    end
    avatar_raster_texture_axis chain(
        .clk_i,.rst_i,.upload_begin_valid_i(upload_begin_valid_i && !tile_active),
        .upload_begin_ready_o(upload_ready),.tex_valid_i,.tex_ready_o,.tex_rgb_i,.tex_last_i,
        .texture_valid_o,.upload_error_o,.cmd_valid_i(chain_cmd_valid),.cmd_ready_o(chain_cmd_ready),
        .bbox_i,.edge_seed_i,.edge_dx_i,.edge_dy_i,.uv_seed_i,.uv_steps_i,.shade_i,.empty_i,
        .out_valid_o(frag_valid),.out_ready_i(frag_ready),.rgb_o(frag_rgb),.tag_o(frag_tag),
        .last_o(frag_last),.error_o(frag_error));
    avatar_tile_buffer buffer(
        .clk_i,.rst_i,.begin_valid_i(buffer_begin_valid),.begin_ready_o(buffer_begin_ready),
        .origin_x_i,.origin_y_i,.background_i,.active_o(tile_active),.drawing_o(tile_drawing),
        .frag_valid_i(frag_valid),.frag_ready_o(frag_ready),.frag_rgb_i(frag_rgb),.frag_tag_i(frag_tag),
        .frag_last_i(frag_last),.frag_error_i(frag_error),
        .finish_valid_i(buffer_finish_valid),.finish_ready_o(buffer_finish_ready),
        .out_valid_o,.out_ready_i,.out_data_o,.out_last_o,.out_error_o);
endmodule
