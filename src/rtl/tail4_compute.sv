// SPDX-License-Identifier: GPL-2.0-only
// Frozen public4PE/32DSP core plus a layer-level optional signed16 Relu6 clamp.
// No added owner/latency: raw/post/tag use the original core's valid/ready.
module tail4_compute(
 input logic clk_i,rst_i,
 input logic cfg_valid_i,output logic cfg_ready_o,input logic[7:0]cfg_groups_i,
 input logic relu_i,input logic[14:0]limit_s16_i,
 output logic cfg_error_o,
 input logic w_valid_i,output logic w_ready_o,input logic[127:0]w_data_i,
 input logic p_valid_i,output logic p_ready_o,input logic[1:0]p_pe_i,
 input logic signed[47:0]p_bias_i,input logic[4:0]p_shift_i,
 input logic s_valid_i,output logic s_ready_o,input logic[127:0]features_i,input logic[15:0]tag_i,
 output logic m_valid_o,input logic m_ready_i,
 output logic[191:0]raw_o,output logic[63:0]values_o,output logic[15:0]tag_o
);
 logic relu_q;
 logic[14:0]limit_q;
 wire[63:0]linear_values;
 full33_head4_s16 core(
  .clk_i(clk_i),.rst_i(rst_i),.cfg_valid_i(cfg_valid_i),.cfg_ready_o(cfg_ready_o),.cfg_groups_i(cfg_groups_i),.cfg_error_o(cfg_error_o),
  .w_valid_i(w_valid_i),.w_ready_o(w_ready_o),.w_data_i(w_data_i),
  .p_valid_i(p_valid_i),.p_ready_o(p_ready_o),.p_pe_i(p_pe_i),.p_bias_i(p_bias_i),.p_shift_i(p_shift_i),
  .s_valid_i(s_valid_i),.s_ready_o(s_ready_o),.features_i(features_i),.tag_i(tag_i),
  .m_valid_o(m_valid_o),.m_ready_i(m_ready_i),.raw_o(raw_o),.values_o(linear_values),.tag_o(tag_o));
 // Limit is invalid until a cold header; do not fan reset into this datapath.
 always_ff @(posedge clk_i)begin
  if(rst_i)relu_q<=1'b0;
  else if(cfg_valid_i&&cfg_ready_o)begin relu_q<=relu_i;limit_q<=limit_s16_i;end
 end
 for(genvar p=0;p<4;p=p+1)begin: clamp_lane
  wire[15:0]v=linear_values[p*16+:16];
  assign values_o[p*16+:16]=!relu_q?v:v[15]?16'd0:(v>{1'b0,limit_q}?{1'b0,limit_q}:v);
 end
endmodule
