#include "xaxidma.h"
#include "xil_cache.h"
#include "xil_printf.h"
#include "xparameters.h"
#include "xstatus.h"
#include "xiltimer.h"
#include "../../include/posepriv_joint_metrics_packet.h"

#include <stdint.h>
#include <string.h>

#define BEAT_BYTES POSEPRIV_JOINT_AXIS_BEAT_BYTES
#define MAX_BEATS 4U
#define METRIC_BITS POSEPRIV_JOINT_METRIC_BITS
#define POLL_TIMEOUT 10000000U
#define STEADY_WARMUP_ITERATIONS 4U
#define STEADY_MEASURED_ITERATIONS 64U

typedef unsigned __int128 uint128_t;

typedef struct {
    int16_t ax;
    int16_t ay;
    int16_t bx;
    int16_t by;
    int16_t cx;
    int16_t cy;
    uint8_t joint_id;
    uint8_t view_id;
} joint_request_t;

static XAxiDma dma;
static uint128_t tx_buffer[MAX_BEATS] __attribute__((aligned(64)));
static uint128_t rx_buffer[MAX_BEATS] __attribute__((aligned(64)));
static uint128_t expected_buffer[MAX_BEATS] __attribute__((aligned(64)));

static const joint_request_t test_requests[MAX_BEATS] = {
    {3, 4, 0, 0, -4, 3, 0, 0},
    {-8, 5, 2, -1, 7, 11, 1, 0},
    {12, -3, 4, 5, -6, 9, 0, 1},
    {-11, -7, -2, 6, 8, -4, 1, 1},
};

static uint128_t metric_mask(void)
{
    return (((uint128_t)1U) << METRIC_BITS) - 1U;
}

static uint128_t pack_request(const joint_request_t *request)
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

static uint128_t reference_result(const joint_request_t *request)
{
    int64_t ab_x = (int32_t)request->ax - (int32_t)request->bx;
    int64_t ab_y = (int32_t)request->ay - (int32_t)request->by;
    int64_t cb_x = (int32_t)request->cx - (int32_t)request->bx;
    int64_t cb_y = (int32_t)request->cy - (int32_t)request->by;
    int64_t dot = ab_x * cb_x + ab_y * cb_y;
    uint64_t mag_ab = (uint64_t)(ab_x * ab_x + ab_y * ab_y);
    uint64_t mag_cb = (uint64_t)(cb_x * cb_x + cb_y * cb_y);
    uint128_t mask = metric_mask();
    uint128_t word = ((uint128_t)dot) & mask;

    word |= (((uint128_t)mag_ab) & mask) << POSEPRIV_JOINT_RESULT_MAG_AB_SHIFT;
    word |= (((uint128_t)mag_cb) & mask) << POSEPRIV_JOINT_RESULT_MAG_CB_SHIFT;
    word |= ((uint128_t)request->joint_id) << POSEPRIV_JOINT_RESULT_ID_SHIFT;
    word |= ((uint128_t)(request->view_id & 1U)) << POSEPRIV_JOINT_RESULT_VIEW_SHIFT;
    return word;
}

static int wait_for_dma(void)
{
    uint32_t remaining = POLL_TIMEOUT;

    while (remaining > 0U) {
        if (!XAxiDma_Busy(&dma, XAXIDMA_DEVICE_TO_DMA) &&
            !XAxiDma_Busy(&dma, XAXIDMA_DMA_TO_DEVICE)) {
            return XST_SUCCESS;
        }
        --remaining;
    }
    return XST_FAILURE;
}

static int execute_case(uint32_t beat_count, uint64_t *elapsed_ticks)
{
    uint32_t byte_count = beat_count * BEAT_BYTES;
    uint32_t index;
    XTime dma_start;
    XTime dma_end;
    int status;

    memset(rx_buffer, 0, sizeof(rx_buffer));
    for (index = 0U; index < beat_count; ++index) {
        tx_buffer[index] = pack_request(&test_requests[index]);
        expected_buffer[index] = reference_result(&test_requests[index]);
    }

    Xil_DCacheFlushRange((UINTPTR)tx_buffer, byte_count);
    Xil_DCacheFlushRange((UINTPTR)rx_buffer, byte_count);

    XTime_GetTime(&dma_start);
    /* S2MM must be ready before MM2S starts producing output. */
    status = XAxiDma_SimpleTransfer(&dma, (UINTPTR)rx_buffer, byte_count,
                                    XAXIDMA_DEVICE_TO_DMA);
    if (status != XST_SUCCESS) {
        xil_printf("S2MM start failed: %d\r\n", status);
        return XST_FAILURE;
    }

    status = XAxiDma_SimpleTransfer(&dma, (UINTPTR)tx_buffer, byte_count,
                                    XAXIDMA_DMA_TO_DEVICE);
    if (status != XST_SUCCESS) {
        xil_printf("MM2S start failed: %d\r\n", status);
        return XST_FAILURE;
    }

    if (wait_for_dma() != XST_SUCCESS) {
        xil_printf("DMA timeout for %lu bytes\r\n", (unsigned long)byte_count);
        return XST_FAILURE;
    }
    XTime_GetTime(&dma_end);
    *elapsed_ticks = (uint64_t)(dma_end - dma_start);

    Xil_DCacheInvalidateRange((UINTPTR)rx_buffer, byte_count);
    for (index = 0U; index < beat_count; ++index) {
        if (rx_buffer[index] != expected_buffer[index]) {
            xil_printf("Mismatch at beat %lu\r\n", (unsigned long)index);
            return XST_FAILURE;
        }
    }

    return XST_SUCCESS;
}

