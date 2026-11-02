// v1 fixed 32x32 painter-order tile. Internal triangle markers are not video TLAST.
module avatar_tile_buffer (
    input logic clk_i,rst_i,
    input logic begin_valid_i,output logic begin_ready_o,
    input logic [11:0] origin_x_i,origin_y_i,input logic [23:0] background_i,
    output logic active_o,drawing_o,
    input logic frag_valid_i,output logic frag_ready_o,
    input logic [23:0] frag_rgb_i,input logic [31:0] frag_tag_i,
    input logic frag_last_i,frag_error_i,
    input logic finish_valid_i,output logic finish_ready_o,
    output logic out_valid_o,input logic out_ready_i,
    output logic [127:0] out_data_o,output logic out_last_o,out_error_o
);
    typedef enum logic [1:0] {IDLE,CLEAR,DRAW,DRAIN} state_t;
    state_t state_q;
    logic [11:0] ox_q,oy_q;
    logic [23:0] background_q;
    logic [7:0] clear_q,read_q;
    logic open_q,fault_q,bad_origin_q,issued_last_q;
    logic [15:0] fx,fy,dx,dy;
    logic inside_tile,frag_fire,write_pixel,issue_read;
    logic [7:0] write_addr;
    (* ram_style="block" *) logic [23:0] bank0[0:255],bank1[0:255],bank2[0:255],bank3[0:255];
    logic [23:0] read0,read1,read2,read3;
    assign begin_ready_o=!rst_i && state_q==IDLE;
    assign active_o=state_q!=IDLE;
    assign drawing_o=!rst_i && state_q==DRAW;
    assign finish_ready_o=drawing_o && !open_q;
    assign frag_ready_o=drawing_o && !(finish_valid_i && !open_q);
    assign frag_fire=frag_valid_i && frag_ready_o;
    assign fx=frag_tag_i[15:0];assign fy=frag_tag_i[31:16];
    assign dx=fx-{4'b0,ox_q};assign dy=fy-{4'b0,oy_q};
    assign inside_tile=!bad_origin_q && fx>={4'b0,ox_q} && fy>={4'b0,oy_q} && dx<16'd32 && dy<16'd32;
    assign write_pixel=frag_fire && !frag_last_i && !frag_error_i && inside_tile;
    assign write_addr={dy[4:0],dx[4:2]};
    assign issue_read=!rst_i && state_q==DRAIN && !issued_last_q && (!out_valid_o || out_ready_i);
    assign out_data_o={8'b0,read3,8'b0,read2,8'b0,read1,8'b0,read0};
    assign out_error_o=fault_q;

    // Independent parity banks: one write per fragment, four synchronous reads/beat.
    // No RAM reset; CLEAR makes every pixel valid before any output can be issued.
    always_ff @(posedge clk_i) begin
        if(!rst_i) begin
            if(state_q==CLEAR) begin
                bank0[clear_q]<=background_q;bank1[clear_q]<=background_q;
                bank2[clear_q]<=background_q;bank3[clear_q]<=background_q;
            end else if(write_pixel) begin
                case(dx[1:0])
                    2'd0:bank0[write_addr]<=frag_rgb_i;
                    2'd1:bank1[write_addr]<=frag_rgb_i;
                    2'd2:bank2[write_addr]<=frag_rgb_i;
                    2'd3:bank3[write_addr]<=frag_rgb_i;
                endcase
            end
            if(issue_read) begin
                read0<=bank0[read_q];read1<=bank1[read_q];
                read2<=bank2[read_q];read3<=bank3[read_q];
            end
        end
    end
    always_ff @(posedge clk_i) begin
        if(rst_i) begin
            state_q<=IDLE;out_valid_o<=0;open_q<=0;fault_q<=0;
        end else begin
            if(out_valid_o && out_ready_i) out_valid_o<=0;
            case(state_q)
                IDLE:if(begin_valid_i) begin
                    ox_q<=origin_x_i;oy_q<=origin_y_i;background_q<=background_i;
                    bad_origin_q<=origin_x_i>12'd4064 || origin_y_i>12'd4064;
                    fault_q<=origin_x_i>12'd4064 || origin_y_i>12'd4064;
                    clear_q<=0;open_q<=0;state_q<=CLEAR;
                end
                CLEAR:if(clear_q==8'd255) state_q<=DRAW;else clear_q<=clear_q+1'b1;
                DRAW:begin
                    if(frag_fire) begin
                        open_q<=!frag_last_i;
                        if(frag_error_i || (!frag_last_i && !inside_tile)) fault_q<=1;
                    end
                    if(finish_valid_i && finish_ready_o) begin
                        read_q<=0;issued_last_q<=0;state_q<=DRAIN;
                    end
                end
                DRAIN:begin
                    if(issue_read) begin
                        out_valid_o<=1;out_last_o<=read_q==8'd255;
                        if(read_q==8'd255) issued_last_q<=1;else read_q<=read_q+1'b1;
                    end
                    if(out_valid_o && out_ready_i && out_last_o) state_q<=IDLE;
                end
                default:state_q<=IDLE;
            endcase
        end
    end
endmodule
