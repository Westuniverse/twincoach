#ifndef POSEPRIV_RECTIFIED_STEREO_PACKET_H
#define POSEPRIV_RECTIFIED_STEREO_PACKET_H

/*
 * A53/Linux <-> PL ABI for rectified stereo triangulation.
 *
 * The wire format is deliberately described as byte offsets and bit fields,
 * not as a packed C struct.  That keeps the contract independent of compiler
 * padding and CPU endianness.  The inline helpers below are suitable for a
 * small userspace shim or a standalone A53 test application.
 */
#include <stdint.h>

#define POSEPRIV_RECTIFIED_STEREO_PACKET_ABI_VERSION 1U
#define POSEPRIV_RECTIFIED_STEREO_AXIS_BEAT_BYTES 16U
#define POSEPRIV_RECTIFIED_STEREO_Q14_FRACTION_BITS 14U

#define POSEPRIV_RECTIFIED_STEREO_REQUEST_BYTES 16U
#define POSEPRIV_RECTIFIED_STEREO_RESULT_BYTES 16U
#define POSEPRIV_RECTIFIED_STEREO_COORDINATE_BITS 16U
#define POSEPRIV_RECTIFIED_STEREO_RESULT_COORDINATE_BITS 32U
#define POSEPRIV_RECTIFIED_STEREO_JOINT_ID_BITS 8U
#define POSEPRIV_RECTIFIED_STEREO_CONFIDENCE_BITS 8U
#define POSEPRIV_RECTIFIED_STEREO_DISPARITY_BITS 17U
#define POSEPRIV_RECTIFIED_STEREO_DISPARITY_REPORT_BITS 15U
#define POSEPRIV_RECTIFIED_STEREO_MIN_DISPARITY 64U
#define POSEPRIV_RECTIFIED_STEREO_MIN_CONFIDENCE 64U

#define POSEPRIV_RECTIFIED_STEREO_REQUEST_X_FRONT_SHIFT 0U
#define POSEPRIV_RECTIFIED_STEREO_REQUEST_Y_FRONT_SHIFT 16U
#define POSEPRIV_RECTIFIED_STEREO_REQUEST_X_SIDE_SHIFT 32U
#define POSEPRIV_RECTIFIED_STEREO_REQUEST_Y_SIDE_SHIFT 48U
#define POSEPRIV_RECTIFIED_STEREO_REQUEST_BASELINE_SHIFT 64U
#define POSEPRIV_RECTIFIED_STEREO_REQUEST_FOCAL_SHIFT 80U
#define POSEPRIV_RECTIFIED_STEREO_REQUEST_JOINT_ID_SHIFT 96U
#define POSEPRIV_RECTIFIED_STEREO_REQUEST_FRONT_CONFIDENCE_SHIFT 104U
#define POSEPRIV_RECTIFIED_STEREO_REQUEST_SIDE_CONFIDENCE_SHIFT 112U
#define POSEPRIV_RECTIFIED_STEREO_REQUEST_RESERVED_SHIFT 120U

#define POSEPRIV_RECTIFIED_STEREO_RESULT_X_SHIFT 0U
#define POSEPRIV_RECTIFIED_STEREO_RESULT_Y_SHIFT 32U
#define POSEPRIV_RECTIFIED_STEREO_RESULT_Z_SHIFT 64U
#define POSEPRIV_RECTIFIED_STEREO_RESULT_JOINT_ID_SHIFT 96U
#define POSEPRIV_RECTIFIED_STEREO_RESULT_CONFIDENCE_SHIFT 104U
#define POSEPRIV_RECTIFIED_STEREO_RESULT_VALID_SHIFT 112U
#define POSEPRIV_RECTIFIED_STEREO_RESULT_DISPARITY_SHIFT 113U

typedef struct {
    int32_t x;
    int32_t y;
    int32_t z;
    uint8_t joint_id;
    uint8_t confidence;
    uint8_t valid;
    uint16_t disparity_abs;
} posepriv_rectified_stereo_result_t;

static inline void posepriv_rectified_stereo_store_u16_le(
    uint8_t *dst, uint16_t value)
{
    dst[0] = (uint8_t)(value & 0xffU);
    dst[1] = (uint8_t)(value >> 8U);
}

static inline uint16_t posepriv_rectified_stereo_load_u16_le(
    const uint8_t *src)
{
    return (uint16_t)src[0] | ((uint16_t)src[1] << 8U);
}

static inline uint32_t posepriv_rectified_stereo_load_u32_le(
    const uint8_t *src)
{
    return (uint32_t)src[0] |
           ((uint32_t)src[1] << 8U) |
           ((uint32_t)src[2] << 16U) |
           ((uint32_t)src[3] << 24U);
}

static inline int32_t posepriv_rectified_stereo_bits_to_i32(uint32_t value)
{
    uint32_t magnitude;

    if ((value & 0x80000000U) == 0U) {
        return (int32_t)value;
    }
    magnitude = (~value) + 1U;
    if (magnitude == 0x80000000U) {
        return INT32_MIN;
    }
    return -(int32_t)magnitude;
}

/* Pack one request without relying on a host struct layout. */
static inline void posepriv_rectified_stereo_pack_request(
    uint8_t out[POSEPRIV_RECTIFIED_STEREO_REQUEST_BYTES],
    int16_t x_front,
    int16_t y_front,
    int16_t x_side,
    int16_t y_side,
    int16_t baseline,
    int16_t focal,
    uint8_t joint_id,
    uint8_t front_confidence,
    uint8_t side_confidence)
{
    posepriv_rectified_stereo_store_u16_le(out + 0U, (uint16_t)x_front);
    posepriv_rectified_stereo_store_u16_le(out + 2U, (uint16_t)y_front);
    posepriv_rectified_stereo_store_u16_le(out + 4U, (uint16_t)x_side);
    posepriv_rectified_stereo_store_u16_le(out + 6U, (uint16_t)y_side);
    posepriv_rectified_stereo_store_u16_le(out + 8U, (uint16_t)baseline);
    posepriv_rectified_stereo_store_u16_le(out + 10U, (uint16_t)focal);
    out[12] = joint_id;
    out[13] = front_confidence;
    out[14] = side_confidence;
    out[15] = 0U;
}

/* Decode one result beat returned by the PL core. */
static inline void posepriv_rectified_stereo_unpack_result(
    posepriv_rectified_stereo_result_t *result,
    const uint8_t in[POSEPRIV_RECTIFIED_STEREO_RESULT_BYTES])
{
    uint16_t disparity;

    result->x = posepriv_rectified_stereo_bits_to_i32(
        posepriv_rectified_stereo_load_u32_le(in + 0U));
    result->y = posepriv_rectified_stereo_bits_to_i32(
        posepriv_rectified_stereo_load_u32_le(in + 4U));
    result->z = posepriv_rectified_stereo_bits_to_i32(
        posepriv_rectified_stereo_load_u32_le(in + 8U));
    result->joint_id = in[12];
    result->confidence = in[13];
    result->valid = (uint8_t)(in[14] & 0x01U);
    disparity = (uint16_t)(in[14] >> 1U) | ((uint16_t)in[15] << 7U);
    result->disparity_abs = disparity;
}

#endif
