#include "xaxidma.h"
#include "xil_cache.h"
#include "xil_printf.h"
#include "xparameters.h"
#include "xstatus.h"
#include "xiltimer.h"
#include "posepriv_avatar_packet.h"
#include "posepriv_joint_metrics_packet.h"
#include "posepriv_rectified_stereo_packet.h"
#include "posepriv_motion_stats_packet.h"

#include <stdint.h>
#include <string.h>

typedef unsigned __int128 uint128_t;

#define POLL_TIMEOUT 10000000U
#define AV_BEAT_BYTES POSEPRIV_AVATAR_AXIS_BEAT_BYTES
#define AV_MATRIX_BEATS POSEPRIV_AVATAR_MATRIX_PREAMBLE_BEATS
#define AV_VERTEX_COUNT 2U
#define AV_TX_BEATS (AV_MATRIX_BEATS + AV_VERTEX_COUNT)
#define JOINT_BEAT_BYTES POSEPRIV_JOINT_AXIS_BEAT_BYTES
#define JOINT_COUNT 2U
#define RECT_BEAT_BYTES POSEPRIV_RECTIFIED_STEREO_AXIS_BEAT_BYTES
#define RECT_COUNT 2U
#define MOTION_BEAT_BYTES POSEPRIV_MOTION_STATS_AXIS_BEAT_BYTES
#define MOTION_INPUT_BEATS 3U
#define MOTION_OUTPUT_BEATS 1U

static XAxiDma dma_avatar;
static XAxiDma dma_joint;
static XAxiDma dma_rectified;
static XAxiDma dma_motion;
static uint128_t avatar_tx[AV_TX_BEATS] __attribute__((aligned(64)));
static uint128_t avatar_rx[AV_VERTEX_COUNT] __attribute__((aligned(64)));
static uint128_t joint_tx[JOINT_COUNT] __attribute__((aligned(64)));
static uint128_t joint_rx[JOINT_COUNT] __attribute__((aligned(64)));
static uint8_t rect_tx[RECT_COUNT * RECT_BEAT_BYTES] __attribute__((aligned(64)));
static uint8_t rect_rx[RECT_COUNT * RECT_BEAT_BYTES] __attribute__((aligned(64)));
static uint128_t motion_tx[MOTION_INPUT_BEATS] __attribute__((aligned(64)));
static uint128_t motion_rx[MOTION_OUTPUT_BEATS] __attribute__((aligned(64)));

static XAxiDma_Config *lookup_dma_config(unsigned index)
{
#ifdef SDT
    switch (index) {
    case 0U:
#ifdef XPAR_AXI_DMA_AVATAR_BASEADDR
        return XAxiDma_LookupConfig(XPAR_AXI_DMA_AVATAR_BASEADDR);
#endif
        break;
    case 1U:
#ifdef XPAR_AXI_DMA_JOINT_BASEADDR
        return XAxiDma_LookupConfig(XPAR_AXI_DMA_JOINT_BASEADDR);
#endif
        break;
    case 2U:
#ifdef XPAR_AXI_DMA_RECTIFIED_BASEADDR
        return XAxiDma_LookupConfig(XPAR_AXI_DMA_RECTIFIED_BASEADDR);
#endif
        break;
    case 3U:
#ifdef XPAR_AXI_DMA_MOTION_STATS_BASEADDR
        return XAxiDma_LookupConfig(XPAR_AXI_DMA_MOTION_STATS_BASEADDR);
#endif
        break;
    default:
        break;
    }
#else
    switch (index) {
    case 0U:
#ifdef XPAR_AXI_DMA_AVATAR_DEVICE_ID
        return XAxiDma_LookupConfig(XPAR_AXI_DMA_AVATAR_DEVICE_ID);
#elif defined(XPAR_AXIDMA_0_DEVICE_ID)
        return XAxiDma_LookupConfig(XPAR_AXIDMA_0_DEVICE_ID);
#endif
        break;
    case 1U:
#ifdef XPAR_AXI_DMA_JOINT_DEVICE_ID
        return XAxiDma_LookupConfig(XPAR_AXI_DMA_JOINT_DEVICE_ID);
#elif defined(XPAR_AXIDMA_1_DEVICE_ID)
        return XAxiDma_LookupConfig(XPAR_AXIDMA_1_DEVICE_ID);
#endif
        break;
    case 2U:
#ifdef XPAR_AXI_DMA_RECTIFIED_DEVICE_ID
        return XAxiDma_LookupConfig(XPAR_AXI_DMA_RECTIFIED_DEVICE_ID);
#elif defined(XPAR_AXIDMA_2_DEVICE_ID)
        return XAxiDma_LookupConfig(XPAR_AXIDMA_2_DEVICE_ID);
#endif
        break;
    case 3U:
#ifdef XPAR_AXI_DMA_MOTION_STATS_DEVICE_ID
        return XAxiDma_LookupConfig(XPAR_AXI_DMA_MOTION_STATS_DEVICE_ID);
#elif defined(XPAR_AXIDMA_3_DEVICE_ID)
        return XAxiDma_LookupConfig(XPAR_AXIDMA_3_DEVICE_ID);
#endif
        break;
    default:
        break;
    }
#endif
    return NULL;
}

