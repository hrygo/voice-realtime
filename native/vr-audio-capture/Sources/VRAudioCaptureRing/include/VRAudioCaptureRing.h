#ifndef VR_AUDIO_CAPTURE_RING_H
#define VR_AUDIO_CAPTURE_RING_H

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct VRAudioCaptureRing VRAudioCaptureRing;

typedef struct {
    uint64_t sequence;
    uint64_t host_time_nanoseconds;
    uint32_t sample_rate;
    uint32_t frame_count;
    uint16_t channels;
    uint16_t bytes_per_sample;
} VRAudioCaptureRingMetadata;

typedef struct {
    VRAudioCaptureRingMetadata metadata;
    uint32_t payload_byte_count;
} VRAudioCaptureRingRecord;

enum {
    VR_AUDIO_CAPTURE_RING_PUSH_STORED = 0,
    VR_AUDIO_CAPTURE_RING_PUSH_STORED_DROPPING_OLDEST = 1,
    VR_AUDIO_CAPTURE_RING_PUSH_PAYLOAD_TOO_LARGE = 2,
    VR_AUDIO_CAPTURE_RING_PUSH_DROPPED_INCOMING = 3,
};

VRAudioCaptureRing *vr_audio_capture_ring_create(
    uint32_t capacity,
    uint32_t maximum_payload_bytes
);

void vr_audio_capture_ring_destroy(VRAudioCaptureRing *ring);

int32_t vr_audio_capture_ring_push(
    VRAudioCaptureRing *ring,
    const VRAudioCaptureRingMetadata *metadata,
    const void *payload,
    uint32_t payload_byte_count
);

bool vr_audio_capture_ring_pop(
    VRAudioCaptureRing *ring,
    void *destination,
    uint32_t destination_byte_count,
    VRAudioCaptureRingRecord *record
);

uint32_t vr_audio_capture_ring_capacity(const VRAudioCaptureRing *ring);
uint32_t vr_audio_capture_ring_maximum_payload_bytes(const VRAudioCaptureRing *ring);
uint32_t vr_audio_capture_ring_count(const VRAudioCaptureRing *ring);
uint64_t vr_audio_capture_ring_dropped_records(const VRAudioCaptureRing *ring);

/* Clear is only valid while producer and consumer are quiescent. */
void vr_audio_capture_ring_clear(VRAudioCaptureRing *ring);

#ifdef __cplusplus
}
#endif

#endif
