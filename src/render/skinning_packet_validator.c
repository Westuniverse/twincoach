/* SPDX-License-Identifier: GPL-2.0-only */
/* CPU userspace wrapper of the existing byte contract; no MMIO or arithmetic. */
#include <stdint.h>
#include <stddef.h>
#include <string.h>
#include "../board_drivers/posepriv_shared_dma/posepriv_skinning_contract.h"

uint32_t posepriv_skinning_validator_abi(void) { return 1; }

uint32_t posepriv_skinning_validate_input_v1(const unsigned char *packet, uint32_t bytes)
{
    if (!packet || !posepriv_shared_output_bytes(5, bytes)) return 0;
    return posepriv_skinning_payload_valid(packet, bytes) ? (bytes-512U)/16U : 0;
}

uint32_t posepriv_skinning_validate_output_v1(const unsigned char *packet, uint32_t bytes)
{
    if (!packet || !bytes || bytes > POSEPRIV_SHARED_CAPACITY || (bytes & 15U)) return 0;
    return posepriv_skinning_output_valid(packet, bytes+512U) ? 1U : 0U;
}

/* Container-only upper64 check; transport/driver still validate bytes6..15. */
uint32_t posepriv_skinning_upper64_zero_v1(const unsigned char *packet, size_t bytes)
{
    uint64_t accumulated = 0;
    if ((bytes & 15U) || (bytes && !packet)) return 0;
    for (size_t offset = 8; offset < bytes; offset += 16) {
        uint64_t word;
        memcpy(&word, packet+offset, sizeof(word));
        accumulated |= word;
    }
    return accumulated == 0 ? 1U : 0U;
}