static int init_dma(XAxiDma *dma, unsigned index)
{
    XAxiDma_Config *config = lookup_dma_config(index);
    int status;

    if (config == NULL) {
        return XST_FAILURE;
    }
    status = XAxiDma_CfgInitialize(dma, config);
    if (status != XST_SUCCESS || XAxiDma_HasSg(dma)) {
        return XST_FAILURE;
    }
    XAxiDma_IntrDisable(dma, XAXIDMA_IRQ_ALL_MASK, XAXIDMA_DEVICE_TO_DMA);
    XAxiDma_IntrDisable(dma, XAXIDMA_IRQ_ALL_MASK, XAXIDMA_DMA_TO_DEVICE);
    return XST_SUCCESS;
}

static int wait_for_dma(XAxiDma *dma)
{
    uint32_t remaining = POLL_TIMEOUT;

    while (remaining > 0U) {
        if (!XAxiDma_Busy(dma, XAXIDMA_DEVICE_TO_DMA) &&
            !XAxiDma_Busy(dma, XAXIDMA_DMA_TO_DEVICE)) {
            return XST_SUCCESS;
        }
        --remaining;
    }
    return XST_FAILURE;
}

static int run_dma(XAxiDma *dma, void *tx, uint32_t tx_bytes,
                   void *rx, uint32_t rx_bytes, uint64_t *ticks)
{
    XTime start;
    XTime end;
    int status;

    Xil_DCacheFlushRange((UINTPTR)tx, tx_bytes);
    Xil_DCacheFlushRange((UINTPTR)rx, rx_bytes);
    XTime_GetTime(&start);
    status = XAxiDma_SimpleTransfer(
        dma, (UINTPTR)rx, rx_bytes, XAXIDMA_DEVICE_TO_DMA);
    if (status != XST_SUCCESS) {
        return XST_FAILURE;
    }
    status = XAxiDma_SimpleTransfer(
        dma, (UINTPTR)tx, tx_bytes, XAXIDMA_DMA_TO_DEVICE);
    if (status != XST_SUCCESS || wait_for_dma(dma) != XST_SUCCESS) {
        return XST_FAILURE;
    }
    XTime_GetTime(&end);
    *ticks = (uint64_t)(end - start);
    Xil_DCacheInvalidateRange((UINTPTR)rx, rx_bytes);
    return XST_SUCCESS;
}

static uint128_t pack_matrix_beat(const int16_t *values, uint32_t first)
{
    uint128_t word = 0U;
    uint32_t count = first == 0U ? 8U : 4U;
    uint32_t index;

    for (index = 0U; index < count; ++index) {
        word |= ((uint128_t)(uint16_t)values[first + index]) << (index * 16U);
    }
    return word;
}

static uint128_t pack_avatar_vertex(int16_t x, int16_t y, int16_t z)
{
    uint128_t word = 0U;
    word |= ((uint128_t)(uint16_t)x) << 0U;
    word |= ((uint128_t)(uint16_t)y) << 16U;
    word |= ((uint128_t)(uint16_t)z) << 32U;
    word |= ((uint128_t)POSEPRIV_AVATAR_WEIGHT_Q15_ONE) << 64U;
    return word;
}

