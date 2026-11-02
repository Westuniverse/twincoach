#include "xaxidma.h"
#include "xil_cache.h"
#include "xil_printf.h"
#include "xparameters.h"
#include "xstatus.h"
#include "xiltimer.h"
#include "posepriv_avatar_packet.h"

#include <stdint.h>
#include <string.h>

#define BEAT_BYTES POSEPRIV_AVATAR_AXIS_BEAT_BYTES
#define MATRIX_COUNT POSEPRIV_AVATAR_MATRIX_COUNT
#define MATRIX_BEATS POSEPRIV_AVATAR_MATRIX_PREAMBLE_BEATS
#define VERTEX_COUNT 8U
#define TX_BEATS (MATRIX_BEATS + VERTEX_COUNT)
#define RX_BEATS VERTEX_COUNT
#define POLL_TIMEOUT 10000000U
#define Q14_ONE POSEPRIV_AVATAR_Q14_ONE
#define HALF_WEIGHT 16384U

typedef unsigned __int128 uint128_t;

typedef struct {
    int16_t x;
    int16_t y;
    int16_t z;
    uint8_t matrix_indices[4];
    uint16_t weights[4];
} vertex_request_t;

static XAxiDma dma;
static uint128_t tx_buffer[TX_BEATS] __attribute__((aligned(64)));
static uint128_t rx_buffer[RX_BEATS] __attribute__((aligned(64)));

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

static uint128_t pack_vertex(const vertex_request_t *request)
{
    uint128_t word = 0U;
    uint32_t index;

    word |= ((uint128_t)(uint16_t)request->x) << 0U;
    word |= ((uint128_t)(uint16_t)request->y) << 16U;
    word |= ((uint128_t)(uint16_t)request->z) << 32U;
    for (index = 0U; index < 4U; ++index) {
        word |= ((uint128_t)(request->matrix_indices[index] & 0xFU)) << (48U + index * 4U);
        word |= ((uint128_t)request->weights[index]) << (64U + index * 16U);
    }
    return word;
}

static uint128_t pack_expected(int16_t x, int16_t y, int16_t z)
{
    return ((uint128_t)(uint16_t)x) |
           ((uint128_t)(uint16_t)y << 16U) |
           ((uint128_t)(uint16_t)z << 32U);
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

static int execute_case(int16_t translation_x, uint64_t *elapsed_ticks)
{
    static const int16_t identity[12] = {
        Q14_ONE, 0, 0, 0,
        0, Q14_ONE, 0, 0,
        0, 0, Q14_ONE, 0,
    };
    int16_t translated_x[12];
    vertex_request_t requests[VERTEX_COUNT];
    int16_t expected_x[VERTEX_COUNT];
    uint32_t matrix_index;
    uint32_t vertex_index;
    uint32_t tx_bytes = TX_BEATS * BEAT_BYTES;
    uint32_t rx_bytes = RX_BEATS * BEAT_BYTES;
    XTime dma_start;
    XTime dma_end;
    int status;

    memset(tx_buffer, 0, sizeof(tx_buffer));
    memset(rx_buffer, 0, sizeof(rx_buffer));
    memset(requests, 0, sizeof(requests));
    memcpy(translated_x, identity, sizeof(translated_x));
    translated_x[3] = translation_x;
    for (vertex_index = 0U; vertex_index < VERTEX_COUNT; ++vertex_index) {
        requests[vertex_index].x = (int16_t)(vertex_index * (Q14_ONE / VERTEX_COUNT));
        requests[vertex_index].matrix_indices[0] = 0U;
        requests[vertex_index].matrix_indices[1] = 1U;
        requests[vertex_index].weights[0] = HALF_WEIGHT;
        requests[vertex_index].weights[1] = HALF_WEIGHT;
        expected_x[vertex_index] =
            (int16_t)(requests[vertex_index].x + translation_x / 2);
    }
    for (matrix_index = 0U; matrix_index < MATRIX_COUNT; ++matrix_index) {
        const int16_t *matrix = matrix_index == 0U ? identity :
            (matrix_index == 1U ? translated_x : identity);
        tx_buffer[matrix_index * 2U] = pack_matrix_beat(matrix, 0U);
        tx_buffer[matrix_index * 2U + 1U] = pack_matrix_beat(matrix, 8U);
    }
    for (vertex_index = 0U; vertex_index < VERTEX_COUNT; ++vertex_index) {
        tx_buffer[MATRIX_BEATS + vertex_index] = pack_vertex(&requests[vertex_index]);
    }

    Xil_DCacheFlushRange((UINTPTR)tx_buffer, tx_bytes);
    Xil_DCacheFlushRange((UINTPTR)rx_buffer, rx_bytes);
    XTime_GetTime(&dma_start);

    /* S2MM must be armed before MM2S produces the first vertex result. */
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
    for (vertex_index = 0U; vertex_index < VERTEX_COUNT; ++vertex_index) {
        uint128_t expected = pack_expected(expected_x[vertex_index], 0, 0);
        uint128_t reserved = rx_buffer[vertex_index] >> 64U;
        if (rx_buffer[vertex_index] != expected || reserved != 0U) {
            xil_printf("Mismatch at vertex %lu\r\n", (unsigned long)vertex_index);
            return XST_FAILURE;
        }
    }
    return XST_SUCCESS;
}

int main(void)
{
    XAxiDma_Config *config;
    uint64_t elapsed_ticks;
    uint64_t second_elapsed_ticks;
    int status;

    xil_printf("\r\nPosePriv avatar-skinning packet DMA self-test\r\n");
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

    if (execute_case(Q14_ONE, &elapsed_ticks) != XST_SUCCESS ||
        execute_case(-(int16_t)Q14_ONE, &second_elapsed_ticks) != XST_SUCCESS) {
        xil_printf("POSEPRIV_AVATAR_DMA_TEST: FAIL\r\n");
        return XST_FAILURE;
    }
    elapsed_ticks += second_elapsed_ticks;
    xil_printf("PASS: %lu TX bytes, %lu RX bytes, DMA ticks=%lu\r\n",
               (unsigned long)(TX_BEATS * BEAT_BYTES),
               (unsigned long)(RX_BEATS * BEAT_BYTES),
               (unsigned long)elapsed_ticks);
    xil_printf("Continuous frames: 2 PASS\r\n");
    xil_printf("POSEPRIV_AVATAR_DMA_TEST: PASS\r\n");
    return XST_SUCCESS;
}
