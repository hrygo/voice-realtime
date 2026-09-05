#include "VRAudioCaptureRing.h"

#include <stdatomic.h>
#include <stddef.h>
#include <stdlib.h>
#include <string.h>

#define SONA_AUDIO_CAPTURE_RING_NO_READER UINT64_MAX
#define SONA_AUDIO_CAPTURE_RING_MAX_CAPACITY UINT32_C(65536)
#define SONA_AUDIO_CAPTURE_RING_MAX_PAYLOAD_STORAGE ((size_t)64 * 1024 * 1024)

typedef struct {
    SonaAudioCaptureRingMetadata metadata;
    uint32_t payload_byte_count;
    uint8_t *payload;
} SonaAudioCaptureRingSlot;

struct SonaAudioCaptureRing {
    uint32_t capacity;
    uint32_t maximum_payload_bytes;
    SonaAudioCaptureRingSlot *slots;
    uint8_t *payload_storage;
    _Atomic uint64_t read_index;
    _Atomic uint64_t write_index;
    _Atomic uint64_t reader_index;
    _Atomic uint64_t dropped_records;
};

static bool vr_audio_capture_ring_target_is_being_read(
    const SonaAudioCaptureRing *ring,
    uint64_t write_index
) {
    if (write_index < ring->capacity) {
        return false;
    }
    uint64_t reader_index = atomic_load_explicit(
        &ring->reader_index,
        memory_order_seq_cst
    );
    return reader_index == write_index - ring->capacity;
}

SonaAudioCaptureRing *vr_audio_capture_ring_create(
    uint32_t capacity,
    uint32_t maximum_payload_bytes
) {
    if (
        capacity == 0 || maximum_payload_bytes == 0 ||
        capacity > SONA_AUDIO_CAPTURE_RING_MAX_CAPACITY ||
        (size_t)maximum_payload_bytes > SONA_AUDIO_CAPTURE_RING_MAX_PAYLOAD_STORAGE ||
        (size_t)capacity >
            SONA_AUDIO_CAPTURE_RING_MAX_PAYLOAD_STORAGE / maximum_payload_bytes
    ) {
        return NULL;
    }
    if ((size_t)capacity > SIZE_MAX / sizeof(SonaAudioCaptureRingSlot)) {
        return NULL;
    }
    if ((size_t)capacity > SIZE_MAX / maximum_payload_bytes) {
        return NULL;
    }

    SonaAudioCaptureRing *ring = calloc(1, sizeof(*ring));
    if (ring == NULL) {
        return NULL;
    }
    ring->slots = calloc(capacity, sizeof(*ring->slots));
    ring->payload_storage = calloc(capacity, maximum_payload_bytes);
    if (ring->slots == NULL || ring->payload_storage == NULL) {
        vr_audio_capture_ring_destroy(ring);
        return NULL;
    }
    ring->capacity = capacity;
    ring->maximum_payload_bytes = maximum_payload_bytes;
    for (uint32_t index = 0; index < capacity; index += 1) {
        ring->slots[index].payload =
            ring->payload_storage + ((size_t)index * maximum_payload_bytes);
    }
    atomic_init(&ring->read_index, 0);
    atomic_init(&ring->write_index, 0);
    atomic_init(&ring->reader_index, SONA_AUDIO_CAPTURE_RING_NO_READER);
    atomic_init(&ring->dropped_records, 0);
    if (
        !atomic_is_lock_free(&ring->read_index) ||
        !atomic_is_lock_free(&ring->write_index) ||
        !atomic_is_lock_free(&ring->reader_index) ||
        !atomic_is_lock_free(&ring->dropped_records)
    ) {
        vr_audio_capture_ring_destroy(ring);
        return NULL;
    }
    return ring;
}

void vr_audio_capture_ring_destroy(SonaAudioCaptureRing *ring) {
    if (ring == NULL) {
        return;
    }
    if (ring->payload_storage != NULL) {
        memset(
            ring->payload_storage,
            0,
            (size_t)ring->capacity * ring->maximum_payload_bytes
        );
    }
    free(ring->payload_storage);
    free(ring->slots);
    free(ring);
}

