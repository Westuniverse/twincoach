// SPDX-License-Identifier: GPL-2.0-only
// Four or eight physical lanes, full 16-lane tap-major streaming records.
// ADD0, half-pixel Resize1, MaxPool2; Q0..15; ties toward positive infinity.
// One holding record, five arithmetic stages, complete-record assembly.
// II=16/LANES without stalls; the entire pipeline stalls with a pending output.
module tensor_alu16_folded_s16 #(
 parameter integer LANES=4
)(
 input logic clk_i,rst_i,
 input logic cfg_valid_i,output logic cfg_ready_o,input logic[1:0] cfg_op_i,
 input logic[3:0] cfg_qa_i,cfg_qb_i,cfg_qout_i,
 input logic cfg_relu6_i,input logic[14:0] cfg_limit_i,output logic cfg_error_o,
 input logic s_valid_i,output logic s_ready_o,input logic[1023:0] taps_i,
 input logic[1:0] phase_i,input logic[15:0] tag_i,
 output logic m_valid_o,input logic m_ready_i,output logic[255:0] values_o,
 output logic[15:0] tag_o
);
 localparam integer CHUNKS=16/LANES;
 localparam integer CHUNK_BITS=$clog2(CHUNKS);
 logic configured_q,holding_valid_q;
 logic[1023:0] holding_taps_q;
 logic[1:0] holding_phase_q,op_q;
 logic[15:0] holding_tag_q;
 logic[CHUNK_BITS-1:0] issue_chunk_q,chunk_pipe_q[0:4];
 logic[3:0] add_left_a_q,add_left_b_q;
 logic[4:0] final_left_q,final_right_q;
 logic relu_q;logic[14:0] limit_q;
 logic[4:0] valid_q;
 logic[15:0] tag_pipe_q[0:4];
 logic y_odd_q;
 (* use_dsp="no" *)logic signed[31:0] pair_a_q[0:LANES-1],pair_b_q[0:LANES-1];
 (* use_dsp="no" *)logic signed[32:0] combined_q[0:LANES-1],aligned_q[0:LANES-1];
 (* use_dsp="no" *)logic signed[33:0] rounded_q[0:LANES-1],shifted_q[0:LANES-1];
 logic[4:0] raw_q,target_q;
 wire advance=~m_valid_o|m_ready_i;
 wire issue=holding_valid_q & advance;
 wire last_issue=(issue_chunk_q==CHUNK_BITS'(CHUNKS-1));
 wire accept=s_valid_i & s_ready_o;
 assign cfg_ready_o=~rst_i & ~holding_valid_q & ~(|valid_q) & ~m_valid_o;
 assign s_ready_o=~rst_i & configured_q & ~cfg_valid_i & advance &
                  (~holding_valid_q | last_issue);
 always_comb begin
  if(cfg_op_i==2'd0)raw_q=({1'b0,cfg_qa_i}>{1'b0,cfg_qb_i})?{1'b0,cfg_qa_i}:{1'b0,cfg_qb_i};
  else if(cfg_op_i==2'd1)raw_q={1'b0,cfg_qa_i}+5'd4;
  else raw_q={1'b0,cfg_qa_i};
  target_q=raw_q>{1'b0,cfg_qout_i}?raw_q:{1'b0,cfg_qout_i};
 end
 function automatic logic[15:0] clamp16(input logic signed[33:0] value);
  if(relu_q)begin
   if(value<0)clamp16=0;
   else if(value>$signed({19'd0,limit_q}))clamp16={1'b0,limit_q};
   else clamp16=value[15:0];
  end else begin
   if(value>34'sd32767)clamp16=16'h7fff;
   else if(value < -34'sd32768)clamp16=16'h8000;
   else clamp16=value[15:0];
  end
 endfunction
 for(genvar p=0;p<LANES;p=p+1)begin: lane
  wire signed[15:0] a=$signed(holding_taps_q[(0*16+int'(issue_chunk_q)*LANES+p)*16+:16]);
  wire signed[15:0] b=$signed(holding_taps_q[(1*16+int'(issue_chunk_q)*LANES+p)*16+:16]);
  wire signed[15:0] c=$signed(holding_taps_q[(2*16+int'(issue_chunk_q)*LANES+p)*16+:16]);
  wire signed[15:0] d=$signed(holding_taps_q[(3*16+int'(issue_chunk_q)*LANES+p)*16+:16]);
  wire signed[31:0] a32={{16{a[15]}},a},b32={{16{b[15]}},b};
  wire signed[31:0] c32={{16{c[15]}},c},d32={{16{d[15]}},d};
  wire signed[32:0] pair_a33={pair_a_q[p][31],pair_a_q[p]};
  wire signed[32:0] pair_b33={pair_b_q[p][31],pair_b_q[p]};
  wire signed[33:0] increment=(final_right_q==0)?34'sd0:$signed(34'd1<<(final_right_q-1'b1));
  always_ff @(posedge clk_i)if(advance)begin
   if(issue)case(op_q)
    2'd0:begin pair_a_q[p]<=a32<<<add_left_a_q;pair_b_q[p]<=b32<<<add_left_b_q;end
    2'd1:begin
     pair_a_q[p]<=holding_phase_q[0]?(a32+(a32<<<1)+b32):(a32+b32+(b32<<<1));
     pair_b_q[p]<=holding_phase_q[0]?(c32+(c32<<<1)+d32):(c32+d32+(d32<<<1));
    end
    default:begin pair_a_q[p]<=a>b?a32:b32;pair_b_q[p]<=c>d?c32:d32;end
   endcase
   if(valid_q[0])case(op_q)
    2'd0:combined_q[p]<=pair_a33+pair_b33;
    2'd1:combined_q[p]<=y_odd_q?(pair_a33+(pair_a33<<<1)+pair_b33):(pair_a33+pair_b33+(pair_b33<<<1));
    default:combined_q[p]<=pair_a33>pair_b33?pair_a33:pair_b33;
   endcase
   if(valid_q[1])aligned_q[p]<=combined_q[p]<<<final_left_q;
   if(valid_q[2])rounded_q[p]<={aligned_q[p][32],aligned_q[p]}+increment;
   if(valid_q[3])shifted_q[p]<=rounded_q[p]>>>final_right_q;
   if(valid_q[4])values_o[(int'(chunk_pipe_q[4])*LANES+p)*16+:16]<=clamp16(shifted_q[p]);
  end
 end
 always_ff @(posedge clk_i)begin
  if(rst_i)begin
   configured_q<=0;holding_valid_q<=0;valid_q<=0;
   m_valid_o<=0;cfg_error_o<=0;issue_chunk_q<=0;
  end else begin
   cfg_error_o<=0;
   if(cfg_valid_i & cfg_ready_o)begin
    if(cfg_op_i==2'd3)begin configured_q<=0;cfg_error_o<=1;end
    else begin
     configured_q<=1;op_q<=cfg_op_i;relu_q<=cfg_relu6_i;limit_q<=cfg_limit_i;
     add_left_a_q<=4'(target_q-{1'b0,cfg_qa_i});add_left_b_q<=4'(target_q-{1'b0,cfg_qb_i});
     final_left_q<=(cfg_op_i==0)?5'd0:(target_q-raw_q);
     final_right_q<=target_q-{1'b0,cfg_qout_i};
    end
   end
   if(advance)begin
    if(issue)begin
     if(last_issue)holding_valid_q<=0;
     else issue_chunk_q<=issue_chunk_q+1'b1;
    end
    if(accept)begin
     holding_valid_q<=1;holding_taps_q<=taps_i;holding_phase_q<=phase_i;
     holding_tag_q<=tag_i;issue_chunk_q<=0;
    end
    valid_q<={valid_q[3:0],issue};
    if(issue)begin
     tag_pipe_q[0]<=holding_tag_q;chunk_pipe_q[0]<=issue_chunk_q;
     y_odd_q<=holding_phase_q[1];
    end
    for(integer k=1;k<5;k=k+1)if(valid_q[k-1])begin
     tag_pipe_q[k]<=tag_pipe_q[k-1];chunk_pipe_q[k]<=chunk_pipe_q[k-1];
    end
    m_valid_o<=valid_q[4] & (chunk_pipe_q[4]==CHUNK_BITS'(CHUNKS-1));
    if(valid_q[4] & (chunk_pipe_q[4]==CHUNK_BITS'(CHUNKS-1)))tag_o<=tag_pipe_q[4];
   end
  end
 end
endmodule
