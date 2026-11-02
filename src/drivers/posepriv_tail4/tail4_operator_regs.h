/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef POSEPRIV_TAIL4_OPERATOR_REGS_H
#define POSEPRIV_TAIL4_OPERATOR_REGS_H
/* AXI-Lite32 byte offsets; physical base supplied by the platform. */
#define TAIL4_OP_ID 0x54344f50u
#define TAIL4_OP_ABI_VERSION 0x00010000u
#define TAIL4_OP_REG_ID 0x00u
#define TAIL4_OP_REG_VERSION 0x04u
#define TAIL4_OP_REG_CONTROL 0x08u
#define TAIL4_OP_REG_STATUS 0x0cu
#define TAIL4_OP_REG_LAST_TAG 0x10u
#define TAIL4_OP_REG_CONFIG 0x14u
#define TAIL4_OP_REG_QPARAM 0x18u
#define TAIL4_OP_REG_RELU_LIMIT 0x1cu
#define TAIL4_OP_REG_JOB_TAG 0x20u
#define TAIL4_OP_REG_A_LO 0x30u
#define TAIL4_OP_REG_A_HI 0x34u
#define TAIL4_OP_REG_WB_LO 0x38u
#define TAIL4_OP_REG_WB_HI 0x3cu
#define TAIL4_OP_REG_P_LO 0x40u
#define TAIL4_OP_REG_P_HI 0x44u
#define TAIL4_OP_REG_DST_LO 0x48u
#define TAIL4_OP_REG_DST_HI 0x4cu
#define TAIL4_OP_START 1u
#define TAIL4_OP_CANCEL 2u
#define TAIL4_OP_ACK 4u
#define TAIL4_OP_BUSY 1u
#define TAIL4_OP_DONE 2u
#define TAIL4_OP_SUCCESS 4u
#define TAIL4_OP_ERROR 8u
#define TAIL4_OP_CANCELLED 16u
#define TAIL4_OP_IRQ 32u
#define TAIL4_OP_KIND_CONV 0u
#define TAIL4_OP_KIND_ADD 1u
#define TAIL4_OP_KIND_POOL 2u
#define TAIL4_OP_CONV_MODE_SHIFT 4u
#define TAIL4_OP_DIM4 (1u << 8)
#define TAIL4_OP_STRIDE2 (1u << 9)
#define TAIL4_OP_RELU (1u << 16)
#endif
