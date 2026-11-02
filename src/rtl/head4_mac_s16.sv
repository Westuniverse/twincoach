// SPDX-License-Identifier: GPL-2.0-only
// Four output PEs x eight shared signed16 terms; exactly32 multipliers.
// First clears each accumulator, last retires four signed48 sums.
module head4_mac_s16(
 input logic clk_i,rst_i,
 input logic s_valid_i,output logic s_ready_o,
 input logic[127:0] features_i,input logic[511:0] weights_i,
 input logic first_i,last_i,input logic[15:0] tag_i,
 output logic m_valid_o,input logic m_ready_i,
 output logic[191:0] sums_o,output logic[15:0] tag_o
);
 logic[3:0]valid_q,first_q,last_q;
 logic[15:0]tag_q[0:3];
 (* use_dsp="yes" *)logic signed[31:0]product_q[0:3][0:7];
 (* use_dsp="no" *)logic signed[32:0]sum2_q[0:3][0:3];
 (* use_dsp="no" *)logic signed[33:0]sum4_q[0:3][0:1];
 (* use_dsp="no" *)logic signed[34:0]sum8_q[0:3];
 (* use_dsp="no" *)logic signed[47:0]acc_q[0:3];
 logic signed[47:0]accumulated[0:3];
 wire advance=~m_valid_o|m_ready_i;
 assign s_ready_o=~rst_i&advance;
 for(genvar p=0;p<4;p=p+1)begin: pe
  assign accumulated[p]=first_q[3]?{{13{sum8_q[p][34]}},sum8_q[p]}:
                         acc_q[p]+{{13{sum8_q[p][34]}},sum8_q[p]};
  always_ff @(posedge clk_i)if(advance)begin
   for(integer k=0;k<8;k=k+1)
    product_q[p][k]<=$signed(features_i[k*16+:16])*$signed(weights_i[(p*8+k)*16+:16]);
   for(integer k=0;k<4;k=k+1)
    sum2_q[p][k]<={product_q[p][2*k][31],product_q[p][2*k]}+{product_q[p][2*k+1][31],product_q[p][2*k+1]};
   for(integer k=0;k<2;k=k+1)
    sum4_q[p][k]<={sum2_q[p][2*k][32],sum2_q[p][2*k]}+{sum2_q[p][2*k+1][32],sum2_q[p][2*k+1]};
   sum8_q[p]<={sum4_q[p][0][33],sum4_q[p][0]}+{sum4_q[p][1][33],sum4_q[p][1]};
   if(valid_q[3])begin acc_q[p]<=accumulated[p];if(last_q[3])sums_o[p*48+:48]<=accumulated[p];end
  end
 end
 always_ff @(posedge clk_i)begin
  if(rst_i)begin valid_q<=0;m_valid_o<=0;end
  else if(advance)begin
   valid_q<={valid_q[2:0],s_valid_i};
   first_q<={first_q[2:0],first_i};last_q<={last_q[2:0],last_i};
   tag_q[0]<=tag_i;for(integer k=1;k<4;k=k+1)tag_q[k]<=tag_q[k-1];
   m_valid_o<=valid_q[3]&last_q[3];if(valid_q[3]&last_q[3])tag_o<=tag_q[3];
  end
 end
endmodule