int32_t vr_audio_capture_ring_push(
    SonaAudioCaptureRing *ring,
    const SonaAudioCaptureRingMetadata *metadata,
    const void *payload,
    uint32_t payload_byte_count
) {
    if (
        ring == NULL || metadata == NULL ||
        payload_byte_count > ring->maximum_payload_bytes ||
        (payload_byte_count > 0 && payload == NULL)
    ) {
        return SONA_AUDIO_CAPTURE_RING_PUSH_PAYLOAD_TOO_LARGE;
    }

    uint64_t write_index = atomic_load_explicit(
        &ring->write_index,
        memory_order_relaxed
    );
    if (vr_audio_capture_ring_target_is_being_read(ring, write_index)) {
        atomic_fetch_add_explicit(
            &ring->dropped_records,
            1,
            memory_order_relaxed
        );
        return SONA_AUDIO_CAPTURE_RING_PUSH_DROPPED_INCOMING;
    }

    bool dropped_oldest = false;
    for (;;) {
        uint64_t read_index = atomic_load_explicit(
            &ring->read_index,
            memory_order_seq_cst
        );
        if (write_index - read_index < ring->capacity) {
            break;
        }
        uint64_t expected = read_index;
        if (atomic_compare_exchange_strong_explicit(
                &ring->read_index,
                &expected,
                read_index + 1,
                memory_order_seq_cst,
                memory_order_seq_cst
            )) {
            dropped_oldest = true;
            atomic_fetch_add_explicit(
                &ring->dropped_records,
                1,
                memory_order_relaxed
            );
            break;
        }
    }

    if (vr_audio_capture_ring_target_is_being_read(ring, write_index)) {
        if (!dropped_oldest) {
            atomic_fetch_add_explicit(
                &ring->dropped_records,
                1,
                memory_order_relaxed
            );
        }
        return SONA_AUDIO_CAPTURE_RING_PUSH_DROPPED_INCOMING;
    }

    SonaAudioCaptureRingSlot *slot = &ring->slots[write_index % ring->capacity];
    slot->metadata = *metadata;
    slot->payload_byte_count = payload_byte_count;
    if (payload_byte_count > 0) {
        memcpy(slot->payload, payload, payload_byte_count);
    }
    atomic_store_explicit(
        &ring->write_index,
        write_index + 1,
        memory_order_release
    );
    return dropped_oldest
        ? SONA_AUDIO_CAPTURE_RING_PUSH_STORED_DROPPING_OLDEST
        : SONA_AUDIO_CAPTURE_RING_PUSH_STORED;
}

bool vr_audio_capture_ring_pop(
    SonaAudioCaptureRing *ring,
    void *destination,
    uint32_t destination_byte_count,
    SonaAudioCaptureRingRecord *record
) {
    if (ring == NULL || destination == NULL || record == NULL) {
        return false;
    }
    for (;;) {
        uint64_t read_index = atomic_load_explicit(
            &ring->read_index,
            memory_order_seq_cst
        );
        uint64_t write_index = atomic_load_explicit(
            &ring->write_index,
            memory_order_acquire
        );
        if (read_index == write_index) {
            return false;
        }

        atomic_store_explicit(
            &ring->reader_index,
            read_index,
            memory_order_seq_cst
        );
        if (atomic_load_explicit(&ring->read_index, memory_order_seq_cst) !=
            read_index) {
            atomic_store_explicit(
                &ring->reader_index,
                SONA_AUDIO_CAPTURE_RING_NO_READER,
                memory_order_seq_cst
            );
            continue;
        }

        SonaAudioCaptureRingSlot *slot = &ring->slots[read_index % ring->capacity];
        if (slot->payload_byte_count > destination_byte_count) {
            atomic_store_explicit(
                &ring->reader_index,
                SONA_AUDIO_CAPTURE_RING_NO_READER,
                memory_order_seq_cst
            );
            return false;
        }
        record->metadata = slot->metadata;
        record->payload_byte_count = slot->payload_byte_count;
        if (slot->payload_byte_count > 0) {
            memcpy(destination, slot->payload, slot->payload_byte_count);
        }

        uint64_t expected = read_index;
        atomic_compare_exchange_strong_explicit(
            &ring->read_index,
            &expected,
            read_index + 1,
            memory_order_seq_cst,
            memory_order_seq_cst
        );
        atomic_store_explicit(
            &ring->reader_index,
            SONA_AUDIO_CAPTURE_RING_NO_READER,
            memory_order_seq_cst
        );
        return true;
    }
}

uint32_t vr_audio_capture_ring_capacity(const SonaAudioCaptureRing *ring) {
    return ring == NULL ? 0 : ring->capacity;
}

uint32_t vr_audio_capture_ring_maximum_payload_bytes(
    const SonaAudioCaptureRing *ring
) {
    return ring == NULL ? 0 : ring->maximum_payload_bytes;
}

uint32_t vr_audio_capture_ring_count(const SonaAudioCaptureRing *ring) {
    if (ring == NULL) {
        return 0;
    }
    uint64_t read_index = atomic_load_explicit(
        &ring->read_index,
        memory_order_acquire
    );
    uint64_t write_index = atomic_load_explicit(
        &ring->write_index,
        memory_order_acquire
    );
    uint64_t count = write_index - read_index;
    return count > ring->capacity ? ring->capacity : (uint32_t)count;
}

uint64_t vr_audio_capture_ring_dropped_records(const SonaAudioCaptureRing *ring) {
    if (ring == NULL) {
        return 0;
    }
    return atomic_load_explicit(&ring->dropped_records, memory_order_relaxed);
}

void vr_audio_capture_ring_clear(SonaAudioCaptureRing *ring) {
    if (ring == NULL) {
        return;
    }
    uint64_t write_index = atomic_load_explicit(
        &ring->write_index,
        memory_order_acquire
    );
    atomic_store_explicit(&ring->read_index, write_index, memory_order_release);
    atomic_store_explicit(
        &ring->reader_index,
        SONA_AUDIO_CAPTURE_RING_NO_READER,
        memory_order_release
    );
    atomic_store_explicit(&ring->dropped_records, 0, memory_order_relaxed);
    memset(
        ring->payload_storage,
        0,
        (size_t)ring->capacity * ring->maximum_payload_bytes
    );
    memset(ring->slots, 0, (size_t)ring->capacity * sizeof(*ring->slots));
    for (uint32_t index = 0; index < ring->capacity; index += 1) {
        ring->slots[index].payload =
            ring->payload_storage +
            ((size_t)index * ring->maximum_payload_bytes);
    }
}