static void print_u128(const char *label, uint128_t value)
{
    xil_printf(
        "%s=0x%08lx%08lx%08lx%08lx\r\n",
        label,
        (unsigned long)(uint32_t)(value >> 96U),
        (unsigned long)(uint32_t)(value >> 64U),
        (unsigned long)(uint32_t)(value >> 32U),
        (unsigned long)(uint32_t)value);
}

static int run_avatar(uint64_t *ticks)
{
    static const int16_t identity[12] = {
        POSEPRIV_AVATAR_Q14_ONE, 0, 0, 0,
        0, POSEPRIV_AVATAR_Q14_ONE, 0, 0,
        0, 0, POSEPRIV_AVATAR_Q14_ONE, 0,
    };
    uint32_t matrix;
    uint32_t vertex;

    memset(avatar_tx, 0, sizeof(avatar_tx));
    memset(avatar_rx, 0, sizeof(avatar_rx));
    for (matrix = 0U; matrix < POSEPRIV_AVATAR_MATRIX_COUNT; ++matrix) {
        avatar_tx[matrix * 2U] = pack_matrix_beat(identity, 0U);
        avatar_tx[matrix * 2U + 1U] = pack_matrix_beat(identity, 8U);
    }
    avatar_tx[AV_MATRIX_BEATS] = pack_avatar_vertex(0, 0, 0);
    avatar_tx[AV_MATRIX_BEATS + 1U] = pack_avatar_vertex(4096, 0, 0);
    if (run_dma(&dma_avatar, avatar_tx, sizeof(avatar_tx),
                avatar_rx, sizeof(avatar_rx), ticks) != XST_SUCCESS) {
        return XST_FAILURE;
    }
    for (vertex = 0U; vertex < AV_VERTEX_COUNT; ++vertex) {
        uint128_t expected = vertex == 0U ? 0U : ((uint128_t)4096U);
        if (avatar_rx[vertex] != expected || (avatar_rx[vertex] >> 64U) != 0U) {
            xil_printf("AVATAR mismatch vertex=%lu\r\n", (unsigned long)vertex);
            print_u128("  expected", expected);
            print_u128("  actual", avatar_rx[vertex]);
            return XST_FAILURE;
        }
    }
    return XST_SUCCESS;
}

typedef struct {
    int16_t ax, ay, bx, by, cx, cy;
    uint8_t joint_id, view_id;
} joint_request_t;

static const joint_request_t joint_requests[JOINT_COUNT] = {
    {3, 4, 0, 0, -4, 3, 2, 0},
    {-8, 5, 2, -1, 7, 11, 3, 1},
};

static uint128_t joint_mask(void)
{
    return (((uint128_t)1U) << POSEPRIV_JOINT_METRIC_BITS) - 1U;
}

static uint128_t pack_joint(const joint_request_t *request)
{
    uint128_t word = 0U;
    word |= ((uint128_t)(uint16_t)request->ax) << POSEPRIV_JOINT_REQUEST_AX_SHIFT;
    word |= ((uint128_t)(uint16_t)request->ay) << POSEPRIV_JOINT_REQUEST_AY_SHIFT;
    word |= ((uint128_t)(uint16_t)request->bx) << POSEPRIV_JOINT_REQUEST_BX_SHIFT;
    word |= ((uint128_t)(uint16_t)request->by) << POSEPRIV_JOINT_REQUEST_BY_SHIFT;
    word |= ((uint128_t)(uint16_t)request->cx) << POSEPRIV_JOINT_REQUEST_CX_SHIFT;
    word |= ((uint128_t)(uint16_t)request->cy) << POSEPRIV_JOINT_REQUEST_CY_SHIFT;
    word |= ((uint128_t)request->joint_id) << POSEPRIV_JOINT_REQUEST_ID_SHIFT;
    word |= ((uint128_t)(request->view_id & 1U)) << POSEPRIV_JOINT_REQUEST_VIEW_SHIFT;
    return word;
}

