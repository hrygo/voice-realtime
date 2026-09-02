#ifndef SONA_AUDIO_CAPTURE_RING_H
#define SONA_AUDIO_CAPTURE_RING_H

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct SonaAudioCaptureRing SonaAudioCaptureRing;

typedef struct {
    uint64_t sequence;
    uint64_t host_time_nanoseconds;
    uint32_t sample_rate;
    uint32_t frame_count;
    uint16_t channels;
    uint16_t bytes_per_sample;
} SonaAudioCaptureRingMetadata;

typedef struct {
    SonaAudioCaptureRingMetadata metadata;
    uint32_t payload_byte_count;
} SonaAudioCaptureRingRecord;

enum {
    SONA_AUDIO_CAPTURE_RING_PUSH_STORED = 0,
    SONA_AUDIO_CAPTURE_RING_PUSH_STORED_DROPPING_OLDEST = 1,
    SONA_AUDIO_CAPTURE_RING_PUSH_PAYLOAD_TOO_LARGE = 2,
    SONA_AUDIO_CAPTURE_RING_PUSH_DROPPED_INCOMING = 3,
};

SonaAudioCaptureRing *vr_audio_capture_ring_create(
    uint32_t capacity,
    uint32_t maximum_payload_bytes
);

void vr_audio_capture_ring_destroy(SonaAudioCaptureRing *ring);

int32_t vr_audio_capture_ring_push(
    SonaAudioCaptureRing *ring,
    const SonaAudioCaptureRingMetadata *metadata,
    const void *payload,
    uint32_t payload_byte_count
);

bool vr_audio_capture_ring_pop(
    SonaAudioCaptureRing *ring,
    void *destination,
    uint32_t destination_byte_count,
    SonaAudioCaptureRingRecord *record
);

uint32_t vr_audio_capture_ring_capacity(const SonaAudioCaptureRing *ring);
uint32_t vr_audio_capture_ring_maximum_payload_bytes(const SonaAudioCaptureRing *ring);
uint32_t vr_audio_capture_ring_count(const SonaAudioCaptureRing *ring);
uint64_t vr_audio_capture_ring_dropped_records(const SonaAudioCaptureRing *ring);

/* Clear is only valid while producer and consumer are quiescent. */
void vr_audio_capture_ring_clear(SonaAudioCaptureRing *ring);

#ifdef __cplusplus
}
#endif

#endif
