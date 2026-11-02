#include "xaxidma.h"
#include "xil_cache.h"
#include "xil_printf.h"
#include "xparameters.h"
#include "xstatus.h"
#include "xiltimer.h"
#include "posepriv_rectified_stereo_packet.h"

#include <stdint.h>
#include <string.h>

#define POINT_COUNT 4U
#define BEAT_BYTES POSEPRIV_RECTIFIED_STEREO_AXIS_BEAT_BYTES
#define TX_BYTES (POINT_COUNT * POSEPRIV_RECTIFIED_STEREO_REQUEST_BYTES)
#define RX_BYTES (POINT_COUNT * POSEPRIV_RECTIFIED_STEREO_RESULT_BYTES)
#define POLL_TIMEOUT 10000000U

typedef struct {
    int32_t x;
    int32_t y;
    int32_t z;
    uint8_t joint_id;
    uint8_t confidence;
    uint8_t valid;
    uint16_t disparity_abs;
} expected_result_t;

static XAxiDma dma;
static uint8_t tx_buffer[TX_BYTES] __attribute__((aligned(64)));
static uint8_t rx_buffer[RX_BYTES] __attribute__((aligned(64)));

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

static void build_requests(expected_result_t expected[POINT_COUNT])
{
    posepriv_rectified_stereo_pack_request(
        tx_buffer + 0U * BEAT_BYTES,
        819, 410, -819, 410, 3277, 16384, 7, 240, 230);
    expected[0] = (expected_result_t){1638, 820, 32778, 7, 230, 1, 1638};

    posepriv_rectified_stereo_pack_request(
        tx_buffer + 1U * BEAT_BYTES,
        100, 200, 100, 200, 3277, 16384, 8, 255, 255);
    expected[1] = (expected_result_t){0, 0, 0, 8, 255, 0, 0};

    posepriv_rectified_stereo_pack_request(
        tx_buffer + 2U * BEAT_BYTES,
        -819, -410, 819, -410, 3277, 16384, 9, 200, 210);
    expected[2] = (expected_result_t){1638, 820, -32778, 9, 200, 1, 1638};

    posepriv_rectified_stereo_pack_request(
        tx_buffer + 3U * BEAT_BYTES,
        600, 100, 0, 100, 3277, 16384, 10, 200, 201);
    expected[3] = (expected_result_t){3277, 546, 89483, 10, 200, 1, 600};
}

static int execute_batch(uint64_t *elapsed_ticks)
{
    expected_result_t expected[POINT_COUNT];
    XTime dma_start;
    XTime dma_end;
    uint32_t index;
    int status;

    memset(tx_buffer, 0, sizeof(tx_buffer));
    memset(rx_buffer, 0, sizeof(rx_buffer));
    build_requests(expected);

    Xil_DCacheFlushRange((UINTPTR)tx_buffer, TX_BYTES);
    Xil_DCacheFlushRange((UINTPTR)rx_buffer, RX_BYTES);
    XTime_GetTime(&dma_start);

    /* Arm S2MM before MM2S so the first result cannot be dropped. */
    status = XAxiDma_SimpleTransfer(
        &dma, (UINTPTR)rx_buffer, RX_BYTES, XAXIDMA_DEVICE_TO_DMA);
    if (status != XST_SUCCESS) {
        xil_printf("S2MM start failed: %d\r\n", status);
        return XST_FAILURE;
    }
    status = XAxiDma_SimpleTransfer(
        &dma, (UINTPTR)tx_buffer, TX_BYTES, XAXIDMA_DMA_TO_DEVICE);
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

    Xil_DCacheInvalidateRange((UINTPTR)rx_buffer, RX_BYTES);
    for (index = 0U; index < POINT_COUNT; ++index) {
        posepriv_rectified_stereo_result_t actual;
        const expected_result_t *want = &expected[index];
        posepriv_rectified_stereo_unpack_result(
            &actual, rx_buffer + index * BEAT_BYTES);
        if (actual.x != want->x || actual.y != want->y || actual.z != want->z ||
            actual.joint_id != want->joint_id ||
            actual.confidence != want->confidence ||
            actual.valid != want->valid ||
            actual.disparity_abs != want->disparity_abs) {
            xil_printf("Mismatch at joint %lu\r\n", (unsigned long)index);
            return XST_FAILURE;
        }
    }
    return XST_SUCCESS;
}

int main(void)
{
    XAxiDma_Config *config;
    uint64_t first_ticks;
    uint64_t second_ticks;
    int status;

    xil_printf("\r\nPosePriv rectified-stereo DMA self-test\r\n");
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

    if (execute_batch(&first_ticks) != XST_SUCCESS ||
        execute_batch(&second_ticks) != XST_SUCCESS) {
        xil_printf("POSEPRIV_RECTIFIED_DMA_TEST: FAIL\r\n");
        return XST_FAILURE;
    }
    xil_printf("PASS: %lu TX bytes, %lu RX bytes, DMA ticks=%lu\r\n",
               (unsigned long)TX_BYTES,
               (unsigned long)RX_BYTES,
               (unsigned long)(first_ticks + second_ticks));
    xil_printf("Continuous batches: 2 PASS\r\n");
    xil_printf("POSEPRIV_RECTIFIED_DMA_TEST: PASS\r\n");
    return XST_SUCCESS;
}