static uint128_t reference_joint(const joint_request_t *request)
{
    int64_t ab_x = (int32_t)request->ax - (int32_t)request->bx;
    int64_t ab_y = (int32_t)request->ay - (int32_t)request->by;
    int64_t cb_x = (int32_t)request->cx - (int32_t)request->bx;
    int64_t cb_y = (int32_t)request->cy - (int32_t)request->by;
    uint128_t word = ((uint128_t)(ab_x * cb_x + ab_y * cb_y)) & joint_mask();
    word |= ((uint128_t)(ab_x * ab_x + ab_y * ab_y) & joint_mask())
        << POSEPRIV_JOINT_RESULT_MAG_AB_SHIFT;
    word |= ((uint128_t)(cb_x * cb_x + cb_y * cb_y) & joint_mask())
        << POSEPRIV_JOINT_RESULT_MAG_CB_SHIFT;
    word |= ((uint128_t)request->joint_id) << POSEPRIV_JOINT_RESULT_ID_SHIFT;
    word |= ((uint128_t)(request->view_id & 1U)) << POSEPRIV_JOINT_RESULT_VIEW_SHIFT;
    return word;
}

static int run_joint(uint64_t *ticks)
{
    uint32_t index;

    memset(joint_tx, 0, sizeof(joint_tx));
    memset(joint_rx, 0, sizeof(joint_rx));
    for (index = 0U; index < JOINT_COUNT; ++index) {
        joint_tx[index] = pack_joint(&joint_requests[index]);
    }
    if (run_dma(&dma_joint, joint_tx, sizeof(joint_tx),
                joint_rx, sizeof(joint_rx), ticks) != XST_SUCCESS) {
        return XST_FAILURE;
    }
    for (index = 0U; index < JOINT_COUNT; ++index) {
        if (joint_rx[index] != reference_joint(&joint_requests[index])) {
            return XST_FAILURE;
        }
    }
    return XST_SUCCESS;
}

static int run_rectified(uint64_t *ticks)
{
    posepriv_rectified_stereo_result_t actual;
    uint8_t expected[RECT_COUNT][RECT_BEAT_BYTES];
    uint32_t index;

    memset(rect_tx, 0, sizeof(rect_tx));
    memset(rect_rx, 0, sizeof(rect_rx));
    posepriv_rectified_stereo_pack_request(rect_tx, 819, 410, -819, 410,
        3277, 16384, 7, 240, 230);
    posepriv_rectified_stereo_pack_request(rect_tx + RECT_BEAT_BYTES, 100, 200,
        100, 200, 3277, 16384, 8, 255, 255);
    memset(expected, 0, sizeof(expected));
    expected[1][12] = 8U;
    expected[1][13] = 255U;
    if (run_dma(&dma_rectified, rect_tx, sizeof(rect_tx),
                rect_rx, sizeof(rect_rx), ticks) != XST_SUCCESS) {
        return XST_FAILURE;
    }
    posepriv_rectified_stereo_unpack_result(&actual, rect_rx);
    if (actual.x != 1638 || actual.y != 820 || actual.z != 32778 ||
        actual.joint_id != 7U || actual.confidence != 230U || actual.valid != 1U) {
        return XST_FAILURE;
    }
    for (index = 0U; index < RECT_BEAT_BYTES; ++index) {
        if (rect_rx[RECT_BEAT_BYTES + index] != expected[1][index]) {
            return XST_FAILURE;
        }
    }
    return XST_SUCCESS;
}

static uint128_t motion_input(uint32_t acceleration_sq, uint8_t joint_id,
                              uint8_t view_id)
{
    uint128_t word = 0U;
    word |= ((uint128_t)acceleration_sq &
             ((((uint128_t)1U) << POSEPRIV_MOTION_STATS_METRIC_BITS) - 1U))
            << POSEPRIV_MOTION_STATS_INPUT_ACCEL_SHIFT;
    word |= ((uint128_t)joint_id) << POSEPRIV_MOTION_STATS_INPUT_JOINT_SHIFT;
    word |= ((uint128_t)(view_id & 1U)) << POSEPRIV_MOTION_STATS_INPUT_VIEW_SHIFT;
    return word;
}

