// PXT1/PXR1 v1 standalone packet frontend, ADR0072. Not a deployed shared opcode.
module avatar_raster_packet_axis (
    input logic clk_i,rst_i,
    input logic s_valid_i,output logic s_ready_o,
    input logic [127:0] s_data_i,input logic [15:0] s_keep_i,input logic s_last_i,
    output logic m_valid_o,input logic m_ready_i,
    output logic [127:0] m_data_o,output logic [15:0] m_keep_o,output logic m_last_o,
    output logic texture_valid_o
);
    typedef enum logic [3:0] {HEADER,UPLOAD_BEGIN,TEX_READ,TEX_PIX,TEX_WAIT,
        TILE_BEGIN,DESC_READ,COMMAND,FINISH,WAIT_TILE,RESPONSE,PIXELS,DRAIN} state_t;
    state_t state_q;
    logic abort_q;
    logic [7:0] op_q,status_q;
    logic [15:0] count_q,payload_q;
    logic [11:0] ox_q,oy_q;logic [23:0] background_q;
    logic [14:0] tex_index_q;
    logic [1:0] tex_lane_q;
    logic [127:0] tex_word_q;
    logic [2:0] desc_beat_q;
    logic [639:0] descriptor_q;
    logic upload_ready,tex_ready,upload_error,begin_ready,finish_ready,cmd_ready;
    logic pixel_valid,pixel_last,pixel_error;
    logic [127:0] pixel_data,response_data;
    logic header_bad,tex_bad,desc_bad,expected_desc_last;
    logic [7:0] header_op;
    logic [15:0] header_count;
    assign header_op=s_data_i[39:32];assign header_count=s_data_i[63:48];
    always_comb begin
        header_bad=s_keep_i!=16'hffff || s_data_i[31:0]!=32'h31545850
            || s_data_i[47:40]!=8'd1 || s_data_i[127:112]!=0;
        if(header_op==8'd1) header_bad=header_bad || s_last_i || s_data_i[111:48]!=0;
        else if(header_op==8'd2) header_bad=header_bad || !texture_valid_o
            || s_data_i[75:64]>12'd4064 || s_data_i[87:76]>12'd4064 || s_last_i!=(header_count==0);
        else header_bad=1;
    end
    assign tex_bad=s_keep_i!=16'hffff || s_last_i!=(tex_index_q==15'd32767)
        || s_data_i[31:24]!=0 || s_data_i[63:56]!=0 || s_data_i[95:88]!=0 || s_data_i[127:120]!=0;
    assign expected_desc_last=count_q==16'd1 && desc_beat_q==3'd4;
    assign desc_bad=s_keep_i!=16'hffff || s_last_i!=expected_desc_last
        || (desc_beat_q==3'd4 && s_data_i[127:42]!=0);
    assign s_ready_o=!rst_i && (state_q==HEADER || state_q==TEX_READ || state_q==DESC_READ || state_q==DRAIN);
    assign response_data={48'b0,payload_q,8'b0,op_q,8'd1,status_q,32'h31525850};
    assign m_valid_o=!rst_i && (state_q==RESPONSE || (state_q==PIXELS && pixel_valid));
    assign m_data_o=state_q==PIXELS ? pixel_data : response_data;
    assign m_keep_o=16'hffff;
    assign m_last_o=state_q==PIXELS ? pixel_last : payload_q==0;

    avatar_raster_tile_axis renderer(
        .clk_i,.rst_i(rst_i || abort_q),
        .upload_begin_valid_i(state_q==UPLOAD_BEGIN),.upload_begin_ready_o(upload_ready),
        .tex_valid_i(state_q==TEX_PIX),.tex_ready_o(tex_ready),
        .tex_rgb_i(tex_word_q[tex_lane_q*32+:24]),
        .tex_last_i(tex_index_q==15'd32767 && tex_lane_q==2'd3),
        .texture_valid_o,.upload_error_o(upload_error),
        .tile_begin_valid_i(state_q==TILE_BEGIN),.tile_begin_ready_o(begin_ready),
        .origin_x_i(ox_q),.origin_y_i(oy_q),.background_i(background_q),
        .tile_finish_valid_i(state_q==FINISH),.tile_finish_ready_o(finish_ready),
        .cmd_valid_i(state_q==COMMAND),.cmd_ready_o(cmd_ready),
        .bbox_i(descriptor_q[47:0]),.edge_seed_i(descriptor_q[143:48]),
        .edge_dx_i(descriptor_q[239:144]),.edge_dy_i(descriptor_q[335:240]),
        .uv_seed_i(descriptor_q[415:336]),.uv_steps_i(descriptor_q[543:416]),
        .shade_i(descriptor_q[552:544]),.empty_i(descriptor_q[553]),
        .out_valid_o(pixel_valid),.out_ready_i(state_q==PIXELS && m_ready_i && !rst_i),
        .out_data_o(pixel_data),.out_last_o(pixel_last),.out_error_o(pixel_error));
    always_ff @(posedge clk_i) begin
        if(rst_i) begin state_q<=HEADER;abort_q<=0;end
        else begin
            case(state_q)
                HEADER:if(s_valid_i) begin
                    op_q<=header_op;status_q<=0;payload_q<=0;
                    if(header_bad) begin
                        abort_q<=1;status_q<=1;state_q<=s_last_i ? RESPONSE:DRAIN;
                    end else if(header_op==8'd1) begin
                        tex_index_q<=0;state_q<=UPLOAD_BEGIN;
                    end else begin
                        count_q<=header_count;ox_q<=s_data_i[75:64];oy_q<=s_data_i[87:76];
                        background_q<=s_data_i[111:88];state_q<=TILE_BEGIN;
                    end
                end
                UPLOAD_BEGIN:if(upload_ready) state_q<=TEX_READ;
                TEX_READ:if(s_valid_i) begin
                    if(tex_bad) begin abort_q<=1;status_q<=1;state_q<=s_last_i ? RESPONSE:DRAIN;end
                    else begin tex_word_q<=s_data_i;tex_lane_q<=0;state_q<=TEX_PIX;end
                end
                TEX_PIX:if(tex_ready) begin
                    if(tex_lane_q==2'd3) begin
                        if(tex_index_q==15'd32767) state_q<=TEX_WAIT;
                        else begin tex_index_q<=tex_index_q+1'b1;state_q<=TEX_READ;end
                    end else tex_lane_q<=tex_lane_q+1'b1;
                end
                TEX_WAIT:begin
                    if(upload_error) begin abort_q<=1;status_q<=1;state_q<=RESPONSE;end
                    else if(texture_valid_o) state_q<=RESPONSE;
                end
                TILE_BEGIN:if(begin_ready) begin
                    desc_beat_q<=0;state_q<=count_q==0 ? FINISH:DESC_READ;
                end
                DESC_READ:if(s_valid_i) begin
                    if(desc_bad) begin abort_q<=1;status_q<=1;state_q<=s_last_i ? RESPONSE:DRAIN;end
                    else begin
                        case(desc_beat_q)
                            3'd0:descriptor_q[127:0]<=s_data_i;
                            3'd1:descriptor_q[255:128]<=s_data_i;
                            3'd2:descriptor_q[383:256]<=s_data_i;
                            3'd3:descriptor_q[511:384]<=s_data_i;
                            3'd4:descriptor_q[639:512]<=s_data_i;
                            default:begin end
                        endcase
                        if(desc_beat_q==3'd4) state_q<=COMMAND;else desc_beat_q<=desc_beat_q+1'b1;
                    end
                end
                COMMAND:if(cmd_ready) begin
                    count_q<=count_q-1'b1;desc_beat_q<=0;
                    state_q<=count_q==16'd1 ? FINISH:DESC_READ;
                end
                FINISH:if(finish_ready) state_q<=WAIT_TILE;
                WAIT_TILE:if(pixel_valid) begin
                    status_q<=pixel_error ? 8'd2:8'd0;payload_q<=16'd256;state_q<=RESPONSE;
                end
                RESPONSE:if(m_ready_i) begin
                    if(payload_q==0) begin state_q<=HEADER;abort_q<=0;end
                    else state_q<=PIXELS;
                end
                PIXELS:if(pixel_valid && m_ready_i && pixel_last) state_q<=HEADER;
                DRAIN:if(s_valid_i && s_last_i) state_q<=RESPONSE;
                default:begin state_q<=HEADER;abort_q<=1;end
            endcase
        end
    end
endmodule