static int run_reported_case(uint32_t beat_count)
{
    uint64_t elapsed_ticks;

    if (execute_case(beat_count, &elapsed_ticks) != XST_SUCCESS) {
        return XST_FAILURE;
    }
    xil_printf("PASS: %lu-byte transaction, DMA ticks=%lu\r\n",
               (unsigned long)(beat_count * BEAT_BYTES),
               (unsigned long)elapsed_ticks);
    return XST_SUCCESS;
}

static int run_steady_case(uint32_t beat_count)
{
    uint32_t iteration;
    uint64_t elapsed_ticks;
    uint64_t minimum_ticks = UINT64_MAX;
    uint64_t maximum_ticks = 0U;
    uint64_t total_ticks = 0U;

    for (iteration = 0U; iteration < STEADY_WARMUP_ITERATIONS; ++iteration) {
        if (execute_case(beat_count, &elapsed_ticks) != XST_SUCCESS) {
            return XST_FAILURE;
        }
    }
    for (iteration = 0U; iteration < STEADY_MEASURED_ITERATIONS; ++iteration) {
        if (execute_case(beat_count, &elapsed_ticks) != XST_SUCCESS) {
            return XST_FAILURE;
        }
        if (elapsed_ticks < minimum_ticks) {
            minimum_ticks = elapsed_ticks;
        }
        if (elapsed_ticks > maximum_ticks) {
            maximum_ticks = elapsed_ticks;
        }
        total_ticks += elapsed_ticks;
    }

    xil_printf(
        "STEADY: %lu-byte transaction, iterations=%lu, "
        "min_ticks=%lu, total_ticks=%lu, max_ticks=%lu\r\n",
        (unsigned long)(beat_count * BEAT_BYTES),
        (unsigned long)STEADY_MEASURED_ITERATIONS,
        (unsigned long)minimum_ticks,
        (unsigned long)total_ticks,
        (unsigned long)maximum_ticks);
    return XST_SUCCESS;
}

int main(void)
{
    XAxiDma_Config *config;
    int status;

    xil_printf("\r\nPosePriv joint-metrics DMA self-test\r\n");
    xil_printf("Timer frequency: %lu Hz\r\n", (unsigned long)COUNTS_PER_SECOND);
#ifdef SDT
    config = XAxiDma_LookupConfig(XPAR_XAXIDMA_0_BASEADDR);
#else
    config = XAxiDma_LookupConfig(XPAR_AXIDMA_0_DEVICE_ID);
#endif
    if (config == NULL) {
        xil_printf("AXI DMA configuration not found\r\n");
        return XST_FAILURE;
    }

    status = XAxiDma_CfgInitialize(&dma, config);
    if (status != XST_SUCCESS || XAxiDma_HasSg(&dma)) {
        xil_printf("AXI DMA initialization failed\r\n");
        return XST_FAILURE;
    }

    XAxiDma_IntrDisable(&dma, XAXIDMA_IRQ_ALL_MASK, XAXIDMA_DEVICE_TO_DMA);
    XAxiDma_IntrDisable(&dma, XAXIDMA_IRQ_ALL_MASK, XAXIDMA_DMA_TO_DEVICE);

    if (run_reported_case(1U) != XST_SUCCESS ||
        run_reported_case(2U) != XST_SUCCESS ||
        run_reported_case(4U) != XST_SUCCESS ||
        run_steady_case(4U) != XST_SUCCESS) {
        xil_printf("POSEPRIV_DMA_TEST: FAIL\r\n");
        return XST_FAILURE;
    }

    xil_printf("POSEPRIV_DMA_TEST: PASS\r\n");
    return XST_SUCCESS;
}