static uint128_t motion_expected(void)
{
    uint128_t word = 0U;
    word |= ((uint128_t)42U) << POSEPRIV_MOTION_STATS_OUTPUT_SUM_SHIFT;
    word |= ((uint128_t)25U) << POSEPRIV_MOTION_STATS_OUTPUT_MAX_SHIFT;
    word |= ((uint128_t)3U) << POSEPRIV_MOTION_STATS_OUTPUT_COUNT_SHIFT;
    word |= ((uint128_t)5U) << POSEPRIV_MOTION_STATS_OUTPUT_JOINT_SHIFT;
    word |= ((uint128_t)1U) << POSEPRIV_MOTION_STATS_OUTPUT_VIEW_SHIFT;
    return word;
}

static int run_motion(uint64_t *ticks)
{
    memset(motion_tx, 0, sizeof(motion_tx));
    memset(motion_rx, 0, sizeof(motion_rx));
    motion_tx[0] = motion_input(10U, 5U, 1U);
    motion_tx[1] = motion_input(25U, 5U, 1U);
    motion_tx[2] = motion_input(7U, 5U, 1U);
    if (run_dma(&dma_motion, motion_tx, sizeof(motion_tx),
                motion_rx, sizeof(motion_rx), ticks) != XST_SUCCESS) {
        return XST_FAILURE;
    }
    return motion_rx[0] == motion_expected() ? XST_SUCCESS : XST_FAILURE;
}

int main(void)
{
    uint64_t avatar_ticks, joint_ticks, rectified_ticks, motion_ticks;

    xil_printf("\r\nPosePriv quad PL accelerator self-test\r\n");
    xil_printf("Timer frequency: %lu Hz\r\n", (unsigned long)COUNTS_PER_SECOND);
    if (init_dma(&dma_avatar, 0U) != XST_SUCCESS ||
        init_dma(&dma_joint, 1U) != XST_SUCCESS ||
        init_dma(&dma_rectified, 2U) != XST_SUCCESS ||
        init_dma(&dma_motion, 3U) != XST_SUCCESS) {
        xil_printf("Quad AXI DMA initialization failed\r\n");
        xil_printf("POSEPRIV_QUAD_ACCEL_TEST: FAIL\r\n");
        return XST_FAILURE;
    }
    if (run_avatar(&avatar_ticks) != XST_SUCCESS) {
        xil_printf("AVATAR PL check failed\r\n");
        xil_printf("POSEPRIV_QUAD_ACCEL_TEST: FAIL\r\n");
        return XST_FAILURE;
    }
    xil_printf("AVATAR_PL: PASS ticks=%lu\r\n", (unsigned long)avatar_ticks);
    if (run_joint(&joint_ticks) != XST_SUCCESS) {
        xil_printf("JOINT PL check failed\r\n");
        xil_printf("POSEPRIV_QUAD_ACCEL_TEST: FAIL\r\n");
        return XST_FAILURE;
    }
    xil_printf("JOINT_PL: PASS ticks=%lu\r\n", (unsigned long)joint_ticks);
    if (run_rectified(&rectified_ticks) != XST_SUCCESS) {
        xil_printf("RECTIFIED PL check failed\r\n");
        xil_printf("POSEPRIV_QUAD_ACCEL_TEST: FAIL\r\n");
        return XST_FAILURE;
    }
    xil_printf("RECTIFIED_PL: PASS ticks=%lu\r\n", (unsigned long)rectified_ticks);
    if (run_motion(&motion_ticks) != XST_SUCCESS) {
        xil_printf("MOTION STATS PL check failed\r\n");
        xil_printf("POSEPRIV_QUAD_ACCEL_TEST: FAIL\r\n");
        return XST_FAILURE;
    }
    xil_printf("MOTION_STATS_PL: PASS ticks=%lu\r\n", (unsigned long)motion_ticks);
    xil_printf("POSEPRIV_QUAD_ACCEL_TEST: PASS\r\n");
    return XST_SUCCESS;
}
