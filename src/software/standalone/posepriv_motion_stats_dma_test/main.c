#include "xaxidma.h"
#include "xil_cache.h"
#include "xil_printf.h"
#include "xparameters.h"
#include "xstatus.h"
#include "xiltimer.h"
#include "posepriv_motion_stats_packet.h"

#include <stdint.h>
#include <string.h>

#define BEAT_BYTES POSEPRIV_MOTION_STATS_AXIS_BEAT_BYTES
#define INPUT_BEATS 3U
#define OUTPUT_BEATS 1U
#define POLL_TIMEOUT 10000000U

typedef unsigned __int128 uint128_t;

static XAxiDma dma;
static uint128_t tx_buffer[INPUT_BEATS] __attribute__((aligned(64)));
static uint128_t rx_buffer[OUTPUT_BEATS] __attribute__((aligned(64)));

static uint128_t mask_bits(unsigned width)
{
    return (((uint128_t)1U) << width) - 1U;
}

static uint128_t pack_input(uint32_t acceleration_sq, uint8_t joint_id,
                            uint8_t view_id, int last)
{
    uint128_t word = 0U;

    (void)last;
    word |= ((uint128_t)acceleration_sq &
             mask_bits(POSEPRIV_MOTION_STATS_METRIC_BITS))
            << POSEPRIV_MOTION_STATS_INPUT_ACCEL_SHIFT;
    word |= ((uint128_t)joint_id) << POSEPRIV_MOTION_STATS_INPUT_JOINT_SHIFT;
    word |= ((uint128_t)(view_id & 1U)) << POSEPRIV_MOTION_STATS_INPUT_VIEW_SHIFT;
    return word;
}

static uint128_t expected_summary(void)
{
    uint128_t word = 0U;
    const uint32_t sum = 10U + 25U + 7U;
    const uint32_t maximum = 25U;
    const uint32_t count = INPUT_BEATS;
    const uint32_t joint = 5U;
    const uint32_t view = 1U;

    word |= ((uint128_t)sum) << POSEPRIV_MOTION_STATS_OUTPUT_SUM_SHIFT;
    word |= ((uint128_t)maximum) << POSEPRIV_MOTION_STATS_OUTPUT_MAX_SHIFT;
    word |= ((uint128_t)count) << POSEPRIV_MOTION_STATS_OUTPUT_COUNT_SHIFT;
    word |= ((uint128_t)joint) << POSEPRIV_MOTION_STATS_OUTPUT_JOINT_SHIFT;
    word |= ((uint128_t)view) << POSEPRIV_MOTION_STATS_OUTPUT_VIEW_SHIFT;
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

static int run_case(uint64_t *elapsed_ticks)
{
    const uint32_t tx_bytes = INPUT_BEATS * BEAT_BYTES;
    const uint32_t rx_bytes = OUTPUT_BEATS * BEAT_BYTES;
    XTime dma_start;
    XTime dma_end;
    int status;

    memset(tx_buffer, 0, sizeof(tx_buffer));
    memset(rx_buffer, 0, sizeof(rx_buffer));
    tx_buffer[0] = pack_input(10U, 5U, 1U, 0);
    tx_buffer[1] = pack_input(25U, 5U, 1U, 0);
    tx_buffer[2] = pack_input(7U, 5U, 1U, 1);

    Xil_DCacheFlushRange((UINTPTR)tx_buffer, tx_bytes);
    Xil_DCacheFlushRange((UINTPTR)rx_buffer, rx_bytes);
    XTime_GetTime(&dma_start);

    /* S2MM must be ready before MM2S emits the first beat. */
    status = XAxiDma_SimpleTransfer(&dma, (UINTPTR)rx_buffer, rx_bytes,
                                    XAXIDMA_DEVICE_TO_DMA);
    if (status != XST_SUCCESS) {
        xil_printf("S2MM start failed: %d\r\n", status);
        return XST_FAILURE;
    }
    status = XAxiDma_SimpleTransfer(&dma, (UINTPTR)tx_buffer, tx_bytes,
                                    XAXIDMA_DMA_TO_DEVICE);
    if (status != XST_SUCCESS) {
        xil_printf("MM2S start failed: %d\r\n", status);
        return XST_FAILURE;
    }
    if (wait_for_dma() != XST_SUCCESS) {
        xil_printf("DMA timeout\r\n");
        return XST_FAILURE;
    }
    XTime_GetTime(&dma_end);
    *elapsed_ticks = (uint64_t)(dma_end - dma_start);

    Xil_DCacheInvalidateRange((UINTPTR)rx_buffer, rx_bytes);
    if (rx_buffer[0] != expected_summary()) {
        xil_printf("Summary mismatch\r\n");
        return XST_FAILURE;
    }
    return XST_SUCCESS;
}

int main(void)
{
    XAxiDma_Config *config;
    uint64_t elapsed_ticks;
    int status;

    xil_printf("\r\nPosePriv temporal-motion-stats DMA self-test\r\n");
    xil_printf("Timer frequency: %lu Hz\r\n", (unsigned long)COUNTS_PER_SECOND);

#ifdef SDT
#if defined(XPAR_XAXIDMA_MOTION_STATS_BASEADDR)
    config = XAxiDma_LookupConfig(XPAR_XAXIDMA_MOTION_STATS_BASEADDR);
#elif defined(XPAR_XAXIDMA_0_BASEADDR)
    config = XAxiDma_LookupConfig(XPAR_XAXIDMA_0_BASEADDR);
#else
    config = NULL;
#endif
#else
#if defined(XPAR_AXIDMA_MOTION_STATS_DEVICE_ID)
    config = XAxiDma_LookupConfig(XPAR_AXIDMA_MOTION_STATS_DEVICE_ID);
#elif defined(XPAR_AXI_DMA_MOTION_STATS_DEVICE_ID)
    config = XAxiDma_LookupConfig(XPAR_AXI_DMA_MOTION_STATS_DEVICE_ID);
#elif defined(XPAR_AXIDMA_0_DEVICE_ID)
    config = XAxiDma_LookupConfig(XPAR_AXIDMA_0_DEVICE_ID);
#else
    config = NULL;
#endif
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

    if (run_case(&elapsed_ticks) != XST_SUCCESS) {
        xil_printf("POSEPRIV_MOTION_STATS_DMA_TEST: FAIL\r\n");
        return XST_FAILURE;
    }

    xil_printf("PASS: %u TX bytes, %u RX bytes, DMA ticks=%lu\r\n",
               INPUT_BEATS * BEAT_BYTES, OUTPUT_BEATS * BEAT_BYTES,
               (unsigned long)elapsed_ticks);
    xil_printf("POSEPRIV_MOTION_STATS_DMA_TEST: PASS\r\n");
    return XST_SUCCESS;
}
